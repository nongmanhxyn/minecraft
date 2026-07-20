import asyncio
import itertools
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

from groq import AsyncGroq, RateLimitError
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

load_dotenv()

BOT_NAMES = ["BotAlpha", "BotBeta", "BotGamma"]
MINECRAFT_HOST = "dynamic-8.magmanode.com"
MINECRAFT_PORT = 25788
MINECRAFT_VERSION = "1.21.11"

# --- Giới hạn an toàn cho hành động do AI sinh ra -----------------------------
WORLD_MIN_Y = -64
WORLD_MAX_Y = 320
MAX_MOVE_DISTANCE = 128  # AI không được ra lệnh xa quá bán kính này so với vị trí hiện tại
STATUS_TIMEOUT = 45.0    # nếu quá lâu không nhận status từ Node -> coi như tiến trình bị treo

ACTION_TIMEOUTS = {
    "moveTo": 25.0,
    "mineBlock": 15.0,
    "placeBlock": 15.0,
    "equipItem": 8.0,
    "attack": 8.0,
}

VALID_EQUIP_DESTINATIONS = {"hand", "off-hand", "head", "torso", "legs", "feet"}


def _extract_json(raw: str) -> dict:
    """Groq bật response_format=json_object nên gần như luôn trả JSON thuần,
    nhưng vẫn phòng trường hợp model bọc thêm ```json ... ``` hoặc dư khoảng trắng."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # cố gắng lấy khối {...} đầu tiên trong chuỗi trả về
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


class GroqKeyPool:
    def __init__(self, keys: List[str]):
        if not keys:
            raise ValueError("Cần ít nhất một API key")
        self.keys = list(keys)
        self.current_index = 0
        self.lock = asyncio.Lock()

    async def get_client(self) -> AsyncGroq:
        async with self.lock:
            key = self.keys[self.current_index]
            return AsyncGroq(api_key=key)

    async def report_rate_limit(self):
        async with self.lock:
            self.current_index = (self.current_index + 1) % len(self.keys)
            print(f"[KeyPool] Rate limit hit, chuyển key index {self.current_index}")


class MinecraftBot:
    def __init__(self, name: str, key_pool: GroqKeyPool, host: str, port: int, version: str):
        self.name = name
        self.key_pool = key_pool
        self.host = host
        self.port = port
        self.version = version
        self.process: Optional[asyncio.subprocess.Process] = None
        self.state = {
            "position": (0, 0, 0),
            "health": 20.0,
            "time": "day",
            "entities": [],
            "messages": [],
            "inventory": [],
            "last_action": None,
        }
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.new_message_event = asyncio.Event()
        self.stop_event = asyncio.Event()

        # Sổ theo dõi lệnh đã gửi xuống Node, chờ kết quả (ok/fail) trước khi làm bước kế tiếp
        self.pending_actions: Dict[str, asyncio.Future] = {}
        self._cmd_counter = itertools.count(1)
        self.last_status_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    # Quản lý tiến trình Node.js + kết nối lại
    # ------------------------------------------------------------------ #
    async def start(self):
        while not self.stop_event.is_set():
            stderr_task = None
            try:
                print(f"[{self.name}] Đang kết nối tới server qua Node.js...")
                self.process = await asyncio.create_subprocess_exec(
                    "node", "mineflayer_bot.js",
                    self.name, self.host, str(self.port), self.version,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.last_status_ts = time.monotonic()

                stderr_task = asyncio.create_task(self.read_stderr())
                await self.read_stdout_until_close()

            except Exception as e:
                print(f"[{self.name}] Lỗi process: {e}")
            finally:
                if stderr_task:
                    stderr_task.cancel()
                await self._cleanup_process()

            if not self.stop_event.is_set():
                code = self.process.returncode if self.process else "?"
                print(f"[{self.name}] Process đã dừng (mã thoát: {code}), thử kết nối lại sau 10s...")
                await asyncio.sleep(10)

    async def _cleanup_process(self):
        """Đảm bảo tiến trình Node được dọn dẹp sạch (không để zombie), tránh
        ProcessLookupError nếu tiến trình đã tự thoát trước đó."""
        if not self.process:
            return
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
        # bất kỳ lệnh nào còn đang chờ ack đều phải được giải phóng, không để treo mãi
        for fut in self.pending_actions.values():
            if not fut.done():
                fut.set_result((False, "Node process đã đóng"))
        self.pending_actions.clear()

    async def watchdog(self):
        """Nếu tiến trình Node còn sống nhưng quá lâu không gửi status
        (ví dụ treo mạng âm thầm mà không phát sự kiện error/end), chủ động
        kill để vòng lặp start() phát hiện và kết nối lại."""
        while not self.stop_event.is_set():
            await asyncio.sleep(10)
            if self.process and self.process.returncode is None:
                if time.monotonic() - self.last_status_ts > STATUS_TIMEOUT:
                    print(f"[{self.name}] ⚠️ Không nhận status hơn {STATUS_TIMEOUT:.0f}s, "
                          f"nghi ngờ tiến trình bị treo -> khởi động lại")
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    self.last_status_ts = time.monotonic()

    async def read_stderr(self):
        try:
            while self.process and self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break
                print(f"[{self.name} Node]: {line.decode(errors='replace').strip()}")
        except Exception:
            pass

    async def read_stdout_until_close(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode(errors="replace").strip())
                except Exception:
                    continue

                event = data.get("event")
                if event == "status":
                    self.last_status_ts = time.monotonic()
                    self.state["position"] = tuple(data.get("position", (0, 0, 0)))
                    self.state["health"] = data.get("health", 20)
                    self.state["entities"] = data.get("entities", [])
                    self.state["time"] = data.get("time", "day")
                    self.state["inventory"] = data.get("inventory", [])
                elif event == "actionResult":
                    cmd_id = data.get("id")
                    fut = self.pending_actions.get(cmd_id)
                    if fut and not fut.done():
                        fut.set_result((bool(data.get("ok", False)), data.get("message", "")))
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Chat nội bộ giữa các bot
    # ------------------------------------------------------------------ #
    async def message_processor(self):
        while not self.stop_event.is_set():
            try:
                msg = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                self.state["messages"].append(msg)
                if len(self.state["messages"]) > 10:
                    self.state["messages"].pop(0)
                print(f"💬 [CHAT NỘI BỘ] {msg['from']} ➔ {self.name}: \"{msg['text']}\"")
                self.new_message_event.set()
            except asyncio.TimeoutError:
                pass

    async def send_bot_message(self, target: str, text: str):
        # Tra thẳng từ registry toàn cục thay vì monkey-patch, tránh phụ thuộc
        # vào thứ tự khởi tạo bot.
        target_bot = bot_instances.get(target)
        if target_bot:
            await target_bot.message_queue.put({"from": self.name, "text": text})
        else:
            print(f"[{self.name}] Không tìm thấy bot đích để nhắn tin: {target}")

    # ------------------------------------------------------------------ #
    # Xây dựng prompt cho Groq
    # ------------------------------------------------------------------ #
    def build_prompt(self):
        pos = self.state["position"]
        health = self.state["health"]
        time_of_day = self.state["time"]

        inv_desc = ", ".join([f"{item['count']}x {item['name']}" for item in self.state["inventory"]])
        if not inv_desc:
            inv_desc = "Túi đồ trống"

        entities_desc = "\n".join([
            f"- {e.get('name')} ({e.get('type')}) ở {e.get('position')}, cách {e.get('distance')}m"
            for e in self.state["entities"][:10]
        ])
        msgs = "\n".join([f"{m['from']}: {m['text']}" for m in self.state["messages"]])

        other_bots = [b for b in BOT_NAMES if b != self.name]

        return f"""Bạn là bot Minecraft tên {self.name}, đồng đội của {', '.join(other_bots)}.
