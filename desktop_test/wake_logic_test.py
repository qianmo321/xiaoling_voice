# -*- coding: utf-8 -*-
"""唤醒状态机 + 过滤规则的离线测试（不联网、不用麦克风）。"""
import os, sys, json, importlib.util

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ort", os.path.join(_here, "openai_realtime_test.py"))
ort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ort)


class FakeWS:
    def __init__(self):
        self.sent = []
        self.raw = []
    def send(self, payload):
        self.raw.append(payload)
        self.sent.append(json.loads(payload).get("type"))


results = []


def check(desc, cond):
    results.append((desc, cond))
    print(("✅ " if cond else "❌ ") + desc)


# ---------- is_filler ----------
check("语气词'嗯嗯'被忽略", ort.is_filler("嗯嗯"))
check("单字'好'被忽略(太短)", ort.is_filler("好"))
check("幻觉'谢谢观看'被忽略", ort.is_filler("谢谢观看。"))
check("幻觉'Thank you for watching'被忽略", ort.is_filler("Thank you for watching!"))
check("正常问题不被忽略", not ort.is_filler("商场几点关门"))
check("'小灵小灵'不算语气词", not ort.is_filler("小灵小灵"))

# ---------- 唤醒状态机 ----------
ws = FakeWS()
ort._ws_app = ws
ort._awake = False
ort._last_active_ts = 0.0

# 1) 待机时说无关话 → 忽略,不发任何请求
ort.handle_user_utterance("今天中午吃什么？")
check("待机时无关话被忽略", not ort._awake and ws.sent == [])

# 2) 待机时喊唤醒词 → 唤醒并回应
ort.handle_user_utterance("小灵小灵")
check("唤醒词触发唤醒", ort._awake)
check("唤醒后发出了回应请求", "response.create" in ws.sent)

# 3) 激活态内正常对话 → 回应
ws.sent.clear()
ort.handle_user_utterance("商场几点关门")
check("激活态内正常对话被回应", "response.create" in ws.sent)

# 4) 激活态内语气词 → 忽略
ws.sent.clear()
ort.handle_user_utterance("嗯…")
check("激活态内语气词被忽略", ws.sent == [])

# 5) 超过激活窗口 → 自动回待机,无唤醒词的话被忽略
ws.sent.clear()
ort._last_active_ts -= (ort.WAKE_WINDOW_S + 5)   # 模拟已闲置超时
ort.handle_user_utterance("这个周末去哪玩")
check("超时后自动回待机并忽略无唤醒词的话", (not ort._awake) and ws.sent == [])

# 6) 回待机后再喊"小玲"(同音误识别) → 也能唤醒
ws.sent.clear()
ort.handle_user_utterance("小玲，介绍一下IST")
check("同音'小玲'也能唤醒", ort._awake and "response.create" in ws.sent)

# 7) 各种同音/变体唤醒词逐个验证（每次先强制回待机）
for phrase, desc in [
    ("小凌小凌", "同音'小凌'"),
    ("小 灵 小 灵", "带空格'小 灵'"),
    ("你好", "打招呼'你好'"),
    ("小林在吗", "近音'小林'"),
    ("シャオリン、こんにちは", "日语'シャオリン'"),
    ("しゃおりん", "平假名'しゃおりん'"),
    ("こんにちは。", "日语'こんにちは'"),
    ("こんにちわ", "错写'こんにちわ'"),
    ("すみません、ちょっといいですか", "日语'すみません'"),
    ("シャーリン", "近似音'シャーリン'"),
    ("ハロー", "日语'ハロー'"),
]:
    ort._awake = False
    ws.sent.clear()
    ort.handle_user_utterance(phrase)
    check(f"{desc} 能唤醒", ort._awake)

# 8) 待机时不含任何唤醒词的话依然被忽略
ort._awake = False
ws.sent.clear()
ort.handle_user_utterance("我们去吃午饭吧")
check("扩充词表后，无关话仍被忽略", not ort._awake and ws.sent == [])

# 9) 口头待机指令：do_enter_standby 立即置待机
ort._awake = True
ws.sent.clear()
msg = ort.do_enter_standby()
check("口头指令进入待机", not ort._awake and "待机" in msg)
ort.handle_user_utterance("那明天见啦")
check("口头待机后，无唤醒词的话被忽略", ws.sent == [])

# 9.5) 打断门槛：它说话中，普通话不打断、唤醒词才打断
ort._awake = True
ort._last_active_ts = __import__("time").time()
ort._is_responding = True          # 模拟"正在生成/说话"
ws.sent.clear()
ort.handle_user_utterance("换个话题吧", "item_talk1")
check("说话中·普通话被无视(不打断)", "response.create" not in ws.sent and "response.cancel" not in ws.sent)
check("说话中·被无视的话已从历史删除", "conversation.item.delete" in ws.sent)
ws.sent.clear()
ort.handle_user_utterance("你好小灵，换个话题", "item_talk2")
check("说话中·喊唤醒词能打断", "response.cancel" in ws.sent and "response.create" in ws.sent)
ort._is_responding = False
ort._playing = True                # 模拟"生成完但还在播"
ws.sent.clear()
ort.handle_user_utterance("等一下等一下", "item_talk3")
check("播放中·普通话也被无视", "response.create" not in ws.sent)
ort._playing = False
ws.sent.clear()
ort.handle_user_utterance("再介绍一下大连百易")
check("说完后·空闲期不需要唤醒词，正常回应", "response.create" in ws.sent)

# 10) 待机播报：发送了带外（conversation:none）的回应请求
ws.sent.clear(); ws.raw.clear()
ort._announce_standby()
check("待机播报发送了回应请求", "response.create" in ws.sent)
check("待机播报是带外响应(不读/不写历史)", any('"conversation": "none"' in r for r in ws.raw))

# 11) 忽略的话要从服务端历史删除（传 item_id 时发 conversation.item.delete）
ort._awake = False
ws.sent.clear()
ort.handle_user_utterance("路人甲和路人乙的闲聊内容啊", "item_abc123")
check("待机忽略时把该条从历史删除", "conversation.item.delete" in ws.sent and "response.create" not in ws.sent)
ort._awake = True
ort._last_active_ts = __import__("time").time()
ws.sent.clear()
ort.handle_user_utterance("嗯嗯。", "item_def456")
check("语气词忽略时也从历史删除", "conversation.item.delete" in ws.sent and "response.create" not in ws.sent)

print("-" * 40)
fails = [d for d, c in results if not c]
print(f">>> {len(results) - len(fails)}/{len(results)} 通过", "✅ 全部通过" if not fails else f"❌ 失败: {fails}")
