import asyncio
import json
import os
import sys
from typing import Dict, List
from dotenv import load_dotenv

from groq import Groq, RateLimitError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

load_dotenv()

# ========== CẤU HÌNH CHUNG ==========
BOT_NAMES = ["BotAlpha", "BotBeta", "BotGamma"]
MINECRAFT_HOST = "dynamic-8.magmanode.com"
MINECRAFT_PORT = 25788
MINECRAFT_VERSION = "1.21.11"

# ========== LỚP KEY POOL ==========
class GroqKeyPool:
    def __init__(self, keys: List[str]):
        if not keys:
            raise ValueError("Cần ít nhất một API key")
        self.keys = list(keys)
        self.current_index = 0
        self.lock = asyncio.Lock()

    async def get_client(self) -> Groq:
        async with self.lock:
            start = self.current_index
            while True:
                key = self.keys[self.current_index]
                try:
                    return Groq(api_key=key)
                except Exception:
                    self.current_index = (self.current_index + 1) % len(self.keys)
                    if self.current_index == start:
                        raise RuntimeError("Tất cả key đều không hợp lệ")

    async def report_rate_limit(self):
        async with self.lock:
            self.current_index = (self.current_index + 1) % len(self.keys)
            print(f"Rate limit hit, chuyển sang key index {self.current_index}")

