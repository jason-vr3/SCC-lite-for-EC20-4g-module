#!/usr/bin/env python3
"""
SCC-lite-for-EC20-4g-module - EC20 AT 指令速查库.

每条指令的出处:
  [3GPP] = 3GPP TS 27.007 (AT command set for User Equipment) - 公开标准
  [Q]    = Quectel EC2x&EG2x&EG9x&EM05 Series AT Commands Manual - Quectel 官方
  [QCFG] = Quectel EC2x&EG2x&EG9x&EM05 Series QCFG AT Commands Manual - Quectel 官方

分类: basic(基础) / network(网络) / sms(短信) / voice(语音) /
      data(数据) / sim(SIM) / quectel(Quectel 扩展)
"""

# 每条: (指令, 说明, 出处, 示例)
AT_COMMANDS = [
    # ---- 基础 ----
    ("AT", "测试连接，返回 OK", "[3GPP]",
     "AT"),
    ("ATE0 / ATE1", "关闭/开启回显", "[3GPP]",
     "ATE0"),
    ("AT+CGMI", "查询制造商", "[3GPP]",
     "AT+CGMI"),
    ("AT+CGMM", "查询模块型号", "[3GPP]",
     "AT+CGMM"),
    ("AT+CGMR", "查询固件版本", "[3GPP]",
     "AT+CGMR"),
    ("AT+CGSN", "查询 IMEI", "[3GPP]",
     "AT+CGSN"),
    # ---- 网络 ----
    ("AT+CSQ", "信号强度 (rssi 0-31, 99=未知)", "[3GPP]",
     "AT+CSQ"),
    ("AT+CREG?", "CS 网络注册状态", "[3GPP]",
     "AT+CREG?"),
    ("AT+CEREG?", "EPS/LTE 网络注册状态", "[3GPP]",
     "AT+CEREG?"),
    ("AT+COPS?", "当前运营商", "[3GPP]",
     "AT+COPS?"),
    ("AT+COPS=?", "扫描可用运营商（耗时）", "[3GPP]",
     "AT+COPS=?"),
    ("AT+QENG=\"servingcell\"", "服务小区详情（RSRP/SINR）", "[Q]",
     "AT+QENG=\"servingcell\""),
    # ---- 短信 ----
    ("AT+CMGF=0/1", "短信模式：0=PDU，1=文本", "[3GPP]",
     "AT+CMGF=1"),
    ("AT+CSCS=\"GSM\"", "字符集（GSM/UCS2/IRA）", "[3GPP]",
     "AT+CSCS=\"GSM\""),
    ("AT+CMGL=\"ALL\"", "列出所有短信", "[3GPP]",
     "AT+CMGL=\"ALL\""),
    ("AT+CMGR=<i>", "读第 i 条短信", "[3GPP]",
     "AT+CMGR=0"),
    ("AT+CMGD=<i>", "删第 i 条短信", "[3GPP]",
     "AT+CMGD=0"),
    ("AT+CMGS=\"<号码>\"", "发短信（等 > 提示符后输入内容，Ctrl+Z 发送）", "[3GPP]",
     "AT+CMGS=\"13800138000\""),
    ("AT+CSCA?", "短信中心号码", "[3GPP]",
     "AT+CSCA?"),
    ("AT+CNMI=2,1,0,0,0", "新短信上报到串口（+CMTI）", "[3GPP]",
     "AT+CNMI=2,1,0,0,0"),
    # ---- 语音 ----
    ("ATD<号码>;", "拨打语音电话（; 不可少）", "[3GPP]",
     "ATD10086;"),
    ("ATA", "接听来电", "[3GPP]",
     "ATA"),
    ("ATH", "挂断", "[3GPP]",
     "ATH"),
    ("AT+CLIP=1", "来电显示（+CLIP 上报）", "[3GPP]",
     "AT+CLIP=1"),
    ("AT+CLCC", "查询当前通话", "[3GPP]",
     "AT+CLCC"),
    ("AT+VTS=<dtmf>", "发送 DTMF", "[3GPP]",
     "AT+VTS=1"),
    # ---- 数据 ----
    ("AT+CGDCONT?", "查询 APN 配置", "[3GPP]",
     "AT+CGDCONT?"),
    ("AT+CGDCONT=1,\"IP\",\"<apn>\"", "设置 APN", "[3GPP]",
     "AT+CGDCONT=1,\"IP\",\"3gnet\""),
    ("AT+CGACT?", "PDP 上下文状态", "[3GPP]",
     "AT+CGACT?"),
    # ---- SIM ----
    ("AT+CPIN?", "SIM 卡状态（READY=就绪）", "[3GPP]",
     "AT+CPIN?"),
    ("AT+CIMI", "查询 IMSI", "[3GPP]",
     "AT+CIMI"),
    ("AT+QCCID", "查询 ICCID", "[Q]",
     "AT+QCCID"),
    # ---- Quectel 扩展 ----
    ("AT+QCFG=\"usbnet\",0/1/2", "USB 网络模式：0=QMI，1=ECM，2=MBIM", "[QCFG]",
     "AT+QCFG=\"usbnet\",0"),
    ("AT+QCFG=\"usbcfg\",<vid>,<pid>,<diag>,<nmea>,<at>,<modem>,<rmnet>,<adb>,<uac>",
     "USB 端口组合（UAC 开关等）", "[QCFG]",
     "AT+QCFG=\"USBCFG\",0x2C7C,0x0125,1,1,1,1,1,0,1"),
    ("AT+QURCCFG=\"urcport\",\"usbat\"", "URC 输出口（usbat=USB AT 口）", "[Q]",
     "AT+QURCCFG=\"urcport\",\"usbat\""),
    ("AT+QCFG=\"ims\",0/1", "VoLTE 开关", "[QCFG]",
     "AT+QCFG=\"ims\",1"),
    ("AT+CFUN=0/1", "0=飞行模式（射频关），1=全功能", "[3GPP]",
     "AT+CFUN=1"),
    ("AT+CFUN=1,1", "重启模块", "[3GPP]",
     "AT+CFUN=1,1"),
]

