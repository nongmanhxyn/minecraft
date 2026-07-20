import asyncio
import json
import os
import sys
from typing import Dict, List
from dotenv import load_dotenv

from groq import AsyncGroq, RateLimitError
from fastapi import FastAPI
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

    async def get_client(self) -> AsyncGroq:
        async with self.lock:
            key = self.keys[self.current_index]
            return AsyncGroq(api_key=key)

    async def report_rate_limit(self):
        async with self.lock:
            self.current_index = (self.current_index + 1) % len(self.keys)
            print(f"[KeyPool] Rate limit hit, chuyển sang key index {self.current_index}")

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
            "food": 20.0,
            "inventory": [],
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
                print(f"[{self.name}] Đang kết nối tới server qua Node.js...")
                self.process = await asyncio.create_subprocess_exec(
                    "node", "mineflayer_bot.js",
                    self.name, self.host, str(self.port), self.version,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                asyncio.create_task(self.read_stderr())
                await self.read_stdout_until_close()
                
                if not self.stop_event.is_set():
                    print(f"[{self.name}] Process bị ngắt, thử kết nối lại sau 10 giây...")
                    await asyncio.sleep(10)
            except Exception as e:
                print(f"[{self.name}] Lỗi process: {e}")
                await asyncio.sleep(10)

    async def read_stderr(self):
        try:
            while self.process and self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break
                print(f"[{self.name} Node]: {line.decode().strip()}")
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
                        self.state["food"] = data.get("food", 20)
                        self.state["inventory"] = data.get("inventory", [])
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
                print(f"💬 [CHAT NỘI BỘ] {msg['from']} ➔ {self.name}: \"{msg['text']}\"")
                self.new_message_event.set()
            except asyncio.TimeoutError:
                pass

    def build_prompt(self):
        pos = self.state["position"]
        health = self.state["health"]
        food = self.state["food"]
        time = self.state["time"]
        inv_desc = ", ".join([f"{i['name']} (x{i['count']})" for i in self.state["inventory"]])
        entities_desc = "\n".join([
            f"- {e.get('name','entity')} ({e.get('type','')}) ở {e.get('position')}, cách {e.get('distance','?')}m"
            for e in self.state["entities"][:10]
        ])
        msgs = "\n".join([f"{m['from']}: {m['text']}" for m in self.state["messages"]])
        
        other_bots = [b for b in BOT_NAMES if b != self.name]

        return f"""Bạn là bot Minecraft tên {self.name}, đồng đội cùng nhóm với {', '.join(other_bots)}.
Nhóm bạn sinh tồn cùng nhau, trao đổi thông tin, thu thập tài nguyên và bảo vệ lẫn nhau.

Tình huống hiện tại:
- Vị trí: {pos}
- Máu: {health}/20 | Độ đói: {food}/20
- Túi đồ: {inv_desc if inv_desc else "Trống"}
- Thời gian trong game: {time}
- Thực thể xung quanh:
{entities_desc if entities_desc else "Không có"}
- Tin nhắn nội bộ gần đây từ đồng đội:
{msgs if msgs else "Chưa có tin nhắn mới"}

Hãy đưa ra danh sách các hành động tiếp theo dạng JSON duy nhất.

Danh sách các action khả thi:
1. Trò chuyện nội bộ: {{"type": "sendMessageToBot", "botName": "{other_bots[0]}", "message": "Nội dung"}}
2. Di chuyển: {{"type": "moveTo", "x": {pos[0]+1}, "y": {pos[1]}, "z": {pos[2]+1}}}
3. Đánh nhau / Tấn công: {{"type": "attack", "targetName": "zombie"}} (hoặc bỏ targetName để đánh mob gần nhất)
4. Đào block: {{"type": "mineBlock", "x": {pos[0]}, "y": {pos[1]-1}, "z": {pos[2]}}}
5. Đặt block: {{"type": "placeBlock", "x": {pos[0]}, "y": {pos[1]-1}, "z": {pos[2]}, "itemName": "cobblestone"}}
6. Ăn uống (khi đói/mất máu): {{"type": "eat"}}
7. Nhặt đồ văng quanh đây: {{"type": "collectItem"}}
8. Tương tác (mở rương/cửa/nói chuyện NPC): {{"type": "interact", "x": {pos[0]}, "y": {pos[1]}, "z": {pos[2]}}}

Ví dụ JSON trả về:
{{
  "actions": [
    {{"type": "sendMessageToBot", "botName": "{other_bots[0]}", "message": "Tui đi nhặt đồ với đập cây nè"}},
    {{"type": "collectItem"}}
  ]
}}"""

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
                        {"role": "system", "content": "Bạn là AI bot Minecraft, chỉ xuất duy nhất định dạng JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    max_tokens=400,
                    response_format={"type": "json_object"}
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
            data = json.loads(content)
            actions = data.get("actions", [])
            self.state["last_action"] = actions
            for action in actions:
                await self.execute_action(action)
        except Exception as e:
            print(f"[{self.name}] Lỗi suy nghĩ decision: {e}")

    async def decision_loop(self):
        await asyncio.sleep(10)
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.new_message_event.wait(), timeout=8.0)
                self.new_message_event.clear()
                await self.make_decision()
            except asyncio.TimeoutError:
                await self.make_decision()

    async def execute_action(self, action: dict):
        action_type = action.get("type")
        
        if action_type == "sendMessageToBot":
            target = action.get("botName")
            message = action.get("message")
            if target and message:
                await self.send_bot_message(target, message)
            return

        if not self.process or self.process.returncode is not None:
            return

        cmd = None
        if action_type == "moveTo":
            cmd = {"action": "moveTo", "x": action["x"], "y": action["y"], "z": action["z"]}
        elif action_type == "attack":
            cmd = {"action": "attack", "targetName": action.get("targetName")}
        elif action_type == "mineBlock":
            cmd = {"action": "mineBlock", "x": action["x"], "y": action["y"], "z": action["z"]}
        elif action_type == "placeBlock":
            cmd = {"action": "placeBlock", "x": action["x"], "y": action["y"], "z": action["z"], "itemName": action.get("itemName")}
        elif action_type == "eat":
            cmd = {"action": "eat"}
        elif action_type == "collectItem":
            cmd = {"action": "collectItem"}
        elif action_type == "interact":
            cmd = {"action": "interact", "x": action.get("x"), "y": action.get("y"), "z": action.get("z"), "entityName": action.get("entityName")}

        if cmd:
            try:
                self.process.stdin.write((json.dumps(cmd) + "\n").encode())
                await self.process.stdin.drain()
            except Exception as e:
                print(f"[{self.name}] Lỗi gửi lệnh Node: {e}")

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
            "food": bot.state["food"],
            "inventory": bot.state["inventory"],
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

    for bot in bots:
        asyncio.create_task(bot.start())
        asyncio.create_task(bot.decision_loop())
        asyncio.create_task(bot.message_processor())
        await asyncio.sleep(4)

    print("Tất cả bot đã khởi động xong!")

    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Đang dừng...")
