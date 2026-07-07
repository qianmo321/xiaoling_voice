# xiaoling_voice · 小灵语音助手

面向导览/问答场景的实时语音助手（OpenAI Realtime 端到端）。
功能：唤醒词与待机、场景切换（机器人展厅/商场/清水寺）、中日双语、
本地知识库（简介路由+长上下文，不切片）、Tavily 联网检索、打断、语气词/杂音过滤。

## 目录结构
```
xiaoling_voice/
├── config.json          ← 真实密钥配置（不入 git！首次使用从模板复制）
├── config.example.json  ← 配置模板
├── core/                ← 共享核心：kb.py（知识库）、scenes.json（场景定义）
├── 知识库/               ← 知识库文档（按 场景/语言 分目录，含索引）
├── web_voice/           ← ★ 网页版服务（部署主体，浏览器即可语音对话）
└── desktop_test/        ← Windows 桌面原型（开发调参用，带离线测试脚本）
```

## 快速开始（网页版）
```bash
# 1. 配置
cp config.example.json config.json    # 填 openai key / tavily key / 代理

# 2. 依赖
pip install -r web_voice/requirements.txt

# 3. 知识库索引（文档变更后重跑）
cd core && python kb.py build && cd ..

# 4. 启动
cd web_voice && python server.py
```
浏览器打开 http://localhost:8000 → 「开始对话」→ 喊「你好小灵」。
服务器部署（HTTPS 证书、systemd 常驻、排错）见 `web_voice/README.md`。

## 日常修改
| 改什么 | 位置 |
|--------|------|
| 场景人设/别名/通用规则 | `core/scenes.json` → 重启 |
| 知识库文档 | `知识库/<场景>/<zh|ja>/` → `python core/kb.py build` → 重启 |
| key/代理/语言/唤醒窗口/端口 | `config.json` → 重启 |
| 唤醒词/VAD/过滤等行为参数 | `web_voice/session.py` 顶部常量 → 重启 |

## 测试
```bash
cd desktop_test && python wake_logic_test.py     # 唤醒/过滤状态机（离线，31 用例）
cd web_voice   && python ws_test.py              # 网页版全链路（需服务已启动）
cd web_voice   && python lang_test.py            # 中日语言切换（需服务已启动）
```

## 相关
- 机器人版（ROS2 包 openai_s2s）另行管理，接口/逻辑与本仓库一致。
