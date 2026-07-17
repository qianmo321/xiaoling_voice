# -*- coding: utf-8 -*-
"""
本地知识库（简介路由 + 长上下文，不做 RAG 切片）——多场景/双语版
----------------------------------------------------------------
目录结构：知识库/<场景>/<语言zh|ja>/*.md|*.txt，每个目录独立一份 kb_index.json
原理：
  入库：LLM 给每个文件生成"检索用简介"，存该目录的 kb_index.json
  提问：所有简介+问题 → 小模型路由选文件 → 选中文件【全文】塞长上下文 → 大模型作答

用法（命令行）：
  python kb.py build                    # 按 scenes.json 把所有 场景×语言 目录全部建索引
  python kb.py list                     # 查看各场景已入库的文件
  python kb.py ask <场景> <zh|ja> 问题   # 测试某场景某语言的问答，如: python kb.py ask mall zh 有什么美食
"""
import os
import sys
import json
import hashlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

# ============ 配置 ============
_HERE = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.normpath(os.path.join(_HERE, "..", "知识库"))   # 知识库根目录
SCENES_PATH = os.path.join(_HERE, "scenes.json")                  # 场景配置
EXTS = {".md", ".txt"}
SUMMARY_MODEL = "gpt-4o-mini"   # 生成简介（便宜）
ROUTE_MODEL = "gpt-4o-mini"     # 路由选文件（便宜、快）
ANSWER_MODEL = "gpt-4o"         # 读全文作答（长上下文）
MAX_DOC_CHARS = 120_000
# ==============================


