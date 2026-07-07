# -*- coding: utf-8 -*-
"""
只测“能不能听到模型声音”（不开麦克风）。
连接 -> 让模型回一句 -> 收集音频 -> 用扬声器/耳机播放出来。
如果你能听到，说明播放没问题，问题在麦克风/打断逻辑；听不到则是输出设备问题。
"""
import os, sys, json, base64, threading, importlib.util
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import sounddevice as sd
import websocket

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ort", os.path.join(_here, "openai_realtime_test.py"))
ort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ort)

URL = f"wss://api.openai.com/v1/realtime?model=gpt-realtime"
HEADERS = [f"Authorization: Bearer {ort.API_KEY}"]

audio = bytearray()
done = threading.Event()


def proxy_kwargs():
    p = ort.PROXY or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not p:
        return {}
    u = urlparse(p if "://" in p else "http://" + p)
    return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}


def on_open(ws):
    ws.send(json.dumps({"type": "session.update", "session": {
        "type": "realtime", "output_modalities": ["audio"],
        "instructions": "你是小灵，用中文热情地说一段30个字左右的自我介绍。",
        "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}},
    }}))
    ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "请做个自我介绍"}]}}))
    ws.send(json.dumps({"type": "response.create"}))


def on_message(ws, message):
    e = json.loads(message)
    t = e.get("type", "")
    if t in ("response.output_audio.delta", "response.audio.delta"):
        audio.extend(base64.b64decode(e["delta"]))
    elif t == "response.done":
        done.set()
    elif t == "error":
        print("[错误]", json.dumps(e.get("error", {}), ensure_ascii=False))
        done.set()


def main():
    print("默认输出设备:", sd.query_devices(kind="output")["name"])
    ws = websocket.WebSocketApp(URL, header=HEADERS, on_open=on_open, on_message=on_message)
    threading.Thread(target=lambda: ws.run_forever(**proxy_kwargs()), daemon=True).start()

    if not done.wait(timeout=30):
        print("拿音频超时（网络问题？）"); ws.close(); return
    ws.close()

    secs = len(audio) / 2 / 24000
    print(f"收到音频: {len(audio)} 字节, 约 {secs:.1f} 秒")
    if not audio:
        print("没收到音频，先别管播放。"); return

    print(">>> 开始播放（5 秒内注意听）...")
    stream = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16")
    stream.start()
    stream.write(bytes(audio))
    stream.stop(); stream.close()
    print(">>> 播放结束。听到了吗？")


if __name__ == "__main__":
    main()