Nhiệm vụ: sinh tồn, hỗ trợ đồng đội, làm việc chung.

Trạng thái hiện tại:
- Vị trí (x,y,z): {pos}
- Máu: {health}/20
- Thời gian: {time_of_day}
- Túi đồ: {inv_desc}
- Thực thể xung quanh (đã sắp xếp gần -> xa):
{entities_desc if entities_desc else "Không có"}
- Chat nội bộ gần đây:
{msgs if msgs else "Không có tin nhắn mới"}

YÊU CẦU BẮT BUỘC:
1. Trả về DUY NHẤT một object JSON, không kèm text, giải thích hay markdown nào khác.
2. Object JSON PHẢI luôn có key "actions" là một mảng (có thể là mảng rỗng [] nếu không cần làm gì).
3. Tất cả tọa độ (x, y, z) PHẢI LÀ SỐ NGUYÊN (Integer), không dùng số thập phân.
4. Chỉ sử dụng các 'type' hành động sau:
- "sendMessageToBot": Gửi tin nhắn nội bộ (cần "botName", "message").
- "moveTo": Đi tới tọa độ (cần "x", "y", "z" là SỐ NGUYÊN).
- "mineBlock": Đập block tại tọa độ (cần "x", "y", "z" là SỐ NGUYÊN).
- "placeBlock": Đặt block (cần "x", "y", "z" là SỐ NGUYÊN, "blockName" là tên block bằng tiếng Anh vd: "dirt", "cobblestone").
- "equipItem": Cầm đồ ra tay hoặc mặc giáp (cần "itemName" bằng tiếng Anh, "destination" chọn 1 trong: "hand", "head", "torso", "legs", "feet").
- "attack": Tấn công (cần "target": "nearest_hostile").
5. Mỗi hành động di chuyển/đào/đặt nên nằm trong bán kính khoảng {MAX_MOVE_DISTANCE} block quanh vị trí hiện tại.