# ========== LỚP MINECRAFT BOT ==========
class MinecraftBot:
    def __init__(self, name: str, key_pool: GroqKeyPool, host: str, port: int, version: str):
        self.name = name
        self.key_pool = key_pool
        self.host = host
        self.port = port
        self.version = version
        self.process = None
        self.state = {
            "position": (0, 0, 0),
            "health": 20.0,
            "time": "day",
            "entities": [],
            "messages": [],
            "last_action": None
        }
        self.message_queue = asyncio.Queue()
        self.new_message_event = asyncio.Event()
        self.current_client = None
        self.stop_event = asyncio.Event()  # dùng để dừng hẳn nếu cần

    async def start(self):
        # Vòng lặp giám sát: nếu process chết thì khởi động lại
        while not self.stop_event.is_set():
            try:
                self.process = await asyncio.create_subprocess_exec(
                    "node", "mineflayer_bot.js",
                    self.name, self.host, str(self.port), self.version,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                # Tạo task đọc stdout, chạy đến khi process kết thúc
                await self.read_stdout_until_close()
                # Nếu process kết thúc mà không do stop_event, sẽ chờ 5s rồi khởi động lại
                if not self.stop_event.is_set():
                    print(f"[{self.name}] Process node bị dừng, khởi động lại sau 5 giây...")
                    await asyncio.sleep(5)
            except Exception as e:
                print(f"[{self.name}] Lỗi khi khởi động process: {e}")
                await asyncio.sleep(5)

    async def read_stdout_until_close(self):
        """Đọc stdout cho đến khi process kết thúc."""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode().strip())
                    if data.get("event") == "status":
                        self.state["position"] = tuple(data.get("position", (0, 0, 0)))
                        self.state["health"] = data.get("health", 20)
                        self.state["entities"] = data.get("entities", [])
                        self.state["time"] = data.get("time", "day")
                except Exception as e:
                    print(f"[{self.name}] Lỗi parse stdout: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            # Dọn dẹp khi process kết thúc
            if self.process and self.process.returncode is None:
                self.process.terminate()

    async def message_processor(self):
        """Nhận tin nhắn từ queue và kích hoạt phản hồi tức thì"""
        while not self.stop_event.is_set():
            try:
                msg = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                self.state["messages"].append(msg)
                if len(self.state["messages"]) > 10:
                    self.state["messages"].pop(0)
                print(f"[{self.name}] Nhận tin nhắn từ {msg['from']}: {msg['text']}")
                self.new_message_event.set()
            except asyncio.TimeoutError:
                pass

    def build_prompt(self):
        pos = self.state["position"]
        health = self.state["health"]
        time = self.state["time"]
        entities_desc = "\n".join([
            f"- {e.get('name','entity')} ({e.get('type','')}) ở {e.get('position')}, cách {e.get('distance','?')}m"
            for e in self.state["entities"][:10]
        ])
        msgs = "\n".join([
            f"{m['from']}: {m['text']}" for m in self.state["messages"]
        ])
        return f"""Bạn là bot Minecraft tên {self.name}, thành viên của đội gồm BotAlpha, BotBeta, BotGamma.
Đội của bạn đoàn kết tuyệt đối: luôn hỗ trợ, bảo vệ lẫn nhau, chia sẻ tài nguyên, cùng xây dựng căn cứ chung.
Mục tiêu chung: sinh tồn, thu thập tài nguyên, xây nhà tập thể, tiêu diệt quái vật và bảo vệ đồng đội.
Khi nhận được tin nhắn từ đồng đội, hãy phản hồi phù hợp. Nếu đồng đội cần giúp đỡ, ưu tiên trả lời và hỗ trợ.

Tình huống hiện tại:
- Vị trí: {pos}
- Máu: {health}
- Thời gian: {time}
- Thực thể gần nhất:
{entities_desc if entities_desc else "Không có"}
- Tin nhắn từ đồng đội:
{msgs if msgs else "Không có"}

Hãy chọn hành động tiếp theo. Trả về JSON với khóa "actions" chứa danh sách hành động. Mỗi hành động có "type" và tham số:
- "moveTo": {{"x": số, "y": số, "z": số}}
- "attack": {{"target": "nearest_hostile"}}
- "mineBlock": {{"x": số, "y": số, "z": số}}
- "placeBlock": {{"x": số, "y": số, "z": số, "blockType": "dirt"}}
- "sendMessageToBot": {{"botName": "BotAlpha", "message": "nội dung"}} (để trò chuyện nội bộ)
- "wait": {{"seconds": số}}

Chỉ trả về JSON, không kèm markdown. Ví dụ: {{"actions": [{{"type": "moveTo", "x": 100, "y": 64, "z": 100}}]}}"""

    async def call_groq_with_retry(self, prompt: str, max_retries=None):
        if max_retries is None:
            max_retries = len(self.key_pool.keys) * 2
        retries = 0
        while retries < max_retries:
            try:
                client = await self.key_pool.get_client()
                self.current_client = client
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": "Bạn là trợ lý chỉ xuất JSON hợp lệ."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )
                return response.choices[0].message.content
            except RateLimitError as e:
                print(f"[{self.name}] Rate limit, chuyển key... ({retries+1}/{max_retries})")
                await self.key_pool.report_rate_limit()
                retries += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[{self.name}] Lỗi Groq: {e}")
                retries += 1
                await asyncio.sleep(2)
        raise RuntimeError(f"[{self.name}] Hết số lần thử gọi Groq API")

    async def make_decision(self):
        """Đưa ra quyết định dựa trên trạng thái hiện tại và tin nhắn"""
        try:
            prompt = self.build_prompt()
            content = await self.call_groq_with_retry(prompt)
            print(f"[{self.name}] AI phản hồi: {content}")
            data = json.loads(content)
            actions = data.get("actions", [])
            self.state["last_action"] = actions
            for action in actions:
                await self.execute_action(action)
        except Exception as e:
            print(f"[{self.name}] Lỗi quyết định: {e}")

    async def decision_loop(self):
        await asyncio.sleep(5)  # chờ kết nối
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.new_message_event.wait(), timeout=5.0)
                print(f"[{self.name}] Phản hồi tin nhắn mới...")
                self.new_message_event.clear()
                await self.make_decision()
            except asyncio.TimeoutError:
                await self.make_decision()

    async def execute_action(self, action: dict):
        # Chỉ gửi lệnh nếu process đang sống
        if not self.process or self.process.returncode is not None:
            return
        action_type = action.get("type")
        if action_type == "moveTo":
            cmd = {"action": "moveTo", "x": action["x"], "y": action["y"], "z": action["z"]}
        elif action_type == "attack":
            cmd = {"action": "attack", "target": action.get("target", "nearest_hostile")}
        elif action_type == "mineBlock":
            cmd = {"action": "mineBlock", "x": action["x"], "y": action["y"], "z": action["z"]}
        elif action_type == "placeBlock":
            cmd = {
                "action": "placeBlock",
                "x": action["x"], "y": action["y"], "z": action["z"],
                "blockType": action.get("blockType", "dirt")
            }
        elif action_type == "sendMessageToBot":
            target = action.get("botName")
            message = action.get("message")
            if target and message:
                await self.send_bot_message(target, f"{self.name}: {message}")
            return
        elif action_type == "wait":
            return
        else:
            return
        try:
            self.process.stdin.write((json.dumps(cmd) + "\n").encode())
            await self.process.stdin.drain()
        except Exception as e:
            print(f"[{self.name}] Lỗi gửi lệnh: {e}")

    async def send_bot_message(self, target: str, text: str):
        # Override khi khởi tạo các bot
        pass

    async def shutdown(self):
        self.stop_event.set()
        if self.process and self.process.returncode is None:
            self.process.terminate()

