# chan_quectel 截胡短信（2026-10-11，N1 实机）

## 症状

Asterisk + SCC-lite 同跑时，短信偶发"人间蒸发"：`AT+CMGL` 查不到（没进模块存储），SCC-lite 库也没有。

## 实锤

Asterisk 日志：

```text
Channel 'Local/sms@dongle-incoming-...' sent to invalid extension (...,dongle-incoming,sms,1)
```

是 `chan_quectel.so`（不是 chan_dongle）拦截了短信事件。

## 根因

```text
Direct Message : On    ← +CMT 直接送 chan_quectel，不进模块存储
Auto delete SMS : Yes  ← 读完即删
```

"截胡 + 销毁"二连，SCC-lite 的 `AT+CMGL` 轮询永远看不见。

## 修复

`/etc/asterisk/quectel.conf`：`disablesms=no` → `yes`（IchthysMaranatha 版保留此选项；新版文档说已移除，别被误导）。重启 asterisk，确认 `Disable SMS: Yes`。语音不受影响，还顺带避掉"通话中来短信搞崩 chan_quectel"的 bug。

## 走过的弯路

1. 误判"dialplan 没接住，短信一定还在模块里"——错，+CMT 直达的不进存储。
2. 先怀疑自己代码，查 `_parse_udh` 崩溃路径半天；诊断（零重启、无 poll error）排除后才回头查截胡。分诊顺序反了：**应先直连 AT 看模块存储，存储里没有 = 上游问题**。
3. "让 10086 发短信"测试法含糊：用自己手机发，回复只到自己手机，测了个寂寞。
4. "没收到"有两种：分片被 daemon 收走但不齐（查 journal `concat part` + DB 新行）vs 压根没到模块——先分清再动手。

## 验证

全开状态收长短信：SCC-lite 正常合并入库；asterisk 日志 1 小时无 sms warning。
