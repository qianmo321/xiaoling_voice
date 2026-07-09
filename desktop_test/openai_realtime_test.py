# -*- coding: utf-8 -*-
"""
OpenAI Realtime API 最小验证脚本（端到端语音对话）
-----------------------------------------------------
作用：在你自己的 Windows 笔记本上，用麦克风对它说话，它用语音回你。
目的：验证“把通义千问 Omni 换成 OpenAI Realtime”这条路能跑通，不需要 ROS2 / Linux / 那个 C++ 项目。

依赖安装（在终端执行一次）：
    pip install websocket-client sounddevice

运行：
    1) 把下面的 API_KEY 填成你的 OpenAI 官方 key（或设置环境变量 OPENAI_API_KEY）
    2) 戴上耳机（重要！用外放喇叭会产生回声，它会把自己的声音当成你在说话）
    3) python openai_realtime_test.py
    4) 直接对着麦克风说话，停顿一下，它就会语音回复你
    5) Ctrl+C 退出
"""

import os
import sys
import re
import json
import time
import base64
import queue
import threading
from urllib.parse import urlparse

# Windows 控制台默认编码可能不是 UTF-8，强制成 UTF-8，避免打印中文时崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import sounddevice as sd
import websocket  # 来自 websocket-client 包
import requests   # 调 Tavily 搜索用


# ============ 配置区（你只需要改这里）============
# 密钥/代理从项目根 config.json 读取（该文件不入 git；模板见 config.example.json）
_ROOT_CFG = {}
try:
    with open(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")),
              encoding="utf-8") as _f:
        _ROOT_CFG = json.load(_f)
except Exception:
    pass
API_KEY = os.environ.get("OPENAI_API_KEY", _ROOT_CFG.get("openai", {}).get("api_key", ""))

# Tavily 联网检索 key（在 tavily.com 注册免费拿；把下面引号里换成你的 tvly- 开头的 key）
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", _ROOT_CFG.get("search", {}).get("tavily_api_key", ""))
ENABLE_SEARCH = True   # 是否开启联网检索
ENABLE_KB = True       # 是否开启本地知识库（先运行 python kb.py build 建索引）

# 模型：GA 正式版（beta 已停用）
MODEL = "gpt-realtime"

# 音色：marin / cedar / alloy / ash / ballad / coral / echo / sage / shimmer / verse
VOICE = "marin"

# === 语言配置：助手的主语言，可选 "中文" / "日语" ===
# 影响：人设提示词里的语言说明 + 语音转写的引导提示。
# 注意这是"主语言"不是"锁死"：用户用另一种语言提问时，识别和回答仍会跟随对方。
LANGUAGE = "中文"

# 语音转写模型：gpt-4o-mini-transcribe 比 whisper-1 幻觉少、短音频（喊唤醒词）识别更稳
TRANSCRIBE_MODEL = _ROOT_CFG.get("openai", {}).get("transcribe_model", "gpt-4o-transcribe")
# 转写引导提示（按 LANGUAGE 自动选）。
# 注意措辞要"弱引导"：只说明语言/领域、名字用"可能出现"带过。
# 若写成"名字叫小灵(你好小灵)"这种强引导，咳嗽等模糊人声会被转写器直接"脑补"成唤醒词→误唤醒。
_TRANSCRIBE_PROMPTS = {
    # 注意：中文版不写"可能夹杂日语"——写了的话，清嗓子等模糊人声会被"脑补"成日语套话（如こんにちは、ご案内いたします）
    "中文": "以下是中文普通话的导览问答对话，内容多与公司、商场、景点介绍有关。对话中可能出现名字『小灵』。",
    "日语": "以下は日本語の案内・質問応答の会話です。内容は会社・商業施設・観光地の案内が中心です。会話に『小灵（シャオリン）』という名前が出ることがあります。",
}

# 人设 / 系统提示词（无场景时的默认；语言跟随上面的 LANGUAGE 配置）
INSTRUCTIONS = (f"你是一个友好、活泼的{LANGUAGE}语音助手，名字叫小灵。回答简洁口语化，像朋友聊天一样。"
                "需要实时信息时调用 web_search 工具；"
                "用户问到知识库文档相关内容（如 IST集团、大连百易、百易东京等公司资料）时，"
                "必须调用 search_knowledge_base 工具，依据它返回的内容回答，不要凭记忆编造。")

# 代理：墙内连 OpenAI 必须走代理。直接写死你科学上网工具的本地端口。
# （留空 "" 则改为自动读环境变量；如果代理工具重启后端口变了，来这里改）
PROXY = _ROOT_CFG.get("network", {}).get("proxy", "127.0.0.1:7890")