def load_scenes():
    with open(SCENES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def scene_kb_dir(scene_cfg, language):
    """场景配置 + 语言(zh/ja) → 知识库目录绝对路径。"""
    rel = scene_cfg.get("kb_ja" if language == "ja" else "kb_zh", "")
    return os.path.join(KB_ROOT, rel) if rel else ""


def detect_language(text):
    """兜底的语言检测：含平假名/片假名 → ja，否则 zh。"""
    for ch in text or "":
        if "぀" <= ch <= "ヿ":
            return "ja"
    return "zh"


def _chat(messages, model, api_key, proxy, timeout=60):
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json={"model": model, "messages": messages, "temperature": 0.2},
        headers={"Authorization": f"Bearer {api_key}"},
        proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _index_path(kb_dir):
    return os.path.join(kb_dir, "kb_index.json")


def _load_index(kb_dir):
    p = _index_path(kb_dir)
    if p and os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_index(kb_dir, index):
    with open(_index_path(kb_dir), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _read_file(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def list_index_files(kb_dir):
    try:
        return list(_load_index(kb_dir).keys())
    except Exception:
        return []


def build_index(kb_dir, api_key, proxy):
    """给一个目录建索引：新增/变更的文件生成简介，没变的跳过。"""
    if not kb_dir or not os.path.isdir(kb_dir):
        return f"目录不存在: {kb_dir}"
    index = _load_index(kb_dir)
    seen = set()
    logs = []
    for name in sorted(os.listdir(kb_dir)):
        path = os.path.join(kb_dir, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(path) or ext not in EXTS:
            continue
        seen.add(name)
        text = _read_file(path)
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        if name in index and index[name].get("hash") == digest:
            logs.append(f"  [跳过] {name}（内容没变）")
            continue
        logs.append(f"  [生成简介] {name}（{len(text)}字）...")
        summary = _chat(
            [{"role": "system", "content":
              "给这份文档写一段『检索用简介』：说明文档主题、覆盖哪些内容和关键词、能回答哪类问题。"
              "若文档开头有『用途』『指代说明／指示語』等使用说明，必须把其中的触发词和指代词"
              "（例如“旁边的展板”“隣のパネル”“このパネル”）原样写进简介，方便按这些说法检索命中。"
              "250字以内，用文档本身的语言（中文文档用中文、日文文档用日文），直接输出简介本身。"},
             {"role": "user", "content": f"文件名：{name}\n\n{text[:MAX_DOC_CHARS]}"}],
            SUMMARY_MODEL, api_key, proxy,
        )
        index[name] = {"hash": digest, "summary": summary}
    for name in [n for n in index if n not in seen]:
        del index[name]
        logs.append(f"  [移除] {name}（文件已删除）")
    _save_index(kb_dir, index)
    logs.append(f"  完成：{len(index)} 个文件")
    return "\n".join(logs)


def build_all(api_key, proxy):
    """按 scenes.json 把所有 场景×语言 目录全部建索引。"""
    scenes = load_scenes()["scenes"]
    out = []
    for key, sc in scenes.items():
        for lang in ("zh", "ja"):
            d = scene_kb_dir(sc, lang)
            out.append(f"[{sc.get('name', key)} / {lang}] {d}")
            out.append(build_index(d, api_key, proxy))
    return "\n".join(out)


def kb_answer(question, kb_dir, api_key, proxy):
    """路由选文件 → 全文作答。回答语言跟随提问语言。"""
    index = _load_index(kb_dir)
    if not index:
        return "知识库是空的（该场景/语言目录还没建索引）。"

    listing = "\n\n".join(
        f"文件名: {name}\n简介: {info['summary']}" for name, info in index.items())
    try:
        route_raw = _chat(
            [{"role": "system", "content":
              "你是文档路由器。这些文件都是当前服务场景（某公司/商场/景点）的资料。"
              "根据用户问题和各文件的简介，选出可能相关的文件。规则："
              "1) 宁可多选、不要漏选：只要问题是在询问该场景主体的任何信息"
              "（哪怕简介里没明确提到那个细节，如某个编号、某项费用），也要选上最可能包含它的文件；"
              "2) 用户常用指代词提问（如『旁边的展板』『隣のパネル』『この施設』），"
              "这类问题要选中对应的介绍文档，不要因为措辞对不上而漏选；"
              "3) 只有与场景完全无关的问题（如闲聊、天气、别处的事）才输出 []。"
              '只输出一个 JSON 数组（文件名），例如 ["a.md"]。不要输出别的。'},
             {"role": "user", "content": f"{listing}\n\n用户问题: {question}"}],
            ROUTE_MODEL, api_key, proxy,
        )
        chosen = [n for n in json.loads(route_raw) if n in index]
    except Exception:
        chosen = list(index.keys())

    if not chosen:
        return "知识库里没有和这个问题相关的内容。"

    docs, used = [], 0
    for name in chosen:
        p = os.path.join(kb_dir, name)
        text = _read_file(p) if os.path.exists(p) else ""
        take = text[: max(0, MAX_DOC_CHARS - used)]
        used += len(take)
        docs.append(f"【文档：{name}】\n{take}")
        if used >= MAX_DOC_CHARS:
            break

    return _chat(
        [{"role": "system", "content":
          "你是语音助手的知识库问答模块。只根据提供的文档内容回答；文档里没有的就明说不知道，不要编造。"
          "用户常用指代词提问（如'旁边的展板''这个设施''隣のパネル'）：只要文档描述的就是该类对象，"
          "就直接按文档介绍，不要因为指代词对不上而说不知道。"
          "用和用户问题相同的语言回答（中文问题用中文答，日语问题用日语答）。"
          "口语化、适合直接朗读，尽量简洁（一般120字以内，需要列举时可以适当多）。"
          "长数字串（编号、法人番号、电话号码等5位以上的数字）必须逐位书写、位与位之间用空格隔开，"
          "例如 7010001235851 要写成「7 0 1 0 0 0 1 2 3 5 8 5 1」，方便语音逐位朗读不出错。"},
         {"role": "user", "content": "\n\n".join(docs) + f"\n\n【用户问题】{question}"}],
        ANSWER_MODEL, api_key, proxy, timeout=90,
    )


# ---------------- 命令行入口 ----------------
def _load_keys():
    """key/代理 读项目根的 config.json（不入 git 的那份）。"""
    root_cfg = os.path.normpath(os.path.join(_HERE, "..", "config.json"))
    with open(root_cfg, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["openai"]["api_key"], cfg.get("network", {}).get("proxy", "")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    key, proxy = _load_keys()
    if cmd == "build":
        print(build_all(key, proxy))
    elif cmd == "list":
        scenes = load_scenes()["scenes"]
        for k, sc in scenes.items():
            for lang in ("zh", "ja"):
                d = scene_kb_dir(sc, lang)
                print(f"\n=== {sc.get('name', k)} / {lang} ===")
                for name, info in _load_index(d).items():
                    print(f"  {name}: {info['summary'][:60]}...")
    elif cmd == "ask" and len(sys.argv) > 4:
        scene_key, lang = sys.argv[2], sys.argv[3]
        q = " ".join(sys.argv[4:])
        sc = load_scenes()["scenes"].get(scene_key)
        if not sc:
            print(f"没有场景 {scene_key}，可选: showroom / mall / temple")
        else:
            d = scene_kb_dir(sc, lang)
            print(f"场景: {sc['name']} | 语言: {lang} | 问题: {q}")
            print("-" * 40)
            print(kb_answer(q, d, key, proxy))
    else:
        print("用法: python kb.py build | list | ask <场景> <zh|ja> 问题")
