# -*- coding: utf-8 -*-
"""
DialogSession：一个浏览器连接对应一个对话会话。
逻辑与 realtime_test/openai_realtime_test.py 一致（场景/知识库/唤醒/语气词过滤/打断/待机播报），
区别只有音频通道：麦克风音频来自浏览器（feed_audio），模型语音发回浏览器（send_audio 回调）。
复用 core/ 的 kb.py、scenes.json 与项目根的 知识库/。
"""
import os
import re
import sys
import json
import time
import base64
import threading
from urllib.parse import urlparse

import websocket
import requests

# 共享核心：core/ 里的 kb 模块和场景配置
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "core")))
import kb as kb_module

# ============ 行为参数（与 Windows 原型一致；要调就改这里） ============
VAD_MODE = "semantic"
VAD_EAGERNESS = "medium"
VAD_THRESHOLD = 0.75
VAD_SILENCE_MS = 900
VAD_PREFIX_MS = 300
FILTER_FILLER = True
MIN_CHARS = 3
MIC_GATE_HOLD_MS = 400

WAKE_WORDS = [
    "你好小灵",
    "小灵", "小玲", "小凌", "小铃", "小翎", "小陵",
    "晓灵", "晓玲", "晓凌",
    "小林", "小淋", "晓菱",
    "xiaoling", "xiao ling", "xiaolin",
    "シャオリン", "しゃおりん",
    "シャオリング", "シャーリン", "シャオリーン",
    # "こんにちは", "こんにちわ",   # 通用打招呼词，误唤醒源，已停用（与桌面版一致）
    "こんにちはシャオリン",
    "ハロー",
    "すみません",
    # "你好",   # 同上
]

_STANDBY_MESSAGES = {
    "中文": "我已经有一段时间没有听到您的声音，现在进入待机状态啦。需要我的时候，喊『你好小灵』就能唤醒我哦。",
    "日语": "しばらくお声が聞こえなかったので、待機モードに入りますね。ご用の際は『シャオリン』と呼んでください。",
}

_TRANSCRIBE_PROMPTS = {
    "中文": "以下是中文普通话的导览问答对话，内容多与公司、商场、景点介绍有关。对话中可能出现名字『小灵』。",
    "日语": "以下は日本語の案内・質問応答の会話です。内容は会社・商業施設・観光地の案内が中心です。会話に『小灵（シャオリン）』という名前が出ることがあります。",
}

_FILLER_CHARS = set("嗯啊呃哦噢唉唔呢额诶嗯哦哈呵")
_FILLER_WORDS = {"um", "uh", "ah", "hmm", "mm", "en", "eh", "oh", "ok"}
_HALLUCINATION_PHRASES = {
    "谢谢观看", "感谢观看", "谢谢收看", "谢谢大家", "请点赞订阅", "字幕由amara.org社区提供",
    "ご視聴ありがとうございました", "ご視聴ありがとうございます",
    "thank you for watching", "thanks for watching", "thank you",
    "bye-bye", "you", "mm-hmm",
    "您好，请问有什么可以帮助您的吗", "您好，请问有什么可以帮到您的吗",
    "请问有什么可以帮助您的吗", "请问有什么可以帮到您的吗", "你好，请问有什么可以帮助你的吗",
    "有什么可以帮助您的吗", "有什么可以帮到您的吗",
}
SAMPLE_RATE = 24000
# ======================================================================


def _normalize(s):
    return re.sub(r"[\s,，。.!！?？、~…·\-'’\"]+", "", (s or "")).lower()


_HALLUCINATION_NORM = {_normalize(p) for p in _HALLUCINATION_PHRASES}


def is_filler(text):
    t = _normalize(text)
    if not t:
        return True
    if len(t) < MIN_CHARS:
        return True
    if all(c in _FILLER_CHARS for c in t):
        return True
    if t in _FILLER_WORDS:
        return True
    if t in _HALLUCINATION_NORM:
        return True
    return False


def _rms(data):
    import array as _arr
    samples = _arr.array('h', data)
    if not samples:
        return 0
    total = 0
    for v in samples:
        total += v * v
    return int((total / len(samples)) ** 0.5)