# 默认所在地（查天气等本地信息的缺省地点），从项目根 config.json 的 location 读
LOCATION = _ROOT_CFG.get("location", "大连")

# OpenAI Realtime 固定要求：PCM16、24000Hz、单声道
SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_FRAMES = 1200  # 每次发送的帧数（50ms）

# === VAD 模式：屏蔽"嗯啊"语气词主要靠这个（方案1）===
# "semantic" = 语义VAD，用语义判断、天生忽略"嗯啊"附和声（推荐）
# "server"   = 能量VAD，只看音量大小（老方案，想对比时用）
VAD_MODE = "semantic"
# 语义VAD的"急切度"：low=最沉得住气、最不容易被语气词打断。可选 low / medium / high / auto
VAD_EAGERNESS = "medium"

# 下面这三个只在 VAD_MODE="server" 时生效（能量VAD的灵敏度）
VAD_THRESHOLD = 0.75   # 0~1，越大越不敏感
VAD_SILENCE_MS = 1000 #900   # 停顿多少毫秒算说完
VAD_PREFIX_MS = 300    # 一般不用动

# === 方案2：屏蔽纯语气词（嗯/啊/呃...）===
# True = 收回应答控制权：只有"实质内容"才打断+回应，纯语气词直接忽略（不打断、不回应）
FILTER_FILLER = True

# === 唤醒词 + 激活窗口：解决"没跟它说话它也答"===
# 待机时只有听到唤醒词才开始对话；对话中超时没人说话就回到待机
ENABLE_WAKE = True
# 唤醒词（识别文字去掉空格标点后"包含"即算命中）：
# "小灵"的各种同音/近音误识别都算，另加"你好"（注意：别人互相打招呼说"你好"也会唤醒它）
WAKE_WORDS = [
    "你好小灵",
    "小灵", "小玲", "小凌", "小铃", "小翎", "小陵",     # xiǎo líng 同音字
    "晓灵", "晓玲", "晓凌",                              # xiǎo 的另一半同音
    "小林", "小淋", "晓菱",                              # 近音误识别（lin/ling 不分）
    "xiaoling", "xiao ling", "xiaolin",                  # 拼音
    # ---- 日语唤醒词 ----
    "シャオリン", "しゃおりん",                          # "小灵"音译（片假名/平假名）
    "シャオリング", "シャーリン", "シャオリーン",        # 音译的常见近似转写
    "こんにちは", "こんにちわ",                          # 你好（含常见错写）
    "こんにちはシャオリン",                              # 你好小灵（整句）
    "ハロー",                                            # hello 的日语说法
    "すみません",                                        # "不好意思/劳驾"——日本人呼唤服务最常用
    "你好",                                              # 通用唤醒（打招呼即唤醒）
]
WAKE_WINDOW_S = 30              # 唤醒后多少秒无对话 → 回待机
MIN_CHARS = 3                   # 识别文字少于这个字数直接忽略（防喘气/杂音被误识别成字）
# 打断门槛：True=它说话时只有喊唤醒词才能打断（其它话当没听见）；False=任何实质内容都能打断
INTERRUPT_REQUIRES_WAKE = True

# 进入待机时是否语音播报提示（文案按 LANGUAGE 自动选）
STANDBY_ANNOUNCE = True
_STANDBY_MESSAGES = {
    "中文": "我已经有一段时间没有听到您的声音，现在进入待机状态啦。需要我的时候，喊『你好小灵』就能唤醒我哦。",
    "日语": "しばらくお声が聞こえなかったので、待機モードに入りますね。ご用の際は『シャオリン』と呼んでください。",
}

# === 播放预缓冲：解决开头几个字卡顿 ===
PREBUFFER_MS = 400              # 开播前先攒多少毫秒的音频（大=更稳，小=开口更快）

# === 麦克风音量门：只拾取"近处"的声音 ===
# 每帧算能量(RMS)，低于门限的帧根本不发给 OpenAI —— 远处人声能量低，直接被挡在门外。
# 调法：先把 MIC_GATE_DEBUG 设 True 跑一次，看自己正常说话/远处人说话各是多少，门限取两者中间。
MIC_GATE_RMS = 0              # 门限（0=关闭音量门）。越大=拾音范围越近
MIC_GATE_HOLD_MS = 400          # 音量跌破门限后，门再保持打开的毫秒数（防止把句子中的轻音切碎）
MIC_GATE_DEBUG = False          # True=校准模式：实时打印当前音量和门开关状态
# ==================================================

URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
HEADERS = [
    f"Authorization: Bearer {API_KEY}",
]

