# -*- coding: utf-8 -*-
"""
场景切换自动化验证（文字版，不用麦克风）。
流程：默认场景=机器人展厅 → 说"切换到商场导览模式" → 验证模型调 switch_scene
     → 切换后问"停车场怎么收费" → 验证知识库查的是【商场】场景。
"""
import os, sys, json, threading, importlib.util
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import websocket

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ort", os.path.join(_here, "openai_realtime_test.py"))
ort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ort)
import kb as kb_module

URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
HEADERS = [f"Authorization: Bearer {ort.API_KEY}"]

SCENES = kb_module.load_scenes()
state = {
    "scene": SCENES["default_scene"],
    "step": 0,
    "switched": False,
    "kb_scene_used": "",
    "answers": [],
}
done = threading.Event()
_lock = threading.Lock()


def instructions():
    sc = SCENES["scenes"][state["scene"]]
    return sc["instructions"] + "\n\n" + SCENES.get("common_rules", "")


def tools():
    sc = SCENES["scenes"][state["scene"]]
    kb_tool = {
        "type": "function", "name": "search_knowledge_base",
        "description": f"查询当前场景（{sc['name']}）的本地知识库并给出答案。用户问到公司/设施/景点等相关内容时必须调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "language": {"type": "string", "enum": ["zh", "ja"]}},
            "required": ["query"]},
    }
    sw_tool = {
        "type": "function", "name": "switch_scene",
        "description": ("切换助手的工作场景。用户说『切换到XX模式』时调用。可选：" + "；".join(
            f"{k}={v['name']}（别名：{'、'.join(v.get('aliases', []))}）" for k, v in SCENES["scenes"].items())),
        "parameters": {"type": "object", "properties": {
            "scene": {"type": "string", "enum": list(SCENES["scenes"].keys())}},
            "required": ["scene"]},
    }
    return [kb_tool, sw_tool]


def session_update():
    return {"type": "session.update", "session": {
        "type": "realtime", "output_modalities": ["text"],
        "instructions": instructions(),
        "tools": tools(), "tool_choice": "auto"}}


def say(ws, text):
    print(f"\n>>> 用户: {text}")
    with _lock:
        ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}))
        ws.send(json.dumps({"type": "response.create"}))


def pk():
    p = ort.PROXY
    if not p:
        return {}
    u = urlparse(p if "://" in p else "http://" + p)
    return {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 80, "proxy_type": "http"}


def on_open(ws):
    print(f"[连接] 默认场景: {SCENES['scenes'][state['scene']]['name']}")
    with _lock:
        ws.send(json.dumps(session_update()))
    say(ws, "切换到商场导览模式")


def on_message(ws, message):
    e = json.loads(message)
    t = e.get("type", "")
    if t == "response.output_text.done":
        state["answers"].append(e.get("text", ""))
    elif t == "response.done":
        calls = [it for it in e.get("response", {}).get("output", []) if it.get("type") == "function_call"]
        if calls:
            for it in calls:
                try:
                    args = json.loads(it.get("arguments") or "{}")
                except Exception:
                    args = {}
                name = it.get("name")
                if name == "switch_scene":
                    target = args.get("scene", "")
                    print(f"[工具] 模型调用 switch_scene → {target}")
                    if target in SCENES["scenes"]:
                        state["scene"] = target
                        state["switched"] = True
                        with _lock:
                            ws.send(json.dumps(session_update()))   # 热更新人设+工具
                        result = f"已切换到『{SCENES['scenes'][target]['name']}』场景，请用一句话向用户确认。"
                    else:
                        result = f"没有场景 {target}"
                elif name == "search_knowledge_base":
                    q = args.get("query", "")
                    lang = args.get("language", "") or kb_module.detect_language(q)
                    sc = SCENES["scenes"][state["scene"]]
                    state["kb_scene_used"] = state["scene"]
                    state.setdefault("kb_langs", []).append(lang)
                    print(f"[工具] 模型调用 search_knowledge_base → 场景={sc['name']} 语言={lang} 问题={q}")
                    result = kb_module.kb_answer(q, kb_module.scene_kb_dir(sc, lang), ort.API_KEY, ort.PROXY)
                    print(f"[知识库] {result[:80]}...")
                else:
                    result = "未知工具"
                with _lock:
                    ws.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": it.get("call_id"), "output": result}}))
            with _lock:
                ws.send(json.dumps({"type": "response.create"}))
        else:
            # 一轮文字回答结束 → 推进测试步骤
            if state["answers"]:
                print(f"<<< 小灵: {state['answers'][-1]}")
            state["step"] += 1
            if state["step"] == 1:
                say(ws, "你们商场停车场怎么收费？")
            elif state["step"] == 2:
                say(ws, "営業時間は何時までですか？")   # 日语提问 → 应走日文知识库
            else:
                done.set()
    elif t == "error":
        print("[错误]", json.dumps(e.get("error", {}), ensure_ascii=False))
        done.set()


ws = websocket.WebSocketApp(URL, header=HEADERS, on_open=on_open, on_message=on_message)
threading.Thread(target=lambda: ws.run_forever(**pk()), daemon=True).start()
ok = done.wait(timeout=120)
ws.close()

print("\n" + "=" * 50)
langs = state.get("kb_langs", [])
print("场景切换被触发 :", "✅" if state["switched"] else "❌")
print("切换后场景     :", SCENES["scenes"][state["scene"]]["name"])
print("知识库用的场景 :", SCENES["scenes"].get(state["kb_scene_used"], {}).get("name", "(没调用)"))
print("知识库语言路由 :", langs, "（期待: 先zh后ja）")
verdict = state["switched"] and state["kb_scene_used"] == "mall" and "zh" in langs and "ja" in langs
print(">>> 结果:", "全链路通过 ✅" if verdict else ("超时 ❌" if not ok else "未完全通过 ❌"))
