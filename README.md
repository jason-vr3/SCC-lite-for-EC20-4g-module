# SCC-lite-for-EC20-4g-module

> **AI 辅助开发声明 / AI-Assisted Development Notice**
>
> 本项目由 AI 辅助开发 (Muse, Meta)，经真机实测验证。
> This project was developed with AI assistance (Muse, Meta) and verified on real hardware.

短信控制中心 - 4G 模块短信收发与推送网关
SMS Control Centre - 4G module SMS gateway with push notifications

---

## 简介 / Introduction

SCC-lite 是运行在 Linux 盒子（如斐讯 N1 / Phicomm N1）上的轻量短信网关，
通过 Quectel EC20/EC25 等 4G 模块收发短信，并可推送到 QQ / Telegram / Webhook / Bark。

SCC-lite is a lightweight SMS gateway for Linux boxes (e.g. Phicomm N1),
sending/receiving SMS via Quectel EC20/EC25 4G modules,
with push notifications to QQ / Telegram / Webhook / Bark.

> 为替代 VoHiveX 而写——VoHiveX 2.1.4 的短信轮询在 Quectel EC20 上不工作（worker 空转、无 `AT+CMGL`），
> 而原生 AT 指令经实机验证完全可用。本项目只做验证过的功能。
>
> Written as a VoHiveX replacement — VoHiveX 2.1.4's SMS polling doesn't work on Quectel EC20
> (worker idles, no `AT+CMGL`), while native AT commands are verified working on real hardware.

### 主要功能 / Features

- 📩 **短信收发**：AT 指令收发短信，中文 UCS2 / 英文 GSM 自动切换
  SMS send/receive via AT commands, auto UCS2/GSM switching
- 💬 **iOS 风格会话界面**：左侧联系人列表，右侧气泡会话
  iOS-style conversation UI
- 🔔 **消息推送**：新短信自动推送到 QQ Bot / Telegram / Webhook / Bark
  Auto push new SMS to QQ Bot / Telegram / Webhook / Bark
- 🔁 **可靠转发**：SQLite 持久化队列，失败指数退避重试，去重保证 at-least-once
  Reliable forwarding: persistent queue, exponential backoff retry, dedup
- 🤖 **QQ OpenID 自动抓取**：WebSocket 直连 QQ 网关（无需公网），收到 QQ 消息自动记录 openid
  Auto capture QQ openid via WebSocket (no public IP needed)
- 💾 **备份**：JSON 全量/单号导出导入
  Backup: JSON full/per-contact export/import
- 🗑️ **删除**：删除整个会话或单条短信
  Delete: whole conversation or single message
- 🔢 **字数统计**：中文 70 / 英文 160 字符计数
  Character count: 70 for Chinese, 160 for English
- 📊 **设备监控**：IMEI/IMSI/ICCID、信号 RSSI、网络注册、运营商、QMI 数据控制
  Device monitoring: IMEI/IMSI/ICCID, signal, registration, carrier, QMI data control
- 🔧 **AT 终端 / USSD / 飞行模式**：Web 直接操作模块
  AT terminal / USSD / airplane mode via Web UI

### 已知限制 / Known Limitations

- **长短信**：超过 70 字中文 / 160 字英文的长短信发送仍有问题，已冻结，计划下个版本解决
  **Long SMS**: sending messages over 70 Chinese chars / 160 English chars still has issues, frozen for now, will fix in next version

---

## 运行环境 / Requirements

- Linux（实测 Ubuntu 24.04 noble, aarch64，也支持 armv7）
  Linux (tested on Ubuntu 24.04 noble, aarch64; armv7 also supported)
- Quectel EC20/EC25 4G 模块（USB 串口）
  Quectel EC20/EC25 4G module (USB serial)
- Python 3.10+
- 依赖：pyserial, flask, pyyaml, websocket-client
  Dependencies: pyserial, flask, pyyaml, websocket-client

### 串口分配 / Serial Port Assignment

| 串口 / Port | 用途 / Purpose |
|---|---|
| `/dev/ttyUSB2` | Asterisk 语音 (chan_quectel) / Voice - 勿动 / Don't touch |
| `/dev/ttyUSB3` | SCC-lite 短信 AT / SMS AT commands |
| `/dev/cdc-wdm0` | QMI 数据 / QMI data |

---

## 快速开始 / Quick Start