# 纯语气词判断（方案2）
_FILLER_CHARS = set("嗯啊呃哦噢唉唔呢额诶嗯哦哈呵")
_FILLER_WORDS = {"um", "uh", "ah", "hmm", "mm", "en", "eh", "oh", "ok"}
# Whisper 对喘气/杂音的常见"幻觉"转写：出现这些整句时视为噪音，直接忽略
_HALLUCINATION_PHRASES = {
    "谢谢观看", "感谢观看", "谢谢收看", "谢谢大家", "请点赞订阅", "字幕由amara.org社区提供",
    "ご視聴ありがとうございました", "ご視聴ありがとうございます",
    "thank you for watching", "thanks for watching", "thank you",
    "Bye-bye","you","Mm-hmm",
    # 转写器把杂音脑补成的"客服欢迎语"（受导览领域引导影响的高频幻觉）
    "您好，请问有什么可以帮助您的吗", "您好，请问有什么可以帮到您的吗",
    "请问有什么可以帮助您的吗", "请问有什么可以帮到您的吗", "你好，请问有什么可以帮助你的吗",
    "有什么可以帮助您的吗", "有什么可以帮到您的吗",
}


def _normalize(s):
    """统一清洗：去掉空格和常见标点（含连字符）+ 转小写。输入和词表都用它，保证匹配一致。"""
    return re.sub(r"[\s,，。.!！?？、~…·\-'’\"]+", "", (s or "")).lower()


# 幻觉词表预先归一化（词表里写 "Bye-bye"、"bye bye" 都能匹配上）
_HALLUCINATION_NORM = {_normalize(p) for p in _HALLUCINATION_PHRASES}

# 幻觉"句式"匹配：整句词表拦不住变体（帮助您的/需要帮助的/帮到你…），按句式一网打尽。
# 注意模式作用在"归一化后"的文本上（无空格无标点）。设计得保守，避免误杀用户真话。
_HALLUCINATION_PATTERNS = [
    r"有什么(可以|需要|能|想)?(帮助|帮到|帮忙|为您|效劳)",   # 有什么可以帮助您/有什么需要帮忙…
    r"(帮助|帮到)(您|你)的?吗$",                             # …帮助您的吗
    r"为您服务",
    r"欢迎光临",
    # 日语客服套话
    r"いらっしゃいませ",
    r"何か.{0,8}(お手伝い|ご用)",                            # 何かお手伝いできることは…
    r"ご用件",
    r"お手伝い(できる|しましょう|いたします)",
    r"ご案内(いたします|します)$",
]
_HALLUCINATION_RE = [re.compile(p) for p in _HALLUCINATION_PATTERNS]



def is_filler(text):
    """识别出来的文字是不是'该忽略的声音'：语气词/太短/Whisper幻觉。"""
    t = _normalize(text)
    if not t:
        return True
    if len(t) < MIN_CHARS:                    # 太短（多为喘气/杂音被识别成单字）
        return True
    if all(c in _FILLER_CHARS for c in t):    # 全是语气字
        return True
    if t in _FILLER_WORDS:
        return True
    if t in _HALLUCINATION_NORM:              # 幻觉整句（已统一归一化）
        return True
    for _pat in _HALLUCINATION_RE:            # 幻觉句式（变体也拦）
        if _pat.search(t):
            return True
    return False


def proxy_kwargs():
    """优先用写死的 PROXY，否则读环境变量；显式传给 websocket-client。"""
    p = PROXY or (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                  or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if not p:
        return {}
    u = urlparse(p if "://" in p else "http://" + p)
    if not u.hostname:
        return {}
    return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}


def requests_proxies():
    """requests 走同一个代理（Tavily 可能也需要代理）。"""
    return {"http": f"http://{PROXY}", "https": f"http://{PROXY}"} if PROXY else None


# 告诉模型有这么个工具可用：需要实时/最新信息时就调它
WEB_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": "当用户问到实时信息、新闻、天气、最新事实、或你不确定/可能过时的内容时，调用此工具联网搜索。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（中文或英文）"}
        },
        "required": ["query"],
    },
}


# ============ 场景系统 + 本地知识库（见 scenes.json / kb.py）============
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core")))
import kb as kb_module

# 加载场景配置；当前场景可被 switch_scene 工具在对话中切换
try:
    _SCENES_CFG = kb_module.load_scenes()
except Exception as _exc:
    print("[警告] scenes.json 加载失败，场景功能不可用:", _exc)
    _SCENES_CFG = {"default_scene": "", "common_rules": "", "scenes": {}}
_current_scene_key = _SCENES_CFG.get("default_scene", "")


def current_scene():
    return _SCENES_CFG["scenes"].get(_current_scene_key, {})