WEB_SEARCH_TOOL = {
    "type": "function", "name": "web_search",
    "description": "当用户问到实时信息、新闻、天气、最新事实、或你不确定/可能过时的内容时，调用此工具联网搜索。",
    "parameters": {"type": "object",
                   "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                   "required": ["query"]},
}

# 只有问"展板"时，才让模型在介绍完后补"可切换场景"的提示（精准投放，别的回答不带）
_PANEL_WORDS = ("展板", "展示板", "看板", "パネル", "ボード", "panel")
_PANEL_FOLLOWUP = {
    "zh": "\n\n【指示】介绍完上述展板内容之后，请补充一句：『我目前有两个场景，您可以说切换到商场模式或者切换到景点模式来进行场景切换哦。』",
    "ja": "\n\n【指示】上記パネルの紹介が終わったら、最後に一言添えてください：『現在、私にはふたつのモードがあります。「ショッピングモールモードに切り替えて」または「観光地モードに切り替えて」と言っていただければ切り替えできますよ。』",
}


class DialogSession:
    """一个浏览器连接 = 一个会话。音频进出走回调，其余逻辑与桌面原型一致。"""

    def __init__(self, cfg, send_json, send_audio):
        """send_json(dict) / send_audio(bytes)：线程安全回调，把内容投递给浏览器。"""
        self.cfg = cfg
        self.send_json = send_json
        self.send_audio = send_audio

        oa = cfg["openai"]
        self.api_key = oa["api_key"]
        self.model = oa.get("model", "gpt-realtime")
        self.voice = oa.get("voice", "marin")
        self.transcribe_model = oa.get("transcribe_model", "gpt-4o-mini-transcribe")
        self.proxy = cfg.get("network", {}).get("proxy", "")
        self.language = cfg.get("language", "中文")
        self.search_on = bool(cfg.get("search", {}).get("enable", True))
        self.tavily_key = cfg.get("search", {}).get("tavily_api_key", "")
        wake = cfg.get("wake", {})
        self.wake_enable = bool(wake.get("enable", True))
        self.wake_window_s = int(wake.get("window_s", 30))
        self.standby_announce = bool(wake.get("standby_announce", True))
        # 打断门槛：True=它说话时只有喊唤醒词才能打断（其它话当没听见）；False=任何实质内容都能打断
        self.interrupt_requires_wake = bool(wake.get("interrupt_requires_wake", True))
        self.mic_gate_rms = int(cfg.get("mic_gate_rms", 0))

        self.scenes_cfg = kb_module.load_scenes()
        self.scene_key = self.scenes_cfg.get("default_scene", "")

        self._ws = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._should_run = True
        self._backoff = 2
        self._is_responding = False
        self._suppress_audio = False
        self._awake = not self.wake_enable
        self._last_active_ts = 0.0
        self._playing = False        # 浏览器是否还在播（靠 playback_done 消息刷新）
        self._gate_open_until = 0.0

    # ---------- 生命周期 ----------
    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def close(self):
        self._should_run = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ---------- 浏览器 → 会话 ----------
    def feed_audio(self, pcm):
        """浏览器麦克风音频（PCM16@24k）。先过音量门，再转发 OpenAI。"""
        if not pcm or not self._ready.is_set():
            return
        if self.mic_gate_rms > 0:
            level = _rms(pcm)
            now = time.time()
            if level >= self.mic_gate_rms:
                self._gate_open_until = now + MIC_GATE_HOLD_MS / 1000.0
            if now >= self._gate_open_until:
                return
        self._send({"type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii")})

    def notify_playback_done(self):
        """浏览器播完一段语音 → 待机倒计时从'它说完'起算。"""
        self._playing = False
        self._last_active_ts = time.time()

    def set_language(self, lang):
        """网页按钮切换主语言（每个会话独立）：热更新人设+转写引导，并口头确认。"""
        if lang not in ("中文", "日语") or lang == self.language:
            return
        self.language = lang
        self._send(self._session_update())   # 热更新（不断线）：instructions + 转写 prompt
        self._log(f"[语言切换] → {lang}")
        try:
            self.send_json({"type": "language", "value": lang})
        except Exception:
            pass
        confirm = {"中文": "好的，已切换为中文模式，现在可以用中文和我聊天啦。",
                   "日语": "かしこまりました。日本語モードに切り替えました。日本語でどうぞ。"}[lang]
        # 带外响应念确认语（不读/不写对话历史）
        self._send({"type": "response.create", "response": {
            "conversation": "none",
            "input": [],   # 显式清空上下文
            "output_modalities": ["audio"],
            "instructions": f"直接说出下面这句话，一字不差，不要加任何开场白、确认语或额外内容：{confirm}"}})

    def feed_text(self, text):
        """网页文字输入（调试用）。"""
        text = (text or "").strip()
        if not text:
            return
        self._log(f"收到文字输入: {text}")
        self._interrupt_playback()
        if self._is_responding:
            self._send({"type": "response.cancel"})
            self._is_responding = False
        self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}})
        self._send({"type": "response.create"})

    # ---------- 会话 → 浏览器 ----------
    def _log(self, text):
        print(f"[session] {text}", flush=True)
        try:
            self.send_json({"type": "log", "text": text})
        except Exception:
            pass

    def _status(self, s):
        try:
            self.send_json({"type": "status", "value": s})
        except Exception:
            pass

    def _interrupt_playback(self):
        """打断：停发新音频 + 让浏览器清掉待播队列。"""
        self._suppress_audio = True
        self._playing = False
        try:
            self.send_json({"type": "clear"})
        except Exception:
            pass

    # ---------- 场景 ----------
    def _scene(self):
        return self.scenes_cfg["scenes"].get(self.scene_key, {})

    def _instructions(self):
        lang_rule = (f"你的主语言是{self.language}，默认用{self.language}交流；"
                     f"用户用其他语言提问时跟随用户的语言。")
        sc = self._scene()
        if not sc:
            return f"你是一个友好的{self.language}语音助手，名字叫小灵。" + lang_rule
        return sc["instructions"] + "\n\n" + self.scenes_cfg.get("common_rules", "") + "\n" + lang_rule

    def _transcribe_prompt(self):
        return _TRANSCRIBE_PROMPTS.get(self.language, _TRANSCRIBE_PROMPTS["中文"])

    def _tools(self):
        tools = []
        if self.search_on:
            tools.append(WEB_SEARCH_TOOL)
        sc = self._scene()
        if sc:
            zh = kb_module.list_index_files(kb_module.scene_kb_dir(sc, "zh"))
            ja = kb_module.list_index_files(kb_module.scene_kb_dir(sc, "ja"))
            hint = "中文文档：" + ("、".join(zh) or "无") + "；日文文档：" + ("、".join(ja) or "无")
            tools.append({
                "type": "function", "name": "search_knowledge_base",
                "description": (f"查询当前场景（{sc.get('name','')}）的本地知识库并给出答案。"
                                f"用户问到公司/设施/景点等相关内容时必须调用。{hint}"),
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "用户的问题（保持原意）"},
                    "language": {"type": "string", "enum": ["zh", "ja"],
                                 "description": "用户提问用的语言：中文=zh，日语=ja"}},
                    "required": ["query"]},
            })
        if self.scenes_cfg["scenes"]:
            scenes_desc = "；".join(
                f"{k}={v['name']}（别名：{'、'.join(v.get('aliases', []))}）"
                for k, v in self.scenes_cfg["scenes"].items())
            tools.append({
                "type": "function", "name": "switch_scene",
                "description": (f"切换助手的工作场景（人设+知识库整体切换）。"
                                f"用户说『切换到XX模式』等时调用。可选场景：{scenes_desc}"),
                "parameters": {"type": "object", "properties": {
                    "scene": {"type": "string", "enum": list(self.scenes_cfg["scenes"].keys())}},
                    "required": ["scene"]},
            })
        if self.wake_enable:
            tools.append({
                "type": "function", "name": "enter_standby",
                "description": ("让助手进入待机状态。用户说『进入待机』『休息吧』"
                                "『待機モードに入って』等时调用。"),
                "parameters": {"type": "object", "properties": {}},
            })
        return tools

    def _session_update(self):
        if VAD_MODE == "semantic":
            td = {"type": "semantic_vad", "eagerness": VAD_EAGERNESS}
        else:
            td = {"type": "server_vad", "threshold": VAD_THRESHOLD,
                  "prefix_padding_ms": VAD_PREFIX_MS, "silence_duration_ms": VAD_SILENCE_MS}
        if FILTER_FILLER or self.wake_enable:
            td["create_response"] = False
            td["interrupt_response"] = False
        session = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self._instructions(),
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                          "noise_reduction": {"type": "near_field"},
                          "transcription": {"model": self.transcribe_model,
                                            "prompt": self._transcribe_prompt()},
                          "turn_detection": td},
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                           "voice": self.voice},
            },
        }
        tools = self._tools()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return {"type": "session.update", "session": session}

    # ---------- OpenAI 连接（带重连） ----------
    def _proxy_kwargs(self):
        if not self.proxy:
            return {}
        u = urlparse(self.proxy if "://" in self.proxy else "http://" + self.proxy)
        return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}

    def _send(self, obj):
        try:
            with self._lock:
                if self._ws:
                    self._ws.send(json.dumps(obj))
        except Exception as exc:
            self._log(f"发送失败(可能在重连): {exc}")

    def _run_loop(self):
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        headers = [f"Authorization: Bearer {self.api_key}"]
        while self._should_run:
            try:
                self._ws = websocket.WebSocketApp(
                    url, header=headers,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=lambda ws, e: self._log(f"连接错误: {e}"))
                self._ws.run_forever(**self._proxy_kwargs())
            except Exception as exc:
                self._log(f"连接异常: {exc}")
            if not self._should_run:
                break
            self._log(f"连接断开，{self._backoff}秒后重连")
            self._status("RECONNECTING")
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 30)

    def _watchdog(self):
        while self._should_run:
            time.sleep(1)
            if (self.wake_enable and self._awake and self._last_active_ts
                    and not self._is_responding and not self._playing
                    and time.time() - self._last_active_ts > self.wake_window_s):
                self._awake = False
                self._log(f"超过{self.wake_window_s}秒无对话，进入待机")
                self._status("STANDBY")
                if self.standby_announce:
                    msg = _STANDBY_MESSAGES.get(self.language, _STANDBY_MESSAGES["中文"])
                    self._send({"type": "response.create", "response": {
                        "conversation": "none",
                        "input": [],   # 显式清空上下文，否则播报会被对话历史带偏
                        "output_modalities": ["audio"],
                        "instructions": f"直接说出下面这句话，一字不差，不要加任何开场白、确认语或额外内容：{msg}"}})

    # ---------- OpenAI 事件 ----------
    def _on_open(self, ws):
        self._backoff = 2
        try:
            ws.send(json.dumps(self._session_update()))
        except Exception as exc:
            self._log(f"会话配置失败: {exc}")
        self._ready.set()
        self._suppress_audio = False
        sc = self._scene()
        self._log(f"已连接 OpenAI | 场景={sc.get('name','默认')} 语言={self.language} "
                  f"唤醒={self.wake_enable} 打断需唤醒词={self.interrupt_requires_wake}")
        self._status("STANDBY" if (self.wake_enable and not self._awake) else "IDLE")

    def _discard_item(self, item_id):
        if item_id:
            self._send({"type": "conversation.item.delete", "item_id": item_id})

    def _handle_utterance(self, transcript, item_id=""):
        now = time.time()
        norm_t = _normalize(transcript)
        norm_p = _normalize(self._transcribe_prompt())
        if len(norm_t) >= 10 and (norm_t in norm_p or norm_p in norm_t):
            self._log("[忽略] 转写回显引导提示")
            self._discard_item(item_id)
            return
        if self.wake_enable and self._awake and self._last_active_ts \
                and now - self._last_active_ts > self.wake_window_s:
            self._awake = False
            self._status("STANDBY")
        if self.wake_enable and not self._awake:
            if not any(_normalize(w) in norm_t for w in WAKE_WORDS):
                self._log("[待机忽略] 无唤醒词")
                self._discard_item(item_id)
                return
            self._awake = True
            self._log("[唤醒] 进入对话模式")
        if FILTER_FILLER and is_filler(transcript):
            self._log("[忽略语气词]")
            self._discard_item(item_id)
            return
        # 打断门槛：它正在说话（生成中/还在播）时，只有含唤醒词的话才允许打断；
        # 其它话当没听见（并从历史删除，防止说完后被"补答"）。说完后的空闲期不受此限制。
        if self.interrupt_requires_wake and (self._is_responding or self._playing):
            if not any(_normalize(w) in norm_t for w in WAKE_WORDS):
                self._log("[说话中忽略] 未喊唤醒词，不打断")
                self._discard_item(item_id)
                return
        self._last_active_ts = now
        self._interrupt_playback()
        if self._is_responding:
            self._send({"type": "response.cancel"})
            self._is_responding = False
        self._send({"type": "response.create"})

    def _tavily(self, query):
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": 5, "include_answer": True},
                headers={"Authorization": f"Bearer {self.tavily_key}"},
                proxies={"http": f"http://{self.proxy}", "https": f"http://{self.proxy}"} if self.proxy else None,
                timeout=20)
            resp.raise_for_status()
            data = resp.json()
            parts = []
            if data.get("answer"):
                parts.append("摘要：" + data["answer"])
            for r in data.get("results", [])[:5]:
                parts.append(f"- {r.get('title','')}：{(r.get('content','') or '')[:200]}")
            return "\n".join(parts) or "没有搜到相关结果。"
        except Exception as exc:
            return f"搜索失败：{exc}"

    def _run_tool(self, name, call_id, arguments):
        def run():
            try:
                args = json.loads(arguments or "{}")
            except Exception:
                args = {}
            if name == "web_search":
                q = args.get("query", "")
                self._log(f"联网搜索: {q}")
                result = self._tavily(q)
            elif name == "search_knowledge_base":
                q = args.get("query", "")
                lang = args.get("language", "") or kb_module.detect_language(q)
                sc = self._scene()
                self._log(f"查知识库: 场景={sc.get('name')} 语言={lang} 问题={q}")
                try:
                    result = kb_module.kb_answer(q, kb_module.scene_kb_dir(sc, lang),
                                                 self.api_key, self.proxy)
                    # 展厅场景 + 问"展板" → 只在这一次回答后提示可切换场景（别的回答不带）
                    if self.scene_key == "showroom" and any(w in (q or "").lower() for w in _PANEL_WORDS):
                        result += _PANEL_FOLLOWUP.get(lang, _PANEL_FOLLOWUP["zh"])
                except Exception as exc:
                    result = f"知识库查询失败：{exc}"
            elif name == "switch_scene":
                target = args.get("scene", "")
                if target in self.scenes_cfg["scenes"]:
                    self.scene_key = target
                    self._send(self._session_update())   # 热更新人设+工具
                    name_cn = self._scene().get("name", target)
                    self._log(f"[场景切换] → {name_cn}")
                    self.send_json({"type": "scene", "value": name_cn})
                    result = f"已切换到『{name_cn}』场景。请用一句话向用户确认（用户用日语就用日语说）。"
                else:
                    result = f"没有场景 {target}"
            elif name == "enter_standby":
                self._awake = False
                self._status("STANDBY")
                self._log("[待机] 口头指令")
                result = ("已进入待机状态。请用一句话向用户确认并告别"
                          "（例如：好的，我先休息啦，需要我时喊『你好小灵』就行）。之后保持安静。")
            else:
                result = "未知工具"
            self._send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id, "output": result}})
            if self._is_responding:
                self._send({"type": "response.cancel"})
            self._send({"type": "response.create"})
        threading.Thread(target=run, daemon=True).start()

    def _on_message(self, ws, message):
        e = json.loads(message)
        t = e.get("type", "")
        if t in ("response.output_audio.delta", "response.audio.delta"):
            if self._suppress_audio:
                return
            self._playing = True
            self._status("SPEAKING")
            try:
                self.send_audio(base64.b64decode(e["delta"]))
            except Exception:
                pass
        elif t == "response.created":
            self._is_responding = True
            self._suppress_audio = False
        elif t == "conversation.item.input_audio_transcription.completed":
            transcript = e.get("transcript", "").strip()
            self._log(f"你: {transcript}")
            self.send_json({"type": "user_text", "text": transcript})
            self._handle_utterance(transcript, e.get("item_id", ""))
        elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            text = e.get("transcript", "").strip()
            if text:
                self._log(f"小灵: {text}")
                self.send_json({"type": "bot_text", "text": text})
        elif t == "response.done":
            self._is_responding = False
            self._last_active_ts = time.time()
            calls = [it for it in e.get("response", {}).get("output", [])
                     if it.get("type") == "function_call"]
            for it in calls:
                self._run_tool(it.get("name"), it.get("call_id"), it.get("arguments"))
            if not calls:
                self._status("STANDBY" if (self.wake_enable and not self._awake) else "IDLE")
        elif t == "error":
            self._log("OpenAI 错误: " + json.dumps(e.get("error", {}), ensure_ascii=False))
