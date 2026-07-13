# 小灵语音助手 · 网页版（web_voice）

浏览器打开网页即可语音对话的测试服务。功能与桌面原型（`../desktop_test/`）一致：
唤醒词/待机播报/口头待机、场景切换（展厅/银座/清水寺）、双语知识库、联网检索、
天气查询（默认城市跟随场景）、打断需唤醒词、语气词/幻觉过滤、断线自动重连。

```
测试者浏览器（采麦克风/放语音/看文字） ←WebSocket→ 本服务(server.py) ←→ OpenAI Realtime
```

## 文件
- `server.py` — FastAPI 后端（网页 + WebSocket）
- `session.py` — 会话逻辑（每个浏览器连接一个独立会话；唤醒词/VAD/过滤等行为参数在文件顶部常量区）
- `static/index.html` — 网页前端（单文件，无需构建）
- `interrupt_test.py` — 打断门槛离线测试；`ws_test.py` / `lang_test.py` — 在线全链路测试
- 复用 `../core/` 的 `kb.py`、`weather.py`、`scenes.json` 和 `../知识库/`（同一份，不要拷两份）
- 配置读**项目根**的 `../config.json`（key/代理/端口/语言/唤醒）

## 本机运行（开发/自测）
```
pip install -r requirements.txt
python server.py
```
浏览器打开 http://localhost:8000 → 点「开始对话」→ 允许麦克风 → 喊「你好小灵」。
（localhost 豁免 HTTPS 限制，本机测试不需要证书。）

## 部署 / 更新 / 排错

**全部见项目根的 [`../DEPLOY.md`](../DEPLOY.md)**（服务器信息速查、首次部署步骤、
systemd 常驻、更新发布流程、配置详解、故障排查 Q1~Q14）。
功能说明与测试指南见 [`../FEATURES.md`](../FEATURES.md)。