def current_instructions():
    """人设 = 当前场景的提示词 + 通用规则 + 主语言说明；没有场景就用默认 INSTRUCTIONS。"""
    lang_rule = (f"你的主语言是{LANGUAGE}，默认用{LANGUAGE}交流；用户用其他语言提问时跟随用户的语言。"
                 f"你所在的城市是{LOCATION}：用户询问天气、新闻等本地信息但没有指明地点时，"
                 f"默认按{LOCATION}查询（联网搜索的关键词里带上{LOCATION}）。")
    sc = current_scene()
    if not sc:
        return INSTRUCTIONS + "\n\n" + lang_rule
    return sc["instructions"] + "\n\n" + _SCENES_CFG.get("common_rules", "") + "\n" + lang_rule


def transcribe_prompt():
    """语音转写的引导提示，跟随 LANGUAGE 配置。"""
    return _TRANSCRIBE_PROMPTS.get(
        LANGUAGE, f"以下是用户对语音助手说的{LANGUAGE}对话。助手的名字叫小灵。")


def kb_search_tool():
    """知识库工具定义：描述带当前场景的中/日文档名；language 参数用于双语路由。"""
    sc = current_scene()
    zh = kb_module.list_index_files(kb_module.scene_kb_dir(sc, "zh")) if sc else []
    ja = kb_module.list_index_files(kb_module.scene_kb_dir(sc, "ja")) if sc else []
    hint = "中文文档：" + ("、".join(zh) or "无") + "；日文文档：" + ("、".join(ja) or "无")
    return {
        "type": "function",
        "name": "search_knowledge_base",
        "description": (f"查询当前场景（{sc.get('name', '默认')}）的本地知识库并给出答案。"
                        f"用户问到公司/设施/景点等相关内容时必须调用。{hint}"),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户的问题（保持原意）"},
                "language": {"type": "string", "enum": ["zh", "ja"],
                             "description": "用户提问用的语言：中文=zh，日语=ja"},
            },
            "required": ["query"],
        },
    }


def switch_scene_tool():
    """场景切换工具定义：让模型听到『切换到XX模式』时调用。"""
    scenes_desc = "；".join(
        f"{k}={sc['name']}（别名：{'、'.join(sc.get('aliases', []))}）"
        for k, sc in _SCENES_CFG["scenes"].items())
    return {
        "type": "function",
        "name": "switch_scene",
        "description": (f"切换助手的工作场景（人设+知识库整体切换）。"
                        f"用户说『切换到XX模式』『XXモードに切り替えて』等时调用。可选场景：{scenes_desc}"),
        "parameters": {
            "type": "object",
            "properties": {"scene": {"type": "string",
                                     "enum": list(_SCENES_CFG["scenes"].keys()) or ["none"],
                                     "description": "目标场景的 key"}},
            "required": ["scene"],
        },
    }


# 只有问"展板"时，才让模型在介绍完后补"可切换场景"的提示（精准投放，别的回答不带）
_PANEL_WORDS = ("展板", "展示板", "看板", "パネル", "ボード", "panel")
_PANEL_FOLLOWUP = {
    "zh": "\n\n【指示】介绍完上述展板内容之后，请补充一句：『我目前有两个场景，您可以说切换到银座模式或者切换到景点模式来进行场景切换哦。』",
    "ja": "\n\n【指示】上記パネルの紹介が終わったら、最後に一言添えてください：『現在、私にはふたつのモードがあります。「銀座モードに切り替えて」または「観光地モードに切り替えて」と言っていただければ切り替えできますよ。』",
}


def kb_search(query, language=""):
    """查当前场景的知识库：按提问语言选中文/日文库 → 简介路由 → 全文作答。"""
    sc = current_scene()
    if not sc:
        return "当前没有可用的场景知识库。"
    lang = language if language in ("zh", "ja") else kb_module.detect_language(query)
    kb_dir = kb_module.scene_kb_dir(sc, lang)
    print(f"  [查知识库] 场景={sc.get('name')} 语言={lang} 问题={query}")
    try:
        out = kb_module.kb_answer(query, kb_dir, API_KEY, PROXY)
        print(f"  [知识库回答] {out[:80]}...")
        # 展厅场景 + 问的是展板 → 在这次结果里附上"介绍完提示切场景"的指示
        if _current_scene_key == "showroom" and any(w in (query or "").lower() for w in _PANEL_WORDS):
            out += _PANEL_FOLLOWUP.get(lang, _PANEL_FOLLOWUP["zh"])
        return out
    except Exception as exc:
        print("  [知识库失败]", exc)
        return f"知识库查询失败：{exc}"


