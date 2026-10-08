#!/usr/bin/env python3
"""
SCC-lite-for-EC20-4g-module - EC20 系列资料库.

数据来源（交叉验证）:
  - Quectel EC20 产品规格书 (via Mouser/Rhydolabz/RS-online)
  - Quectel Products Portfolio Overview (2017)
  - wirelesscommunicationmodule.com / buybestelectronic.com 规格表

如发现错误，欢迎提 Issue 纠正。
"""

# ----------------------------------------------------------------------
# EC20 全系一览
# ----------------------------------------------------------------------
EC20_COMMON = {
    "chipset": "Qualcomm MDM9215",
    "lte_category": "Cat.3",
    "lte_fdd_speed": "100 Mbps (DL) / 50 Mbps (UL)",
    "lte_tdd_speed": "61 Mbps (DL) / 18 Mbps (UL)",
    "lte_version": "3GPP E-UTRA Release 9",
    "bandwidth": "1.4 / 3 / 5 / 10 / 15 / 20 MHz",
    "dimensions": "32.0 × 29.0 × 2.4 mm",
    "package": "LCC",
    "weight": "约 4.6 g",
    "voltage": "3.3V ~ 4.3V（典型 3.8V）",
    "temperature": "-40°C ~ +85°C",
    "usb": "USB 2.0 High Speed (480 Mbps)",
    "sim": "1.8V / 3.0V (U)SIM",
    "audio": "PCM 数字音频（可选）",
    "gnss": "GPS / GLONASS（可选）",
    "at_control": "3GPP TS 27.007 + Quectel 增强 AT",
    "protocols": "TCP/UDP/PPP/FTP/FTPS/HTTP/HTTPS/SMTP/NTP/PING/QMI",
    "drivers": "Windows / Linux (2.6+) / Android",
}

# 各版本频段与适用地区
# 键: 版本后缀; 值: {region, lte_fdd, lte_tdd, wcdma, tdscdma, cdma, gsm, note}
EC20_VARIANTS = {
    "EC20-E": {
        "region": "欧洲/中东/非洲/韩国/泰国/印度",
        "use": "EMEA 及亚洲多国通用，频段最全",
        "lte_fdd": "B1 / B3 / B5 / B7 / B8 / B20",
        "lte_tdd": "—",
        "wcdma": "B1 / B5 / B8",
        "tdscdma": "—",
        "cdma": "—",
        "gsm": "850 / 900 / 1800 / 1900 MHz",
    },
    "EC20-A": {
        "region": "北美（AT&T / T-Mobile / 加拿大）",
        "use": "北美运营商定制频段",
        "lte_fdd": "B2 / B4 / B5 / B12 / B17",
        "lte_tdd": "—",
        "wcdma": "B2 / B4 / B5",
        "tdscdma": "—",
        "cdma": "—",
        "gsm": "850 / 1900 MHz",
    },
    "EC20-C": {
        "region": "中国（移动/联通）",
        "use": "国内 TDD 主力，支持移动 TD-SCDMA",
        "lte_fdd": "B1 / B3 / B8",
        "lte_tdd": "B38 / B39 / B40 / B41",
        "wcdma": "B1 / B8",
        "tdscdma": "B34 / B39",
        "cdma": "—",
        "gsm": "900 / 1800 MHz",
    },
    "EC20-CE": {
        "region": "中国（移动/联通/电信全网通）",
        "use": "国内全网通，唯一支持电信 CDMA 的版本",
        "lte_fdd": "B1 / B3",
        "lte_tdd": "B38 / B39 / B40 / B41",
        "wcdma": "B1",
        "tdscdma": "B34 / B39",
        "cdma": "BC0 (CDMA2000 1x / EVDO)",
        "gsm": "900 / 1800 MHz",
    },
}

# 固件版本命名示例: EC20CEHDLGR06A05M1G
#   EC20-CE = 版本, HD = ? , LG = ?, R06A05M1G = 基线版本
FIRMWARE_NOTE = (
    "固件命名如 EC20CEHDLGR06A05M1G：EC20-CE 为硬件版本，"
    "R06A05M1G 为基线版本号。新固件需经 Quectel 官方渠道获取。"
)

# 典型应用
APPLICATIONS = [
    "CPE / 路由器", "数据卡", "工业 PDA", "车载终端",
    "安防监控", "短信收发（本项目）", "语音中继（配合 Asterisk）",
]
