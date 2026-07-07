# -*- coding: utf-8 -*-
"""验证网页语言切换：连 /ws → 发 set_language 日语 → 应收到 language 确认 + 日语确认语音。"""
import sys, json, time, threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import websocket

got = {"language": "", "audio": 0}
done = threading.Event()


def on_message(ws, message):
    if isinstance(message, bytes):
        got["audio"] += len(message)
        if got["language"] and got["audio"] > 20000:
            done.set()
        return
    m = json.loads(message)
    if m.get("type") == "language":
        got["language"] = m["value"]
        print("语言确认:", m["value"])
    elif m.get("type") == "log":
        print("  [log]", m["text"][:60])


def on_open(ws):
    def run():
        time.sleep(2)   # 等会话连上 OpenAI
        print("发送切换指令 → 日语")
        ws.send(json.dumps({"type": "set_language", "value": "日语"}))
    threading.Thread(target=run, daemon=True).start()


ws = websocket.WebSocketApp("ws://127.0.0.1:8000/ws", on_open=on_open, on_message=on_message)
threading.Thread(target=ws.run_forever, daemon=True).start()
ok = done.wait(timeout=30)
ws.close()

print("-" * 40)
print("收到语言确认 :", "✅" if got["language"] == "日语" else "❌")
print(f"收到确认语音 : {'✅' if got['audio'] > 20000 else '❌'} ({got['audio']} 字节)")
print(">>> 结果:", "语言切换通过 ✅" if ok else "未完成 ❌")