Khuôn mẫu JSON trả về:
{{
  "actions": [
    {{"type": "sendMessageToBot", "botName": "{other_bots[0]}", "message": "Tới phụ t đập đá ở tọa độ này nè!"}},
    {{"type": "equipItem", "itemName": "iron_pickaxe", "destination": "hand"}},
    {{"type": "moveTo", "x": 100, "y": 64, "z": -200}},
    {{"type": "mineBlock", "x": 100, "y": 63, "z": -200}}
  ]
}}"""

    # ------------------------------------------------------------------ #
    # Gọi Groq
    # ------------------------------------------------------------------ #
    async def call_groq_with_retry(self, prompt: str, max_retries=None):
        if max_retries is None:
            max_retries = len(self.key_pool.keys) * 2
        retries = 0
        while retries < max_retries:
            try:
                client = await self.key_pool.get_client()
                response = await client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": "Bạn là AI điều khiển Minecraft, output 100% JSON hợp lệ, "
                                                       "không thêm bất kỳ ký tự nào ngoài JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=1600,  # tăng so với 500: tránh JSON bị cắt cụt khi có nhiều action
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except RateLimitError:
                await self.key_pool.report_rate_limit()
                retries += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[{self.name}] Lỗi Groq API: {e}")
                retries += 1
                await asyncio.sleep(2)
        raise RuntimeError(f"[{self.name}] Hết key Groq khả dụng")

    async def make_decision(self):
        try:
            prompt = self.build_prompt()
            content = await self.call_groq_with_retry(prompt)
        except Exception as e:
            print(f"[{self.name}] Lỗi gọi Groq: {e}")
            return

        try:
            data = _extract_json(content)
        except json.JSONDecodeError as e:
            print(f"[{self.name}] Groq trả JSON không hợp lệ ({e}). Raw (300 ký tự đầu): {content[:300]!r}")
            return

        actions = data.get("actions")
        if not isinstance(actions, list):
            print(f"[{self.name}] Groq không trả 'actions' hợp lệ (thiếu hoặc sai kiểu): {data}")
            return

        self.state["last_action"] = actions
        for action in actions:
            if not isinstance(action, dict):
                print(f"[{self.name}] Bỏ qua action không phải object: {action}")
                continue
            await self.execute_action(action)

    async def decision_loop(self):
        await asyncio.sleep(10)
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.new_message_event.wait(), timeout=8.0)
                self.new_message_event.clear()
                await self.make_decision()
            except asyncio.TimeoutError:
                await self.make_decision()

    # ------------------------------------------------------------------ #
    # Chuẩn hoá & thực thi hành động
    # ------------------------------------------------------------------ #
    def _clamp_coords(self, x: int, y: int, z: int) -> Tuple[int, int, int]:
        cur_x, cur_y, cur_z = self.state["position"]
        x = max(cur_x - MAX_MOVE_DISTANCE, min(cur_x + MAX_MOVE_DISTANCE, x))
        z = max(cur_z - MAX_MOVE_DISTANCE, min(cur_z + MAX_MOVE_DISTANCE, z))
        y = max(WORLD_MIN_Y, min(WORLD_MAX_Y, y))
        return x, y, z

    def _parse_xyz(self, action: dict) -> Tuple[int, int, int]:
        # float/chuỗi số đều được chấp nhận rồi làm tròn về int; thiếu key -> KeyError
        # được bắt ở nơi gọi và bị bỏ qua an toàn.
        x = int(round(float(action["x"])))
        y = int(round(float(action["y"])))
        z = int(round(float(action["z"])))
        return self._clamp_coords(x, y, z)

    async def execute_action(self, action: dict):
        action_type = action.get("type")

        if action_type == "sendMessageToBot":
            target = action.get("botName")
            message = action.get("message")
            if target and message:
                await self.send_bot_message(target, message)
            else:
                print(f"[{self.name}] Bỏ qua sendMessageToBot thiếu botName/message: {action}")
            return

        if not self.process or self.process.returncode is not None:
            print(f"[{self.name}] Bỏ qua hành động '{action_type}': tiến trình Node.js chưa sẵn sàng")
            return

        cmd = None
        try:
            if action_type == "moveTo":
                x, y, z = self._parse_xyz(action)
                cmd = {"action": "moveTo", "x": x, "y": y, "z": z}

            elif action_type == "mineBlock":
                x, y, z = self._parse_xyz(action)
                cmd = {"action": "mineBlock", "x": x, "y": y, "z": z}

            elif action_type == "placeBlock":
                x, y, z = self._parse_xyz(action)
                block_name = action.get("blockName") or "dirt"
                cmd = {"action": "placeBlock", "x": x, "y": y, "z": z, "blockName": str(block_name)}

            elif action_type == "equipItem":
                item_name = action.get("itemName")
                if not item_name:
                    print(f"[{self.name}] Bỏ qua equipItem thiếu itemName: {action}")
                    return
                destination = action.get("destination", "hand")
                if destination not in VALID_EQUIP_DESTINATIONS:
                    destination = "hand"
                cmd = {"action": "equipItem", "itemName": str(item_name), "destination": destination}

            elif action_type == "attack":
                cmd = {"action": "attack", "target": action.get("target", "nearest_hostile")}

            else:
                print(f"[{self.name}] Bỏ qua action type không xác định: {action_type}")
                return

        except (KeyError, ValueError, TypeError) as e:
            print(f"[{self.name}] Bỏ qua lệnh lỗi từ Groq (thiếu key hoặc sai kiểu dữ liệu): {action} - Lỗi: {e}")
            return

        await self._send_command_and_wait(action_type, cmd)

    async def _send_command_and_wait(self, action_type: str, cmd: dict):
        """Gửi lệnh xuống Node kèm id duy nhất, rồi CHỜ Node xác nhận thành công/thất bại
        trước khi trả về. Nhờ vậy các hành động trong cùng 1 quyết định (vd: moveTo rồi
        mineBlock) sẽ chạy tuần tự đúng nghĩa, thay vì bắn lệnh chồng lên nhau khi bot
        còn chưa kịp di chuyển tới nơi."""
        cmd_id = f"{self.name}-{next(self._cmd_counter)}"
        cmd["id"] = cmd_id

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_actions[cmd_id] = fut

        try:
            self.process.stdin.write((json.dumps(cmd) + "\n").encode())
            await self.process.stdin.drain()
        except Exception as e:
            print(f"[{self.name}] Lỗi gửi lệnh Node: {e}")
            self.pending_actions.pop(cmd_id, None)
            return

        timeout = ACTION_TIMEOUTS.get(action_type, 10.0)
        try:
            ok, message = await asyncio.wait_for(fut, timeout=timeout)
            icon = "✅" if ok else "⚠️"
            print(f"[{self.name}] {icon} {action_type} -> {message}")
        except asyncio.TimeoutError:
            print(f"[{self.name}] ⏱️ Timeout chờ kết quả '{action_type}' (id={cmd_id})")
        finally:
            self.pending_actions.pop(cmd_id, None)


app = FastAPI()
bot_instances: Dict[str, MinecraftBot] = {}


@app.get("/api/status")
async def get_status():
    status = {}
    for name, bot in bot_instances.items():
        status[name] = {
            "name": bot.name,
            "position": bot.state["position"],
            "health": bot.state["health"],
            "inventory": bot.state["inventory"],
            "time": bot.state["time"],
            "entities": bot.state["entities"][:5],
            "messages": bot.state["messages"][-5:],
            "last_action": bot.state.get("last_action"),
            "process_alive": bool(bot.process and bot.process.returncode is None),
        }
    return status


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Minecraft Bot Dashboard</title><meta charset="UTF-8"></head>
    <body style="background: #1e1e1e; color: #ddd; font-family: Arial; padding: 20px;">
        <h1>Bot Dashboard</h1>
        <div id="bots"></div>
        <script>
            async function fetchStatus() {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                document.getElementById('bots').innerHTML = '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
            }
            setInterval(fetchStatus, 2000);
            fetchStatus();
        </script>
    </body>
    </html>
    """


def load_api_keys_from_env():
    key_pools = {}
    for bot_name in BOT_NAMES:
        env_var = "GROQ_KEYS_" + bot_name.upper().replace(" ", "_")
        keys_str = os.getenv(env_var)
        if not keys_str:
            print(f"ERROR: Thiếu biến môi trường {env_var}")
            sys.exit(1)
        keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        key_pools[bot_name] = GroqKeyPool(keys)
    return key_pools


async def main():
    key_pools = load_api_keys_from_env()

    bots: List[MinecraftBot] = []
    for name in BOT_NAMES:
        bot = MinecraftBot(name, key_pools[name], MINECRAFT_HOST, MINECRAFT_PORT, MINECRAFT_VERSION)
        bots.append(bot)
        bot_instances[name] = bot

    for bot in bots:
        asyncio.create_task(bot.start())
        asyncio.create_task(bot.decision_loop())
        asyncio.create_task(bot.message_processor())
        asyncio.create_task(bot.watchdog())
        await asyncio.sleep(4)

    print("Khởi động done!")
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        print("Đang dọn dẹp và dừng các bot...")
        for bot in bots:
            bot.stop_event.set()
            if bot.process and bot.process.returncode is None:
                try:
                    bot.process.terminate()
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Đang dừng...")
