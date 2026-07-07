# -*- coding: utf-8 -*-
"""
OpenAI Realtime 连通性自检（GA 正式版，不需要麦克风）
用文字触发模型回一句话，验证 key / 网络 / 模型权限 / 协议是否全通。
"""
import os, sys, json, threading, importlib.util
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import websocket


def proxy_kwargs():
    p = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
         or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if not p:
        return {}
    u = urlparse(p if "://" in p else "http://" + p)
    if not u.hostname:
        return {}
    return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}

# 复用同目录脚本里的 API_KEY / INSTRUCTIONS / VOICE
_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ort", os.path.join(_here, "openai_realtime_test.py"))
ort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ort)

# ---- GA 正式版：模型用 gpt-realtime，去掉 beta 头 ----
MODEL = "gpt-realtime"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
HEADERS = [f"Authorization: Bearer {ort.API_KEY}"]
VOICE = "marin"   # GA 旗舰音色；也可 cedar/alloy/coral...

state = {"connected": False, "created": False, "audio": False, "text": "", "error": None}
done = threading.Event()


def on_open(ws):
    state["connected"] = True
    # GA 会话格式：audio 嵌套 input/output，模态用 output_modalities
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": ort.INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": None,
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": VOICE,
                },
            },
        },
    }))
    ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "用一句话中文跟我打个招呼"}]},
    }))
    ws.send(json.dumps({"type": "response.create"}))


def on_message(ws, message):
    e = json.loads(message)
    t = e.get("type", "")
    if t in ("response.created",):
        state["created"] = True
    elif t in ("response.output_audio.delta", "response.audio.delta"):
        state["audio"] = True
    elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done",
               "response.output_text.done", "response.text.done"):
        state["text"] += e.get("transcript", e.get("text", "")) or ""
    elif t == "response.done":
        done.set()
    elif t == "error":
        state["error"] = e.get("error", {})
        done.set()


def on_error(ws, err):
    state["error"] = str(err)
    done.set()


def main():
    key = ort.API_KEY
    print("URL :", URL)
    print("模型:", MODEL, "| 音色:", VOICE)
    print("KEY :", (key[:8] + "..." + key[-4:]) if key and len(key) > 12 else "(空)")
    print("-" * 50)

    ws = websocket.WebSocketApp(URL, header=HEADERS,
                                on_open=on_open, on_message=on_message, on_error=on_error)
    pk = proxy_kwargs()
    if pk:
        print("代理:", f"{pk['http_proxy_host']}:{pk['http_proxy_port']}")
    th = threading.Thread(target=lambda: ws.run_forever(**pk), daemon=True)
    th.start()

    ok = done.wait(timeout=30)
    ws.close()

    print("连接成功     :", "✅" if state["connected"] else "❌")
    print("会话/响应创建:", "✅" if state["created"] else "❌")
    print("收到语音音频 :", "✅" if state["audio"] else "❌")
    print("模型回复文字 :", state["text"].strip() or "(无)")
    if state["error"]:
        print("错误         :", json.dumps(state["error"], ensure_ascii=False))
    print("-" * 50)
    if state["connected"] and state["created"] and state["audio"]:
        print(">>> 结果：全部通过 ✅  方向确认，可以放心做 openai_s2s 后端了")
    elif not ok:
        print(">>> 结果：超时 ❌  检查 key / 网络 / 模型权限")
    else:
        print(">>> 结果：未完全通过 ❌  看上面的错误信息")


if __name__ == "__main__":
    main()
