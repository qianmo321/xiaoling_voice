# 小灵语音助手 · 部署与维护文档

> 适用：xiaoling_voice 网页版语音服务（浏览器语音对话测试台）
> 最后更新：2026-07（首次服务器部署完成后整理）

---

## 1. 系统架构

```
测试者（同内网的电脑/手机浏览器）
   打开 https://192.168.2.80:8444 → 采麦克风、放语音、显示对话与操作日志
        │  WebSocket（音频流 + JSON 消息）
        ▼
Ubuntu 服务器 ubuntu-ai（192.168.2.80）
   systemd 服务 xiaoling → uvicorn → server.py
   每个浏览器连接 = 一个独立 DialogSession（场景/唤醒/知识库/打断，互不影响）
        │  经本机代理 127.0.0.1:10808（服务器上的 v2rayN）
        ▼
OpenAI Realtime API（gpt-realtime）＋ Tavily 联网检索
```

## 2. 当前部署信息速查

| 项 | 值 |
|----|-----|
| 服务器 | `fada@ubuntu-ai`（Ubuntu 24.04，即 4090 那台） |
| 访问地址 | **https://192.168.2.80:8444**（必须 https；自签证书，首次点"高级→继续前往"） |
| 代码目录 | `/home/fada/xiaoling_voice`（GitHub: `qianmo321/xiaoling_voice`，main 分支） |
| Python 环境 | conda env `xiaoling`：`/home/fada/miniconda3/envs/xiaoling/bin/python` |
| systemd 服务名 | `xiaoling`（开机自启、崩溃自动拉起） |
| 密钥配置 | `/home/fada/xiaoling_voice/config.json`（**不在 git 里**，只存在服务器上） |
| 服务器代理 | v2rayN(Linux版) `127.0.0.1:10808`（git 和 OpenAI 都走它） |
| 证书 | `web_voice/key.pem` / `cert.pem`（自签，有效期10年，不在 git 里） |

## 3. 首次部署完整步骤（换新服务器时照这个做）

```bash
# ① 基础工具
sudo apt update && sudo apt install -y git openssl

# ② 拉代码（连不上 GitHub 就先配代理，见第9节Q1）
cd ~ && git clone https://github.com/qianmo321/xiaoling_voice.git
cd xiaoling_voice

# ③ Python 环境（Ubuntu 24.04 禁止 pip 装系统 Python，必须用 conda 或 venv）
# 方式A：miniconda
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda.sh
bash ~/miniconda.sh -b -p ~/miniconda3
~/miniconda3/bin/conda init bash && source ~/.bashrc
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
conda create -n xiaoling python=3.11 -y
conda activate xiaoling
pip install -r web_voice/requirements.txt

# ④ 配置密钥（config.json 不入 git，必须手动建）
cp config.example.json config.json
nano config.json      # 填 openai.api_key / tavily_api_key / network.proxy（见第6节）

# ⑤ 自签 HTTPS 证书（浏览器规定：非 localhost 必须 https 才给麦克风权限）
cd web_voice
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout key.pem -out cert.pem -subj "/CN=xiaoling"

# ⑥ 试运行（确认能起、能对话，再做常驻）
python -m uvicorn server:app --host 0.0.0.0 --port 8444 --ssl-keyfile key.pem --ssl-certfile cert.pem
# 浏览器验证 OK 后 Ctrl+C，继续 ⑦

# ⑦ systemd 常驻（路径按实际 conda env 改，用 `which python` 查）
sudo tee /etc/systemd/system/xiaoling.service > /dev/null <<EOF
[Unit]
Description=Xiaoling Voice Web
After=network-online.target

[Service]
WorkingDirectory=/home/fada/xiaoling_voice/web_voice
ExecStart=/home/fada/miniconda3/envs/xiaoling/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8444 --ssl-keyfile key.pem --ssl-certfile cert.pem
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now xiaoling
systemctl status xiaoling --no-pager | head -5     # 看到 active (running) 即成功
```