# ========== WEB DASHBOARD ==========
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
            "time": bot.state["time"],
            "entities": bot.state["entities"][:5],
            "messages": bot.state["messages"][-5:],
            "last_action": bot.state.get("last_action"),
            "process_alive": bot.process and bot.process.returncode is None
        }
    return status

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Minecraft Bot Dashboard</title>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial; background: #1e1e1e; color: #ddd; margin: 20px; }
            .bot-card { background: #2d2d2d; padding: 15px; margin: 10px 0; border-radius: 8px; }
            h2 { margin: 0; color: #4CAF50; }
            pre { background: #111; padding: 10px; border-radius: 5px; overflow-x: auto; }
            .messages { max-height: 100px; overflow-y: auto; }
            .alive { color: #0f0; }
            .dead { color: #f00; }
        </style>
    </head>
    <body>
        <h1>Bot Dashboard</h1>
        <div id="bots"></div>
        <script>
            async function fetchStatus() {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                const container = document.getElementById('bots');
                container.innerHTML = '';
                for (const [name, info] of Object.entries(data)) {
                    const card = document.createElement('div');
                    card.className = 'bot-card';
                    const aliveStatus = info.process_alive ? '<span class="alive">● online</span>' : '<span class="dead">● offline</span>';
                    card.innerHTML = `
                        <h2>${name} ${aliveStatus}</h2>
                        <p><strong>Vị trí:</strong> ${info.position.join(', ')} | <strong>Máu:</strong> ${info.health}</p>
                        <p><strong>Thời gian:</strong> ${info.time} | <strong>Thực thể gần:</strong> ${info.entities.length}</p>
                        <div class="messages"><strong>Tin nhắn nội bộ:</strong>
                            ${info.messages.map(m => `<br>${m.from}: ${m.text}`).join('') || 'Không có'}
                        </div>
                        <details><summary>Hành động cuối</summary><pre>${JSON.stringify(info.last_action, null, 2)}</pre></details>
                    `;
                    container.appendChild(card);
                }
            }
            setInterval(fetchStatus, 2000);
            fetchStatus();
        </script>
    </body>
    </html>
    """
    return html

# ========== KHỞI TẠO HỆ THỐNG ==========
def load_api_keys_from_env():
    key_pools = {}
    for bot_name in BOT_NAMES:
        env_var = "GROQ_KEYS_" + bot_name.upper().replace(" ", "_")
        keys_str = os.getenv(env_var)
        if not keys_str:
            print(f"ERROR: Biến môi trường {env_var} không được thiết lập.")
            sys.exit(1)
        keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        if not keys:
            print(f"ERROR: Không có key nào trong {env_var}")
            sys.exit(1)
        key_pools[bot_name] = GroqKeyPool(keys)
        print(f"Đã tải {len(keys)} key cho {bot_name}")
    return key_pools

async def main():
    key_pools = load_api_keys_from_env()

    bots = []
    for name in BOT_NAMES:
        bot = MinecraftBot(name, key_pools[name], MINECRAFT_HOST, MINECRAFT_PORT, MINECRAFT_VERSION)
        bots.append(bot)
        bot_instances[name] = bot

    # Liên kết kênh tin nhắn nội bộ
    for sender_bot in bots:
        def make_sender(bot_name):
            async def send_message(target: str, text: str):
                if target in bot_instances:
                    await bot_instances[target].message_queue.put({"from": bot_name, "text": text})
            return send_message
        sender_bot.send_bot_message = make_sender(sender_bot.name)

    # Khởi động các bot (tự giám sát và reconnect)
    bot_tasks = []
    for bot in bots:
        # Chạy vòng lặp giám sát (sẽ tự restart process)
        bot_tasks.append(asyncio.create_task(bot.start()))
        # Chạy vòng lặp quyết định và xử lý tin nhắn
        bot_tasks.append(asyncio.create_task(bot.decision_loop()))
        bot_tasks.append(asyncio.create_task(bot.message_processor()))

    print("Tất cả bot đã chạy với cơ chế tự phục hồi.")

    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Đang dừng...")
