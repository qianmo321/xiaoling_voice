# -*- coding: utf-8 -*-
"""
网页版语音服务后端。
  浏览器 ←WebSocket(音频+JSON)→ 本服务 ←→ OpenAI Realtime
启动：
  python -m uvicorn server:app --host 0.0.0.0 --port 8000
本机测试直接访问 http://localhost:8000（localhost 免 HTTPS 也能用麦克风）。
"""
import os
import json
import queue
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from session import DialogSession

_HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.normpath(os.path.join(_HERE, "..", "config.json")), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_HERE, "static", "index.html"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    outbound = queue.Queue()   # 会话线程 → 浏览器（线程安全中转）

    session = DialogSession(
        CONFIG,
        send_json=lambda obj: outbound.put(("json", obj)),
        send_audio=lambda pcm: outbound.put(("audio", pcm)),
    )
    session.start()

    loop = asyncio.get_running_loop()

    async def sender():
        while True:
            kind, payload = await loop.run_in_executor(None, outbound.get)
            if kind == "close":
                break
            if kind == "audio":
                await ws.send_bytes(payload)
            else:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                session.feed_audio(msg["bytes"])          # 浏览器麦克风音频
            elif msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                if data.get("type") == "playback_done":
                    session.notify_playback_done()
                elif data.get("type") == "text_input":
                    session.feed_text(data.get("text", ""))
                elif data.get("type") == "set_language":
                    session.set_language(data.get("value", ""))
    except WebSocketDisconnect:
        pass
    finally:
        session.close()
        outbound.put(("close", None))
        try:
            await sender_task
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    srv = CONFIG.get("server", {})
    uvicorn.run(app, host=srv.get("host", "0.0.0.0"), port=int(srv.get("port", 8000)))