> 知识库索引（kb_index.json）随 git 带下来，**不需要**在服务器重建。

## 4. 日常使用（给测试者的说明）

1. 和服务器**同一内网**，浏览器打开 **https://192.168.2.80:8444**；
2. 首次会提示"连接不是私密的" → 点「高级」→「继续前往」（自签证书，正常）；
3. 点「🎙️ 开始对话」→ 允许麦克风；
4. 喊 **「你好小灵」** 唤醒 → 直接对话；30 秒没人说话自动待机（会语音提示）；
5. 常用玩法：
   - "介绍一下IST" / "介绍一下旁边的展板"（知识库）
   - "今天天气怎么样"（天气工具，默认城市跟随场景：展厅=大连/银座=东京银座/清水寺=京都清水寺；只报今天，问"明天"才报未来）
   - "切换到商场导览模式" / "切换到清水寺模式"（场景切换）
   - 右上角 **🌐** 切中/日文，**📜** 开关操作日志
6. 它说话时**普通插话不会打断**，喊"你好小灵"才能打断（有意设计）。

## 5. 更新发布流程

**开发闭环：所有修改在 Windows 的 `xiaoling_voice` 项目里做**（旧 lingze_omni_s2s 只读不写）：

```
Windows 改代码/配置 → 本机测试（python web_voice\server.py + localhost:8000）
→ git add -A && git commit -m "说明" && git push
→ 服务器上两条命令：
     cd ~/xiaoling_voice && git pull && sudo systemctl restart xiaoling
```

改动类型对照：
| 改了什么 | 服务器上要做的 |
|----------|----------------|
| 代码（session/server/前端） | `git pull` + `restart` |
| 场景（core/scenes.json）、知识库文档 | Windows 上改 + `kb.py build` + push；服务器 `pull` + `restart`（索引在 git 里） |
| 服务器专属配置（key/代理/端口） | 直接改服务器 `config.json` + `restart`（不走 git） |

## 6. 配置详解（config.json）

```jsonc
{
  "openai": {
    "api_key": "sk-...",             // OpenAI key（必填）
    "model": "gpt-realtime",         // 端到端语音模型
    "voice": "marin",                // 音色
    "transcribe_model": "gpt-4o-mini-transcribe"   // 转写模型（比whisper-1幻觉少）
  },
  "search": { "enable": true, "tavily_api_key": "tvly-..." },  // 联网检索
  "network": { "proxy": "127.0.0.1:10808" },  // 出网代理；能直连 OpenAI 就填 ""
  "server": { "host": "0.0.0.0", "port": 8000 },  // 注：systemd 启动时端口以命令行 --port 为准
  "language": "中文",                // 主语言：中文/日语（网页上也可每会话切换）
  "wake": {
    "enable": true,                  // 唤醒机制总开关
    "window_s": 30,                  // 唤醒后多少秒无对话回待机
    "standby_announce": true,        // 进待机时语音播报
    "interrupt_requires_wake": true  // 说话中只有唤醒词能打断（false=随时可打断）
  },
  "location": "大连",                // 默认所在地（新闻等本地信息的兜底；各场景的默认天气城市在 core/scenes.json 的 weather 字段里配）
  "mic_gate_rms": 0                  // 音量门：0关闭；越大拾音范围越近（嘈杂环境用）
}
```

行为参数（唤醒词表、VAD、语气词/幻觉过滤词表等）在 `web_voice/session.py` 顶部常量区，改完需走 git 发布。

## 7. 场景与知识库维护

- **场景定义**：`core/scenes.json` —— default_scene、common_rules、每场景的 name/aliases/instructions/kb_zh/kb_ja。加新场景照抄一段 + 建知识库目录。
- **知识库目录**：`知识库/<场景>/<zh|ja>/*.md`。加/改文档后在 Windows 跑：
  ```
  cd core && python kb.py build     # 只处理有变化的文件
  python kb.py ask showroom zh 测试问题     # 离线验证
  ```
  然后 commit+push（索引一起入库），服务器 pull+restart 即生效。