CATEGORIES = {
    "basic": "基础",
    "network": "网络",
    "sms": "短信",
    "voice": "语音",
    "data": "数据",
    "sim": "SIM",
    "quectel": "Quectel 扩展",
}

# 指令 -> 分类 映射（按关键字）
def _categorize(cmd):
    c = cmd.upper()
    if any(k in c for k in ("CMGF", "CMGL", "CMGR", "CMGD", "CMGS", "CSCA",
                             "CNMI", "CSCS")):
        return "sms"
    if any(k in c for k in ("CSQ", "CREG", "CEREG", "COPS", "QENG")):
        return "network"
    if any(k in c for k in ("ATD", "ATA", "ATH", "CLIP", "CLCC", "VTS")):
        return "voice"
    if any(k in c for k in ("CGDCONT", "CGACT")):
        return "data"
    if any(k in c for k in ("CPIN", "CIMI", "QCCID")):
        return "sim"
    if "QCFG" in c or "QURCCFG" in c:
        return "quectel"
    return "basic"


def search(keyword):
    """按关键字搜索指令，返回匹配列表."""
    kw = keyword.strip().upper()
    if not kw:
        return [(cmd, desc, src, ex, _categorize(cmd))
                for cmd, desc, src, ex in AT_COMMANDS]
    return [(cmd, desc, src, ex, _categorize(cmd))
            for cmd, desc, src, ex in AT_COMMANDS
            if kw in cmd.upper() or kw in desc]


def by_category(cat):
    """按分类返回指令."""
    return [(cmd, desc, src, ex)
            for cmd, desc, src, ex in AT_COMMANDS
            if _categorize(cmd) == cat]
