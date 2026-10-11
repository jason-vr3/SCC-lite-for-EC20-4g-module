# chan_quectel 截胡短信问题笔记（2026-10-11，N1 实机）

## 背景

- 架构分工：Asterisk/chan_quectel 管语音（`ttyUSB1`/`ttyUSB2` + UAC 音频），
  SCC-lite 管短信与 QMI 数据（独占 `/dev/ttyUSB3`）。串口分工不可互换。
- SCC-lite 收短信靠 15 秒轮询 `AT+CMGL` 读模块存储。

## 症状

Asterisk 与 SCC-lite 同时运行时，短信偶发"人间蒸发"：

- `AT+CMGL` 查不到（没进模块存储）；
- SCC-lite 数据库也没有；
- 15 秒轮询不可能漏掉正常入库的短信，所以不是轮询问题。

之前曾误判"dialplan 没接住，但短信一定仍留在模块"——**这个判断不可靠**，
是本次排障最大的弯路。

## 实锤证据

Asterisk 日志出现：

```text
Channel 'Local/sms@dongle-incoming-0000000f;1' sent to invalid extension
(context,exten,priority=dongle-incoming,sms,1)
```

证明 chan_quectel 拦截了短信事件，并试图送进 `dongle-incoming,sms,1`
（该 extension 不存在所以报 warning）。注意：`chan_dongle` 并未加载，
实际加载的是 `chan_quectel.so`——这是 chan_quectel 自己的行为。

## 根因

`asterisk -rx "quectel show device settings quectel0"` 显示：

```text
Direct Message : On    ← 短信走 +CMT URC 直接送到 chan_quectel，不进模块存储
Auto delete SMS : Yes  ← 读完即删
```

短信被 +CMT 直接路由到 chan_quectel 手里，根本不进模块存储，
SCC-lite 的 `AT+CMGL` 轮询永远看不到它。这就是"人间蒸发"的完整机制。

## 修复

本机 chan_quectel（IchthysMaranatha 版）的 `/etc/asterisk/quectel.conf`
保留 `disablesms` 选项（新版文档称该选项已移除，本版可用）：

```ini
disablesms=no  →  disablesms=yes
```

配置注释原文：

```text
; disable of SMS reading from device when received
;  chan_quectel has currently a bug with SMS reception. When a SMS gets in during a
;  call chan_quectel might crash. Enable this option to disable sms reception.
```

关闭短信接收还能顺带避掉"通话中来短信可能搞崩 chan_quectel"的已知 bug，
一举两得。语音通话不受影响。

生效与确认：

```bash
sed -i -E 's/^(disablesms\s*=\s*)no/\1yes/' /etc/asterisk/quectel.conf
systemctl restart asterisk
asterisk -rx "quectel show device settings quectel0" | grep -iE "disable sms|auto delete"
# 期望: Disable SMS : Yes
```

此后短信的唯一入口为 SCC-lite。

## 验证（2026-10-11）

- asterisk + scc-lite 全开，手机发长短信 → SCC-lite 正常合并入库；
- `journalctl -u asterisk --since "1 hour ago"` 与
  `/var/log/asterisk/messages` 均无 `sms@` / `invalid extension`，
  chan_quectel 未再碰短信。

## 教训

多个程序共享一个 4G 模块时，"短信丢了"先查有没有**第二个消费者在截胡**，
再怀疑自己的代码。直连 AT 看模块存储（`AT+CMGL`）是最高效的分诊手段：
存储里没有 → 问题在模块上游（被截胡/没送达）；存储里有但软件没读到 →
问题在软件侧。