- **特殊机制**：问"展板/パネル"时，回答末尾会自动带"可切换场景"提示（代码里 `_PANEL_WORDS`/`_PANEL_FOLLOWUP`，精准投放，其它回答不带）。

## 8. 运维命令速查（服务器）

```bash
systemctl status xiaoling              # 服务状态
journalctl -u xiaoling -f              # 实时日志（对话过程、[联网搜索] 等都在这）
journalctl -u xiaoling --since "10 min ago"   # 最近10分钟日志
sudo systemctl restart xiaoling        # 重启
sudo systemctl stop / start xiaoling   # 停止 / 启动
curl -x http://127.0.0.1:10808 -m 10 https://api.openai.com/v1/models   # 测代理→OpenAI（返回JSON=通）
```

## 9. 故障排查（都是实际踩过的坑）

| # | 现象 | 原因与解决 |
|---|------|-----------|
| Q1 | `git pull/clone` 报 GnuTLS/超时 | 服务器连 GitHub 时通时断。给仓库配代理：`git config http.proxy http://127.0.0.1:10808`（https.proxy 同）|
| Q2 | 页面能开，说话没反应；日志见 `socket is already closed` | 服务器连不上 OpenAI：config 的 `network.proxy` 不对。先 `curl -x` 测（见第8节）|
| Q3 | 日志报 `invalid_api_key` 且 key 显示乱码 `Ã¥ÂÂ¨...` | config.json 里 key 还是中文占位符没填。用 sed 精确替换：`sed -i 's\|"api_key": ".*"\|"api_key": "sk-真key"\|' config.json` |
| Q4 | 启动报 `address already in use` | 端口被占（本机 8443 被 docker 占）。换端口或 `sudo ss -tlnp \| grep 端口` 查占用者 |
| Q5 | `pip install` 报 `externally-managed-environment` | Ubuntu 24.04 特性，必须用 conda/venv（见第3节③）|
| Q6 | conda create 报 ToS 未接受 | `conda tos accept ...` 两条命令（见第3节③）|
| Q7 | 浏览器不给麦克风权限 | 必须 **https**（或 localhost）。检查证书、地址是不是 https |
| Q8 | 访问超时 | 用错 IP：`hostname -I` 里 172.x 都是 docker 虚拟网卡，要用 **192.168.2.80** |
| Q9 | 手机 iOS 没声音 | iOS 需用户手势后才能播音——先点一次「开始对话」 |
| Q10 | 它把没人说的话当输入（"谢谢观看""您好请问有什么可以帮您"等） | 转写幻觉。已有拦截词表 `_HALLUCINATION_PHRASES`（session.py），出现新花样就往里加整句 |
| Q11 | 待机播报跑题/复读上一话题 | 带外响应必须 `conversation:"none"` **且** `"input": []`（已修，新增播报类功能时注意） |
| Q12 | 服务器重启后连不上 OpenAI | **v2rayN 是桌面程序不会自启**（已知风险，见第10节） |

## 10. 已知风险与待办

1. **服务器代理不自启**：v2rayN(Linux) 是桌面程序，服务器重启后要人工打开它，否则语音服务断外网。待办：把代理核心(xray)做成 systemd 服务。
2. **自签证书**：浏览器永远有"不安全"提示（功能无影响）。要去掉需正式域名+证书。
3. **多人并发成本**：每个浏览器连接是独立 OpenAI 会话，人多时注意 key 额度；页面无口令，勿把地址外传到内网之外。
4. **通用唤醒词已保留**（你好/こんにちは/すみません 等）：打招呼即唤醒更自然，代价是旁人打招呼会误唤醒；嘈杂场合可在 session.py 词表里注释掉，或开音量门 `mic_gate_rms`。
5. **机器人版（ROS2 openai_s2s）**：在 WSL `~/ros2_ws`，落后网页版若干功能（场景/打断门槛等未同步），上真机前需同步。
