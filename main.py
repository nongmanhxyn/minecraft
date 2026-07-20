import asyncio
import json
import os
import sys
from typing import Dict, List
from dotenv import load_dotenv

from groq import AsyncGroq, RateLimitError  # Dùng AsyncGroq chuẩn bất đồng bộ
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

load_dotenv()

# ========== CẤU HÌNH CHUNG ==========
BOT_NAMES = ["BotAlpha", "BotBeta", "BotGamma"]
MINECRAFT_HOST = "dynamic-8.magmanode.com"
MINECRAFT_PORT = 25788
MINECRAFT_VERSION = "1.21.11"

# ========== LỚP KEY POOL (ASYNC) ==========
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
        self.stop_event = asyncio.Event()

    async def start(self):
        while not self.stop_event.is_set():
            try:
                print(f"[{self.name}] Đang khởi động process Node.js...")
                self.process = await asyncio.create_subprocess_exec(
                    "node", "mineflayer_bot.js",
                    self.name, self.host, str(self.port), self.version,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Task đọc stderr riêng để log lỗi Node ra console Python
                asyncio.create_task(self.read_stderr())
                
                await self.read_stdout_until_close()
                if not self.stop_event.is_set():
                    print(f"[{self.name}] Process node bị dừng, khởi động lại sau 5 giây...")
                    await asyncio.sleep(5)
            except Exception as e:
                print(f"[{self.name}] Lỗi khi khởi động process: {e}")
                await asyncio.sleep(5)

    async def read_stderr(self):
        """Đọc stderr từ Node.js để hiển thị log chuẩn mà không phá JSON"""
        try:
            while self.process and self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break
                print(f"[{self.name} Node Log]: {line.decode().strip()}")
        except Exception:
            pass

    async def read_stdout_until_close(self):
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
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            if self.process and self.process.returncode is None:
                self.process.terminate()

    async def message_processor(self):
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

Tình huống hiện tại:
- Vị trí: {pos}
- Máu: {health}
- Thời gian: {time}
- Thực thể gần nhất:
{entities_desc if entities_desc else "Không có"}
- Tin nhắn từ đồng đội:
{msgs if msgs else "Không có"}

Hãy chọn hành động tiếp theo. Trả về JSON với khóa "actions" chứa danh sách hành động.
Ví dụ: {{"actions": [{{"type": "moveTo", "x": 10, "y": 64, "z": 10}}]}}"""

    async def call_groq_with_retry(self, prompt: str, max_retries=None):
        if max_retries is None:
            max_retries = len(self.key_pool.keys) * 2
        retries = 0
        while retries < max_retries:
            try:
                client = await self.key_pool.get_client()
                # Dùng AWAIT cho AsyncGroq để không đơ event loop
                response = await client.chat.completions.create(
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
            except RateLimitError:
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
        await asyncio.sleep(8)  # Chờ 8s cho bot spawn hẳn vào game
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.new_message_event.wait(), timeout=8.0)
                self.new_message_event.clear()
                await self.make_decision()
            except asyncio.TimeoutError:
                await self.make_decision()

    async def execute_action(self, action: dict):
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
        else:
            return
        try:
            self.process.stdin.write((json.dumps(cmd) + "\n").encode())
            await self.process.stdin.drain()
        except Exception as e:
            print(f"[{self.name}] Lỗi gửi lệnh: {e}")

    async def send_bot_message(self, target: str, text: str):
        pass

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
            print(f"ERROR: Biến môi trường {env_var} không được thiết lập.")
            sys.exit(1)
        keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        key_pools[bot_name] = GroqKeyPool(keys)
    return key_pools

async def main():
    key_pools = load_api_keys_from_env()

    bots = []
    for name in BOT_NAMES:
        bot = MinecraftBot(name, key_pools[name], MINECRAFT_HOST, MINECRAFT_PORT, MINECRAFT_VERSION)
        bots.append(bot)
        bot_instances[name] = bot

    for sender_bot in bots:
        def make_sender(bot_name):
            async def send_message(target: str, text: str):
                if target in bot_instances:
                    await bot_instances[target].message_queue.put({"from": bot_name, "text": text})
            return send_message
        sender_bot.send_bot_message = make_sender(sender_bot.name)

    # Bật bot cách nhau 3 giây để Magmanode không kick vì spam login
    for bot in bots:
        asyncio.create_task(bot.start())
        asyncio.create_task(bot.decision_loop())
        asyncio.create_task(bot.message_processor())
        await asyncio.sleep(3) 

    print("Tất cả bot đã khởi động xong.")
    print(f"=== CHECK KEYS ===\nAlpha: {os.getenv('GROQ_KEYS_BOTALPHA')}\nBeta: {os.getenv('GROQ_KEYS_BOTBETA')}\nGamma: {os.getenv('GROQ_KEYS_BOTGAMMA')}\n==================")
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Đang dừng...")
