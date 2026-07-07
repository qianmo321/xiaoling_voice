# 小灵语音助手 · 网页版（web_voice）

浏览器打开网页即可语音对话的测试服务。功能与桌面原型一致：
唤醒词/待机播报/口头待机、场景切换（展厅/商场/清水寺）、双语知识库、联网检索、打断、语气词过滤。

```
测试者浏览器（采麦克风/放语音/看文字） ←WebSocket→ 本服务(server.py) ←→ OpenAI Realtime
```

## 文件
- `server.py` — FastAPI 后端（网页 + WebSocket）
- `session.py` — 会话逻辑（每个浏览器连接一个会话；行为参数在文件顶部）
- `static/index.html` — 网页前端（单文件，无需构建）
- `config.json` — key/代理/端口/语言/唤醒 配置
- 复用 `../realtime_test/` 的 `kb.py`、`scenes.json` 和 `../知识库/`（同一份，不要拷两份）

## 本机运行（开发/自测）
```
pip install -r requirements.txt
python server.py
```
浏览器打开 http://localhost:8000 → 点「开始对话」→ 允许麦克风 → 喊「你好小灵」。
（localhost 豁免 HTTPS 限制，本机测试不需要证书。）

## 部署到 Ubuntu 服务器
1) 环境：
```bash
sudo apt install -y python3-pip git
git clone <你的仓库> && cd lingze_omni_s2s/web_voice
pip3 install -r requirements.txt
```
2) 配置 `config.json`：填 key；`network.proxy` 填**服务器自己**的代理（能直连就留 ""）。
3) 建知识库索引：
```bash
cd ../realtime_test && python3 kb.py build && cd ../web_voice
```
4) ⚠️ 生成自签名 HTTPS 证书（浏览器规定：非 localhost 必须 HTTPS 才给用麦克风）：
```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout key.pem -out cert.pem -subj "/CN=voice-test"
```
5) 启动（带证书）：
```bash
python3 -m uvicorn server:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```
6) 测试者访问 `https://服务器IP:8443` → 首次会提示"不安全"（自签证书），点「高级→继续前往」→ 允许麦克风即可。

### 常驻运行（systemd，开机自启+崩溃自拉起）
`/etc/systemd/system/voice-web.service`：
```ini
[Unit]
Description=Voice Web Service
After=network.target

[Service]
WorkingDirectory=/home/<user>/lingze_omni_s2s/web_voice
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now voice-web
```

## 日常修改流程
| 改什么 | 怎么做 |
|--------|--------|
| 场景人设/别名/通用规则 | 改 `../realtime_test/scenes.json` → 重启服务 |
| 知识库文档 | 丢进 `../知识库/<场景>/<zh|ja>/` → `python3 kb.py build` → 重启 |
| key/代理/语言/唤醒窗口 | 改 `config.json` → 重启 |
| 唤醒词/VAD/过滤等行为参数 | 改 `session.py` 顶部常量 → 重启 |
| 代码逻辑 | 本地改+测（localhost）→ `git push` → 服务器 `git pull` → `sudo systemctl restart voice-web` |

## 排错
| 现象 | 处理 |
|------|------|
| 浏览器不给麦克风权限 | 必须 https（或 localhost）；检查证书步骤 |
| 网页连上但助手没反应 | 看服务器日志：多为服务器连不上 OpenAI（代理没配对） |
| WebSocket 404 / Unsupported upgrade | `pip3 install websockets`（uvicorn 的 WS 支持库） |
| 手机 iOS 没声音 | 先点一次「开始对话」按钮（iOS 要求用户手势后才能播放） |
