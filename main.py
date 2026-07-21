import asyncio
import itertools
import json
import os
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

# Model Groq dùng cho quyết định của bot. llama-3.1-8b-instant / llama-3.3-70b-versatile
# sẽ ngừng hoạt động 16/08/2026 (Groq đã thông báo deprecation), nên dùng model thay thế
# openai/gpt-oss-20b (tier "instant" mới, nhanh & rẻ, Groq khuyến nghị thay cho 8b-instant).
GROQ_MODEL = "openai/gpt-oss-20b"

# --- DSL lệnh nhỏ gọn thay cho JSON, để giảm tối đa token output của Groq -----
# Mỗi dòng AI trả về là 1 lệnh, dạng: TỪ_KHOÁ tham_số...
DSL_COMMANDS = {"CHAT", "MOVE", "MINE", "PLACE", "EQUIP", "ATTACK", "NOOP"}


def parse_dsl_line(line: str) -> Optional[dict]:
    """Parse 1 dòng lệnh DSL từ Groq thành dict action tương thích với execute_action().
    Trả None nếu dòng không phải lệnh hợp lệ (rác/giải thích thừa -> bỏ qua an toàn,
    không làm hỏng các lệnh khác trong cùng phản hồi)."""
    line = line.strip().strip("`")
    if not line:
        return None

    parts = line.split(maxsplit=1)
    keyword = parts[0].upper()
    if keyword not in DSL_COMMANDS:
        return None
    rest = parts[1] if len(parts) > 1 else ""

    if keyword == "NOOP":
        return None

    if keyword == "CHAT":
        sub = rest.split(maxsplit=1)
        if len(sub) < 2:
            print(f"Bỏ qua dòng CHAT thiếu nội dung: {line!r}")
            return None
        return {"type": "sendMessageToBot", "botName": sub[0], "message": sub[1]}

    if keyword == "MOVE":
        toks = rest.split()
        if len(toks) != 3:
            print(f"Bỏ qua dòng MOVE sai định dạng (cần 3 toạ độ): {line!r}")
            return None
        return {"type": "moveTo", "x": toks[0], "y": toks[1], "z": toks[2]}

    if keyword == "MINE":
        toks = rest.split()
        if len(toks) != 3:
            print(f"Bỏ qua dòng MINE sai định dạng (cần 3 toạ độ): {line!r}")
            return None
        return {"type": "mineBlock", "x": toks[0], "y": toks[1], "z": toks[2]}

    if keyword == "PLACE":
        toks = rest.split(maxsplit=3)
        if len(toks) != 4:
            print(f"Bỏ qua dòng PLACE sai định dạng (cần 3 toạ độ + tên block): {line!r}")
            return None
        return {"type": "placeBlock", "x": toks[0], "y": toks[1], "z": toks[2], "blockName": toks[3]}

    if keyword == "EQUIP":
        toks = rest.split()
        if len(toks) < 1:
            print(f"Bỏ qua dòng EQUIP thiếu itemName: {line!r}")
            return None
        item_name = toks[0]
        destination = toks[1] if len(toks) > 1 else "hand"
        return {"type": "equipItem", "itemName": item_name, "destination": destination}

    if keyword == "ATTACK":
        return {"type": "attack", "target": "nearest_hostile"}

    return None


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

        inv_desc = ", ".join(f"{it['count']}x{it['name']}" for it in self.state["inventory"]) or "trong"

        near = "; ".join(
            f"{e.get('name')}@{e.get('distance')}m" for e in self.state["entities"][:8]
        ) or "khong co"

        msgs = " | ".join(f"{m['from']}:{m['text']}" for m in self.state["messages"][-5:]) or "khong co"

        other_bots = ",".join(b for b in BOT_NAMES if b != self.name)

        # Prompt cố tình viết không dấu ở phần hướng dẫn (không phải dữ liệu) để giảm token
        # và tránh model chép nhầm dấu câu vào bên trong lệnh.
        return (
            f"BOT {self.name} team:{other_bots}\n"
            f"POS {pos[0]} {pos[1]} {pos[2]} HP {health} TIME {time_of_day}\n"
            f"INV {inv_desc}\n"
            f"NEAR {near}\n"
            f"MSG {msgs}\n"
            "\n"
            f"Tra loi bang LENH, moi dong 1 lenh, toa do la SO NGUYEN, trong ban kinh {MAX_MOVE_DISTANCE} block:\n"
            "CHAT <bot> <noi dung>\n"
            "MOVE <x> <y> <z>\n"
            "MINE <x> <y> <z>\n"
            "PLACE <x> <y> <z> <block_en>\n"
            "EQUIP <item_en> <hand|head|torso|legs|feet>\n"
            "ATTACK\n"
            "NOOP\n"
            "Neu khong can lam gi thi tra dung 1 dong NOOP.\n"
            "CHI TRA CAC DONG LENH O TREN. KHONG giai thich. KHONG markdown. KHONG JSON."
        )

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
                    model=GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": "Ban la AI dieu khien bot Minecraft. Chi tra ve cac dong "
                                                       "lenh DSL (CHAT/MOVE/MINE/PLACE/EQUIP/ATTACK/NOOP), "
                                                       "khong JSON, khong markdown, khong giai thich."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=300,  # DSL rất ngắn gọn so với JSON -> giảm mạnh token output & né rate limit
                    # Không dùng response_format=json_object nữa vì output giờ là text DSL, không phải JSON.
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

        actions = []
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("```"):
                continue  # bỏ qua dòng trống / model lỡ bọc code fence dù đã được dặn không làm vậy
            parsed = parse_dsl_line(stripped)
            if parsed:
                actions.append(parsed)

        self.state["last_action"] = actions
        for action in actions:
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
