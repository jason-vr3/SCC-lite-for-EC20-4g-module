#!/usr/bin/env python3
"""
SCC-lite - 全球 APN 数据库与运营商自动识别.

数据来源: open-carrier-data/open-carrier-data
  (generated/android/apns-conf.xml, CC0-1.0 公共领域)
  https://github.com/open-carrier-data/open-carrier-data
共 26000+ 条 APN，打包在项目内，无需联网查询.

识别优先级 (用户 2026-10-08 确认):
  1. IMSI 前 5-6 位 -> MCC/MNC 精确匹配
  2. AT+COPS 运营商名 -> 模糊匹配兜底

用法:
  from apn_db import detect_carrier, find_apn
  info = detect_carrier(imsi="460151008018066", cops_name="CHN-CBN")
  # {"carrier": "CBNET", "apn": "cbnet", "mcc": "460", "mnc": "15",
  #  "protocol": "IPV4V6", "source": "imsi"}
"""

import os
import xml.etree.ElementTree as ET
import logging

log = logging.getLogger("scc-lite.apn")

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "apns-conf.xml")

# 解析缓存: (mcc, mnc) -> [apn dict, ...]
_cache = None


def _parse_db():
    """解析 apns-conf.xml，按 (mcc, mnc) 建索引. 只解析一次."""
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        tree = ET.parse(_DB_PATH)
    except (FileNotFoundError, ET.ParseError) as e:
        log.error("APN 数据库解析失败 %s: %s", _DB_PATH, e)
        return _cache
    for apn in tree.getroot().iter("apn"):
        a = apn.attrib
        mcc, mnc = a.get("mcc"), a.get("mnc")
        if not mcc or not mnc:
            continue
        # mnc 可能是 "15" 或 "015"，统一去前导零后比较时再处理
        key = (mcc, mnc.lstrip("0") or "0")
        entry = {
            "carrier": a.get("carrier", ""),
            "apn": a.get("apn", ""),
            "mcc": mcc,
            "mnc": a.get("mnc"),
            "protocol": a.get("protocol", "IPV4V6"),
            "type": a.get("type", ""),
            "mvno_type": a.get("mvno_type", ""),
            "mvno_match_data": a.get("mvno_match_data", ""),
        }
        _cache.setdefault(key, []).append(entry)
    log.info("APN 库加载: %d 个 MCC/MNC 组合", len(_cache))
    return _cache


def find_apn(mcc, mnc):
    """
    按 MCC/MNC 找最合适的上网 APN.
    优先 type 含 "default" 的条目 (数据上网用), 再按 protocol 排序.
    返回 dict 或 None.
    """
    db = _parse_db()
    key = (str(mcc), str(mnc).lstrip("0") or "0")
    entries = db.get(key, [])
    if not entries:
        return None
    # 只要能上网的 (type 含 default)，不要纯 mms/ia 的
    cands = [e for e in entries if "default" in e["type"].split(",")]
    if not cands:
        cands = entries
    # v0.5.7: 中国运营商优先用标准 APN (避免选中 MVNO 或生僻 APN)
    # 如电信 460/11 优先 ctnet 而非 ctlte
    _preferred = {
        ("460", "0"): ["cmnet"],           # 移动
        ("460", "2"): ["cmnet"],
        ("460", "7"): ["cmnet"],
        ("460", "8"): ["cmnet"],
        ("460", "1"): ["uninet", "3gnet"],  # 联通
        ("460", "6"): ["uninet", "3gnet"],
        ("460", "9"): ["uninet", "3gnet"],
        ("460", "3"): ["ctnet"],           # 电信
        ("460", "5"): ["ctnet"],
        ("460", "11"): ["ctnet"],
        ("460", "15"): ["cbnet"],          # 广电
    }
    pref_list = _preferred.get(key, [])
    for pref_apn in pref_list:
        for e in cands:
            if e["apn"] == pref_apn:
                return e
    # 优先有明确 carrier 名的
    cands.sort(key=lambda e: (0 if e["carrier"] else 1, e["apn"]))
    return cands[0]


def detect_carrier(imsi="", cops_name=""):
    """
    自动识别运营商与 APN.
    imsi: AT+CIMI 返回, 如 "460151008018066" (前5位46015 = MCC460 MNC15)
    cops_name: AT+COPS? 返回的运营商名, 如 "CHN-CBN", 兜底用.
    返回 {"carrier","apn","mcc","mnc","protocol","source"} 或 None.
    """
    imsi = (imsi or "").strip()
    # --- 1. IMSI 精确匹配 ---
    if len(imsi) >= 5 and imsi[:3].isdigit():
        mcc = imsi[:3]
        # MNC 位数: 中国 MCC=460 的 MNC 恒为 2 位, 必须先取 2 位
        # (取 3 位会把 MSIN 首位误判为 MNC, 如 4600036... 取 "003" 误判为电信)
        # 其他国家先试 3 位, 找不到再试 2 位
        mnc_lens = (2,) if mcc == "460" else (3, 2)
        for mnc_len in mnc_lens:
            if len(imsi) >= 3 + mnc_len:
                mnc = imsi[3:3 + mnc_len]
                hit = find_apn(mcc, mnc)
                if hit:
                    hit = dict(hit)
                    hit["source"] = "imsi"
                    return hit
    # --- 2. COPS 运营商名模糊匹配 ---
    name = (cops_name or "").strip().lower()
    if name:
        db = _parse_db()
        for entries in db.values():
            for e in entries:
                carrier = e["carrier"].lower()
                if carrier and (carrier in name or name in carrier):
                    if "default" in e["type"].split(","):
                        hit = dict(e)
                        hit["source"] = "cops"
                        return hit
    return None


def apn_count():
    """库内 APN 条目总数 (页面展示用)."""
    db = _parse_db()
    return sum(len(v) for v in db.values())