```bash
# 1. 解压 / Extract
tar xzf scc-lite-for-EC20-4g-module-v0.5.0.tar.gz
cd scc-lite-for-EC20-4g-module

# 2. 安装 / Install
sudo ./install.sh

# 3. 启动 / Start
sudo systemctl start scc-lite
sudo systemctl start scc-web

# 4. 打开 Web 界面 / Open Web UI
# http://<你的IP>:7577  (默认 admin/admin，请立即修改密码！)
# http://<your-IP>:7577  (default admin/admin, change it immediately!)
```

### 配置消息推送 / Configure Notifications

网页 → 消息推送 → 填写对应渠道配置 → 保存 → 测试推送

Web UI → Notifications → fill in channel config → Save → Test

- **QQ Bot**：填 AppID/AppSecret，启用后给机器人发条 QQ 消息，
  点"🔍 查看抓到的 OpenID" → 填入 → 保存
  Fill AppID/AppSecret, send a QQ message to the bot,
  click "View captured OpenID" → fill in → save
- **Telegram**：用 @BotFather 建机器人拿 token，
  给机器人发条消息后用 `getUpdates` API 拿 chat_id
  Create bot via @BotFather for token,
  send the bot a message then get chat_id via `getUpdates` API

---

## AT 指令依据 / AT Command References

所有指令出自以下公开标准/手册，`modem.py` 中每条均有注释：

All commands from public standards/manuals, each documented in `modem.py`:

| 指令 / Command | 用途 / Purpose | 出处 / Source |
|------|------|------|
| `AT+CGSN` | IMEI | 3GPP TS 27.007 §5.4 |
| `AT+CIMI` | IMSI | 3GPP TS 27.007 §5.6 |
| `AT+QCCID` | ICCID | Quectel EC2x AT 手册 |
| `AT+CSQ` | 信号 / Signal | 3GPP TS 27.007 §8.5 |
| `AT+CREG?` | 网络注册 / Registration | 3GPP TS 27.007 §7.2 |
| `AT+COPS?` | 运营商 / Operator | 3GPP TS 27.007 §7.3 |
| `AT+CFUN=0/1` | 飞行模式 / Airplane mode | 3GPP TS 27.007 §7.11 |
| `AT+CMGF=1` | 短信文本模式 / Text mode | 3GPP TS 27.007 §7.7 |
| `AT+CSCS` | 字符集 / Charset | 3GPP TS 27.007 §5.5 |
| `AT+CMGL` | 列短信 / List SMS | 3GPP TS 27.007 §7.7 |
| `AT+CMGR=<i>` | 读短信 / Read SMS | 3GPP TS 27.007 §7.7 |
| `AT+CMGD=<i>` | 删短信 / Delete SMS | 3GPP TS 27.007 §7.7 |
| `AT+CMGS` | 发短信 / Send SMS | 3GPP TS 27.007 §7.7 |
| `AT+CUSD` | USSD | 3GPP TS 27.007 §7.15 |
| `AT+CNMI=2,1` | 新短信通知 / New SMS URC | 3GPP TS 27.007 §7.7 |

通知渠道 API / Notification APIs：
- QQ Bot：https://bot.q.qq.com/wiki/（`getAppAccessToken` → v2 `/users/{openid}/messages`；WebSocket 接收参考官方"事件订阅与通知"文档）
- Telegram：https://core.telegram.org/bots/api#sendmessage
- Bark：https://github.com/Finb/Bark

---

## 项目结构 / Project Structure

```
scc-lite-for-EC20-4g-module/
├── scc-lite.py       # 短信守护进程：轮询/入库/转发队列 (daemon)
├── scc-web.py        # Web 管理界面 (Flask, :7577)
├── modem.py          # AT 指令封装 (3GPP TS 27.007 / Quectel)
├── notifications.py  # 推送渠道 (QQ/Telegram/Webhook/Bark)
├── qq_receiver.py    # QQ WebSocket 接收 (抓 openid)
├── data_control.py   # QMI 数据连接控制
├── config.yaml       # 配置文件
├── install.sh        # 一键安装脚本
├── scc-lite.service  # systemd 服务
├── scc-web.service   # systemd 服务
└── requirements.txt  # Python 依赖
```

---

## 开源许可 / License

MIT License - 详见 LICENSE 文件 / See LICENSE file.
个人免费使用 / Free for personal use.

---

## 致谢 / Acknowledgments

- AT 指令参考：3GPP TS 27.007, Quectel EC2x AT Commands Manual
- 开源实现参考：macsatcom/sms-gateway-sim7600, Gammu, smstools3
- QQ Bot 文档：https://bot.q.qq.com/wiki/