def do_switch_scene(scene_key):
    """执行场景切换：换人设+知识库+工具描述（session.update 热更新，不断线、不换音色）。"""
    global _current_scene_key
    if scene_key not in _SCENES_CFG["scenes"]:
        return f"没有这个场景：{scene_key}。可选：{'、'.join(_SCENES_CFG['scenes'].keys())}"
    _current_scene_key = scene_key
    sc = current_scene()
    print(f"  [场景切换] → {sc['name']}")
    try:
        with _send_lock:
            _ws_app.send(json.dumps(build_session_update()))
    except Exception as exc:
        print("  [场景切换失败]", exc)
        return f"切换失败：{exc}"
    return (f"已切换到『{sc['name']}』场景。请立刻以新场景的身份，"
            f"用一句话向用户确认已切换（用户用日语提问就用日语确认）。")


def tavily_search(query):
    """调 Tavily 联网搜索，返回给模型用的文字结果。"""
    print(f"  [联网搜索] {query}")
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": 5, "include_answer": True},
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            proxies=requests_proxies(), timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = []
        if data.get("answer"):
            parts.append("摘要：" + data["answer"])
        for r in data.get("results", [])[:5]:
            parts.append(f"- {r.get('title','')}：{(r.get('content','') or '')[:200]}")
        out = "\n".join(parts) or "没有搜到相关结果。"
        print(f"  [搜索完成] {out[:80]}...")
        return out
    except Exception as exc:
        print("  [搜索失败]", exc)
        return f"搜索失败：{exc}"


def handle_function_call(name, call_id, arguments):
    """模型要调工具时：另起线程执行（避免阻塞收消息），把结果送回并让模型继续。"""
    def run():
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            args = {}
        if name == "web_search":
            result = tavily_search(args.get("query", ""))
        elif name == "search_knowledge_base":
            result = kb_search(args.get("query", ""), args.get("language", ""))
        elif name == "switch_scene":
            result = do_switch_scene(args.get("scene", ""))
        elif name == "enter_standby":
            result = do_enter_standby()
        else:
            result = "未知工具"
        with _send_lock:
            _ws_app.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": result},
            }))
        # 查询期间若有别的回应插了进来（如误识别触发的），先取消它，
        # 保证查询结果一定能被念出来（避免 conversation_already_has_active_response 报错）
        if _is_responding:
            with _send_lock:
                _ws_app.send(json.dumps({"type": "response.cancel"}))
        with _send_lock:
            _ws_app.send(json.dumps({"type": "response.create"}))
    threading.Thread(target=run, daemon=True).start()

# 播放队列：收到的音频分片排队，由独立播放线程阻塞写入声卡（和 hear_test 一样的可靠方式）
_audio_q = queue.Queue()
_send_lock = threading.Lock()
_ws_app = None
_connected = threading.Event()
_player_stream = None
_is_responding = False   # 模型当前是否正在回复（方案2 判断要不要打断）
_suppress_audio = False  # 打断后丢弃"迟到的旧音频分片"，直到新回复开始
_awake = not ENABLE_WAKE          # 唤醒状态：不启用唤醒机制时恒为"激活"
_last_active_ts = 0.0             # 最近一次有效交互时间（算激活窗口超时用）
_playing = False                  # 是否正在播放语音（播放中不判待机超时）


def _drain_audio():
    """打断时清空还没播的音频。"""
    try:
        while True:
            _audio_q.get_nowait()
    except queue.Empty:
        pass


def _discard_item(item_id):
    """把被忽略的那句话从服务端对话历史里删掉。
    否则它会一直留在历史里，下次任何回应触发时，模型会把这些"被忽略的话"补答出来。"""
    if not item_id:
        return
    try:
        with _send_lock:
            _ws_app.send(json.dumps({"type": "conversation.item.delete", "item_id": item_id}))
    except Exception:
        pass


