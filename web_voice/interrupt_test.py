# -*- coding: utf-8 -*-
"""网页版打断门槛的离线测试（不联网）：说话中普通话不打断、唤醒词才打断、说完后正常交互。"""
import sys, time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from session import DialogSession

CFG = {
    "openai": {"api_key": "test"},
    "search": {"enable": False, "tavily_api_key": ""},
    "network": {"proxy": ""},
    "language": "中文",
    "wake": {"enable": True, "window_s": 30, "standby_announce": False,
             "interrupt_requires_wake": True},
    "mic_gate_rms": 0,
}

sent = []       # 记录发往 OpenAI 的消息类型
to_browser = [] # 记录发往浏览器的 JSON

s = DialogSession(CFG, send_json=lambda o: to_browser.append(o.get("type")),
                  send_audio=lambda b: None)
s._send = lambda obj: sent.append(obj.get("type"))   # 不真连 OpenAI，只记录

results = []
def check(desc, cond):
    results.append((desc, cond))
    print(("✅ " if cond else "❌ ") + desc)

# 先唤醒进入对话态
s._awake = True
s._last_active_ts = time.time()

# 1) 它说话中(生成中)，普通话 → 无视+删历史
s._is_responding = True
sent.clear()
s._handle_utterance("换个话题吧", "it1")
check("说话中·普通话被无视", "response.create" not in sent and "conversation.item.delete" in sent)

# 2) 它说话中，喊唤醒词 → 打断+回应
sent.clear()
s._handle_utterance("你好小灵，换个话题", "it2")
check("说话中·唤醒词能打断", "response.cancel" in sent and "response.create" in sent)

# 3) 生成完但浏览器还在播 → 普通话也无视
s._is_responding = False
s._playing = True
sent.clear()
s._handle_utterance("等一下等一下", "it3")
check("播放中·普通话被无视", "response.create" not in sent)

# 4) 说完了(空闲) → 不需要唤醒词，正常回应
s._playing = False
sent.clear()
s._handle_utterance("再介绍一下大连百易", "it4")
check("空闲期·正常回应(无需唤醒词)", "response.create" in sent)

# 5) 开关关掉 → 说话中普通话也能打断(旧行为)
s.interrupt_requires_wake = False
s._is_responding = True
sent.clear()
s._handle_utterance("换个话题吧", "it5")
check("开关=False·恢复随时可打断", "response.create" in sent)

print("-" * 40)
fails = [d for d, c in results if not c]
print(f">>> {len(results)-len(fails)}/{len(results)} 通过", "✅" if not fails else f"❌ {fails}")
