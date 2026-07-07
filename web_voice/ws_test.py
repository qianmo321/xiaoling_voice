# -*- coding: utf-8 -*-
"""模拟浏览器的全链路测试：连 /ws → 发一段含"小灵"的音频 → 验证唤醒+识别+回答+语音都回来。"""
import os, sys, json, time, wave, threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import websocket

WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_question.wav")

got = {"user_text": "", "bot_text": "", "audio_bytes": 0, "statuses": []}
done = threading.Event()


def on_message(ws, message):
    if isinstance(message, bytes):
        got["audio_bytes"] += len(message)
        return
    m = json.loads(message)
    t = m.get("type")
    if t == "user_text":
        got["user_text"] = m["text"]
        print("识别:", m["text"][:50])
    elif t == "bot_text":
        got["bot_text"] = m["text"]
        print("回答:", m["text"][:60])
        done.set()
    elif t == "status":
        got["statuses"].append(m["value"])
    elif t == "log":
        print("  [log]", m["text"][:70])


def on_open(ws):
    def run():
        time.sleep(2)  # 等会话连上 OpenAI
        wf = wave.open(WAV, "rb")
        pcm = wf.readframes(wf.getnframes())
        wf.close()
        pcm += b"\x00\x00" * 24000  # 补1秒静音帮 VAD 断句
        print(f"发送音频 {len(pcm)} 字节 ...")
        step = 4800  # 100ms
        for i in range(0, len(pcm), step):
            ws.send(pcm[i:i+step], opcode=websocket.ABNF.OPCODE_BINARY)
            time.sleep(0.1)
        print("音频发完，等回应 ...")
    threading.Thread(target=run, daemon=True).start()


ws = websocket.WebSocketApp("ws://127.0.0.1:8000/ws",
                            on_open=on_open, on_message=on_message)
threading.Thread(target=ws.run_forever, daemon=True).start()
ok = done.wait(timeout=60)
time.sleep(2)   # 再收一点尾部音频
ws.close()

print("-" * 50)
print("识别到文字 :", "✅" if got["user_text"] else "❌")
print("模型回答   :", "✅" if got["bot_text"] else "❌")
print(f"收到语音   : {'✅' if got['audio_bytes'] else '❌'} ({got['audio_bytes']} 字节)")
print("状态流转   :", " → ".join(got["statuses"][:8]) or "(无)")
verdict = got["user_text"] and got["bot_text"] and got["audio_bytes"] > 10000
print(">>> 结果:", "网页版全链路通过 ✅" if verdict else ("超时/未完成 ❌"))