def handle_user_utterance(transcript, item_id=""):
    """收到一句识别文字后的总调度：回显过滤 → 待机/唤醒 → 语气词过滤 → 打断+回应。
    所有"忽略"路径都会把这句话从服务端历史删掉（_discard_item），忽略=像没说过。"""
    global _awake, _last_active_ts, _is_responding, _suppress_audio
    now = time.time()

    # ⓪ 转写器听到杂音时偶尔会把"引导提示"原文回显成识别结果 → 直接忽略
    #（只对较长文本做包含判断，避免误杀真正含"小灵"的短唤醒句）
    norm_t = _normalize(transcript)
    norm_p = _normalize(transcribe_prompt())
    if len(norm_t) >= 10 and (norm_t in norm_p or norm_p in norm_t):
        print("  [忽略] 转写回显了引导提示（多为杂音）")
        _discard_item(item_id)
        return

    # ① 激活窗口超时 → 回待机
    if ENABLE_WAKE and _awake and _last_active_ts and now - _last_active_ts > WAKE_WINDOW_S:
        _awake = False
        print(f"  [待机] 超过 {WAKE_WINDOW_S} 秒无对话，回到待机（喊『{WAKE_WORDS[0]}』唤醒）")

    # ② 待机时：只有听到唤醒词才理会（两人闲聊/路人说话/通知音一律无视）
    # 匹配前先去掉空格和标点，这样识别成"小 灵""小灵，"也能命中
    if ENABLE_WAKE and not _awake:
        norm = _normalize(transcript)
        if not any(_normalize(w) in norm for w in WAKE_WORDS):
            print("  [待机忽略] 没听到唤醒词，不回应")
            _discard_item(item_id)
            return
        _awake = True
        print("  [唤醒] 进入对话模式")

    # ③ 语气词/杂音幻觉/太短 → 忽略（不打断、不回应）
    if FILTER_FILLER and is_filler(transcript):
        print("  [忽略语气词] 不打断、不回应")
        _discard_item(item_id)
        return

    # ③′ 打断门槛：它正在说话（生成中/还在播）时，只有含唤醒词的话才允许打断；
    #     其它话当没听见（并从历史删除，防止说完后被"补答"）。说完后的空闲期不受此限制。
    if INTERRUPT_REQUIRES_WAKE and (_is_responding or _playing):
        norm = _normalize(transcript)
        if not any(_normalize(w) in norm for w in WAKE_WORDS):
            print(f"  [说话中忽略] 未喊『{WAKE_WORDS[0]}』，不打断，继续说完")
            _discard_item(item_id)
            return

    # ④ 实质内容：无条件停掉旧音频（模型生成常早已结束、音频还在慢慢播，不能只在"生成中"才清），再回应
    _last_active_ts = now
    print("  [处理] 停止当前播放，回应这句话")
    _suppress_audio = True       # 之后迟到的旧音频一律丢弃
    _drain_audio()               # 清空待播队列
    if _is_responding:           # 若模型还在生成，再发取消
        with _send_lock:
            _ws_app.send(json.dumps({"type": "response.cancel"}))
        _is_responding = False
    with _send_lock:
        _ws_app.send(json.dumps({"type": "response.create"}))


def _announce_standby():
    """进入待机时，让模型把待机提示念出来（文案跟随 LANGUAGE）。
    用"带外响应"（conversation:none）：不读对话历史、也不写入历史——
    否则模型会被之前的聊天上下文带偏，不照稿念、反而接着聊之前的话题。"""
    msg = _STANDBY_MESSAGES.get(LANGUAGE, _STANDBY_MESSAGES["中文"])
    try:
        with _send_lock:
            _ws_app.send(json.dumps({"type": "response.create", "response": {
                "conversation": "none",
                "input": [],   # 关键：显式清空上下文。只有 conversation:none 不够——不传 input 模型仍会读对话历史、被带偏
                "output_modalities": ["audio"],
                "instructions": f"直接说出下面这句话，一字不差，不要加任何开场白、确认语或额外内容：{msg}"}}))
    except Exception as exc:
        print("[待机播报失败]", exc)


def do_enter_standby():
    """用户口头让助手待机（"进入待机状态/休息吧"）→ 立即置待机，让模型说句告别语。"""
    global _awake
    _awake = False
    print("  [待机] 收到口头指令，进入待机")
    return ("已进入待机状态。请用一句话向用户确认并告别（例如：好的，我先休息啦，"
            "需要我时喊『你好小灵』就行），用户用日语就用日语说。之后保持安静。")


def standby_tool():
    """待机工具定义：让模型听到"进入待机/休息吧"这类指令时调用。"""
    return {
        "type": "function",
        "name": "enter_standby",
        "description": ("让助手进入待机（休眠）状态。用户说『进入待机』『休息吧』『先这样吧，你去休息』"
                        "『待機モードに入って』『休んでいいよ』等时调用。"),
        "parameters": {"type": "object", "properties": {}},
    }


def player_loop():
    """独立线程：从队列取音频，阻塞写入耳机/扬声器。
    每段语音开播前先攒 PREBUFFER_MS 毫秒（预缓冲），避免"边下边播"开头断续卡顿。
    每播完一段就刷新激活计时——待机倒计时从"它说完"起算，而不是"你说完"。"""
    global _last_active_ts, _playing
    prebuf_bytes = SAMPLE_RATE * 2 * PREBUFFER_MS // 1000   # int16 单声道
    while True:
        chunk = _audio_q.get()
        if chunk is None:
            break
        _playing = True
        # —— 新一段语音开始：先攒预缓冲，攒够或短暂等不到新块就开播 ——
        buf = bytearray(chunk)
        while len(buf) < prebuf_bytes:
            try:
                nxt = _audio_q.get(timeout=0.35)
            except queue.Empty:
                break
            if nxt is None:
                return
            buf.extend(nxt)
        try:
            _player_stream.write(bytes(buf))
        except Exception as exc:
            print("[播放错误]", exc)
        # —— 本段后续块流式播放；队列闲置视为本段结束，回外层重新预缓冲 ——
        while True:
            try:
                nxt = _audio_q.get(timeout=0.6)
            except queue.Empty:
                break
            if nxt is None:
                return
            try:
                _player_stream.write(nxt)
            except Exception as exc:
                print("[播放错误]", exc)
        # 这段语音播完 = 它刚说完话 → 激活窗口从现在重新计时
        _last_active_ts = time.time()
        _playing = False


def wake_watchdog():
    """后台看门狗：每秒检查一次激活窗口，到点立刻进入待机并在屏幕提示（不用等下一句话）。"""
    global _awake
    while True:
        time.sleep(1)
        if (ENABLE_WAKE and _awake and _last_active_ts
                and not _is_responding and not _playing
                and time.time() - _last_active_ts > WAKE_WINDOW_S):
            _awake = False
            print(f"\n[待机] 超过 {WAKE_WINDOW_S} 秒无对话，已进入待机（喊『{WAKE_WORDS[0]}』唤醒）")
            if STANDBY_ANNOUNCE:
                _announce_standby()   # 语音播报"我进入待机了，喊你好小灵唤醒我"


def build_session_update():
    """构建会话配置（人设/工具跟随当前场景；切场景时重发即可热更新）。"""
    # 根据 VAD_MODE 选择断句方式：semantic=语义VAD(忽略嗯啊)，server=能量VAD
    if VAD_MODE == "semantic":
        turn_detection = {"type": "semantic_vad", "eagerness": VAD_EAGERNESS}
    else:
        turn_detection = {
            "type": "server_vad",
            "threshold": VAD_THRESHOLD,
            "prefix_padding_ms": VAD_PREFIX_MS,
            "silence_duration_ms": VAD_SILENCE_MS,
        }
    if FILTER_FILLER or ENABLE_WAKE:
        # 收回控制权：不自动应答、不自动打断，改由我们按识别内容（唤醒词/是否语气词）决定
        turn_detection["create_response"] = False
        turn_detection["interrupt_response"] = False
    # GA 正式版会话格式：audio 嵌套 input/output；VAD 放在 audio.input 下
    session = {
        "type": "realtime",
        "output_modalities": ["audio"],
        "instructions": current_instructions(),
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": "near_field"},   # 输入降噪：减少喘气/环境音触发
                # 转写模型 + 引导提示：跟随 LANGUAGE 配置（不锁 language 字段，双语仍可识别）
                "transcription": {"model": TRANSCRIBE_MODEL, "prompt": transcribe_prompt()},
                "turn_detection": turn_detection,
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": VOICE,
            },
        },
    }
    # 挂工具：联网检索 + 本地知识库（跟随场景）+ 场景切换 + 口头待机
    tools = []
    if ENABLE_SEARCH:
        tools.append(WEB_SEARCH_TOOL)
    if ENABLE_KB:
        tools.append(kb_search_tool())
    if _SCENES_CFG["scenes"]:
        tools.append(switch_scene_tool())
    if ENABLE_WAKE:
        tools.append(standby_tool())
    if tools:
        session["tools"] = tools
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


def on_open(ws):
    """连接建立后：发送会话配置（场景人设、音色、音频格式、VAD 断句）"""
    sc = current_scene()
    print(f"[连接成功] 场景={sc.get('name', '默认')} | VAD={VAD_MODE}" +
          (f"/eagerness={VAD_EAGERNESS}" if VAD_MODE == "semantic" else ""))
    ws.send(json.dumps(build_session_update()))
    _connected.set()
    if ENABLE_WAKE:
        print(f"[就绪] 先喊『{WAKE_WORDS[0]}』唤醒再对话；{WAKE_WINDOW_S}秒没人说话自动待机。"
              f"说『切换到商场导览模式』可切场景。Ctrl+C 退出。\n")
    else:
        print("[就绪] 对麦克风说话即可；说『切换到商场导览模式』可切场景。Ctrl+C 退出。\n")


def on_message(ws, message):
    """处理服务端推来的事件"""
    global _is_responding, _suppress_audio, _last_active_ts
    event = json.loads(message)
    etype = event.get("type", "")

    if etype in ("response.output_audio.delta", "response.audio.delta"):
        # 打断窗口内丢弃"迟到的旧音频分片"，避免清空队列后又被塞回来继续播
        if _suppress_audio:
            return
        _audio_q.put(base64.b64decode(event["delta"]))

    elif etype == "response.created":
        _is_responding = True
        _suppress_audio = False   # 新回复开始，恢复正常播放

    elif etype == "conversation.item.input_audio_transcription.completed":
        transcript = event.get("transcript", "").strip()
        print(f"你: {transcript}")
        # item_id 用于"忽略时把这句话从服务端历史删掉"
        handle_user_utterance(transcript, event.get("item_id", ""))

    elif etype in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
        # 模型回复的文字
        print(f"小灵: {event.get('transcript', '').strip()}\n")

    elif etype == "input_audio_buffer.speech_started":
        # 只有"全自动模式"（既不过滤语气词也不用唤醒词）才一开口就打断；
        # 否则等识别出文字、由 handle_user_utterance 判断后再决定
        if not (FILTER_FILLER or ENABLE_WAKE):
            _drain_audio()

    elif etype == "response.done":
        _is_responding = False
        # 兜底刷新激活计时（音频播完时播放线程还会再刷一次，取更晚者）
        _last_active_ts = time.time()
        # 若这次回复里模型决定调工具（联网搜索），在这里执行
        for item in event.get("response", {}).get("output", []):
            if item.get("type") == "function_call":
                handle_function_call(item.get("name"), item.get("call_id"), item.get("arguments"))

    elif etype == "error":
        print("[服务端错误]", json.dumps(event.get("error", {}), ensure_ascii=False))


def on_error(ws, error):
    print("[连接错误]", error)


def on_close(ws, code, msg):
    print("[连接关闭]", code, msg)


def _rms(data):
    """算一帧 int16 音频的能量（响度）。不依赖 numpy/audioop（audioop 在 Python3.13 已移除）。"""
    import array as _arr
    samples = _arr.array('h', data)
    if not samples:
        return 0
    total = 0
    for v in samples:
        total += v * v
    return int((total / len(samples)) ** 0.5)


_gate_open_until = 0.0   # 音量门：开到什么时刻（跌破门限后延迟关门，防切碎句子）


def mic_callback(indata, frames, time_info, status):
    """麦克风回调：先过音量门（低于门限=远处声音，不发），再把音频发给 OpenAI"""
    global _gate_open_until
    if not _connected.is_set():
        return
    data = bytes(indata)

    if MIC_GATE_RMS > 0:
        level = _rms(data)
        now = time.time()
        if level >= MIC_GATE_RMS:
            _gate_open_until = now + MIC_GATE_HOLD_MS / 1000.0
        gate_open = now < _gate_open_until
        if MIC_GATE_DEBUG:
            bar = "#" * min(40, level // 100)
            print(f"\r[音量校准] RMS={level:5d}  门限={MIC_GATE_RMS}  门={'开' if gate_open else '关'}  {bar:<40}",
                  end="", flush=True)
        if not gate_open:
            return   # 门关着：这帧是远处/环境声，不发给 OpenAI

    event = {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(data).decode("ascii"),
    }
    try:
        with _send_lock:
            _ws_app.send(json.dumps(event))
    except Exception:
        pass


def main():
    global _ws_app, _player_stream

    if "粘贴" in API_KEY or not API_KEY.startswith("sk-"):
        print("！请先在脚本顶部把 API_KEY 填成你的 OpenAI key（以 sk- 开头），或设置环境变量 OPENAI_API_KEY")
        return

    _ws_app = websocket.WebSocketApp(
        URL,
        header=HEADERS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # WebSocket 在后台线程跑（显式走代理）
    pk = proxy_kwargs()
    if pk:
        print(f"[代理] 走 {pk['http_proxy_host']}:{pk['http_proxy_port']}")
    ws_thread = threading.Thread(target=lambda: _ws_app.run_forever(**pk), daemon=True)
    ws_thread.start()

    # 等连接就绪（首次握手 + DNS 偶尔较慢，给足 30 秒）
    if not _connected.wait(timeout=30):
        print("！连接超时。多为一次性网络抖动，直接重跑一次通常就好。")
        print("  若反复超时再查：1) 网络能否访问 OpenAI 2) 是否需要科学上网/代理")
        return

    # 扬声器：阻塞写入流 + 独立播放线程（和 hear_test 同样可靠的方式）
    # latency='low' + 小 blocksize：减小声卡缓冲，打断时残留尾音更短
    _player_stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
        blocksize=CHUNK_FRAMES, latency='low',
    )
    _player_stream.start()
    threading.Thread(target=player_loop, daemon=True).start()
    threading.Thread(target=wake_watchdog, daemon=True).start()   # 待机看门狗：到点即时提示

    # 麦克风：回调采集后发给 OpenAI
    mic = sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
        blocksize=CHUNK_FRAMES, callback=mic_callback,
    )

    with mic:
        try:
            while ws_thread.is_alive():
                threading.Event().wait(0.5)
        except KeyboardInterrupt:
            print("\n[退出]")
        finally:
            _audio_q.put(None)
            _ws_app.close()


if __name__ == "__main__":
    main()
