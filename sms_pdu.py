#!/usr/bin/env python3
"""
SCC-lite - SMS PDU 解析器 (3GPP TS 23.040).

用于解析 PDU 模式 (AT+CMGF=0) 下读取的短信, 提取长短信重组所需的
UDH (User Data Header) 信息: reference / total / sequence.

只实现 SMS-DELIVER (接收) 解析, 不涉及发送编码.
"""

import re


def _bcd_swap(s):
    """BCD 半字节交换: '21' -> '12'. 末尾 F 去掉."""
    out = []
    for i in range(0, len(s), 2):
        pair = s[i:i + 2]
        if len(pair) == 2:
            out.append(pair[1] + pair[0])
        else:
            out.append(pair)
    return "".join(out).rstrip("Ff")


def _decode_address(hex_str, addr_len, toa):
    """解码发送方号码. addr_len 为半字节数."""
    # 取 addr_len 个半字节
    hex_digits = len(hex_str)
    # 地址字段字节数 = ceil(addr_len / 2)
    addr_bytes = (addr_len + 1) // 2
    addr_hex = hex_str[:addr_bytes * 2]
    number = _bcd_swap(addr_hex)
    # 国际号码加 +
    if toa & 0x70 == 0x10:  # TON = international
        number = "+" + number
    return number, addr_bytes * 2


def _decode_scts(hex_str):
    """解码 SCTS 时间戳 (7 字节 BCD), 返回 'yy/MM/dd,hh:mm:ss+zz' 风格字符串."""
    if len(hex_str) < 14:
        return ""
    parts = []
    for i in range(0, 14, 2):
        parts.append(_bcd_swap(hex_str[i:i + 2]))
    # parts: yy, MM, dd, hh, mm, ss, tz
    return f"{parts[0]}/{parts[1]}/{parts[2]},{parts[3]}:{parts[4]}:{parts[5]}"


def _gsm7_unpack(data, septet_count):
    """GSM 7-bit 解包. data: bytes, septet_count: 7-bit 字符数."""
    result = []
    for i in range(septet_count):
        # 第 i 个 septet 跨越的字节位置
        bit_pos = i * 7
        byte_pos = bit_pos // 8
        bit_offset = bit_pos % 8
        if byte_pos >= len(data):
            break
        val = data[byte_pos] >> bit_offset
        if bit_offset > 1 and byte_pos + 1 < len(data):
            val |= data[byte_pos + 1] << (8 - bit_offset)
        result.append(chr(val & 0x7F))
    return "".join(result)


# GSM 7-bit 默认字母表到 Unicode 的特殊映射 (与 ASCII 不同的部分)
_GSM7_EXT = {
    0x00: "@", 0x01: "£", 0x02: "$", 0x03: "¥", 0x04: "è",
    0x05: "é", 0x06: "ù", 0x07: "ì", 0x08: "ò", 0x09: "Ç",
    0x0B: "Ø", 0x0C: "ø", 0x0E: "Å", 0x0F: "å",
    0x10: "Δ", 0x11: "_", 0x12: "Φ", 0x13: "Γ", 0x14: "Λ",
    0x15: "Ω", 0x16: "Π", 0x17: "Ψ", 0x18: "Σ", 0x19: "Θ",
    0x1A: "Ξ", 0x1B: "Æ", 0x1C: "æ", 0x1D: "ß", 0x1E: "É",
    0x24: "¤", 0x40: "¡", 0x5B: "Ä", 0x5C: "Ö", 0x5D: "Ñ",
    0x5E: "Ü", 0x5F: "§", 0x60: "¿", 0x7B: "ä", 0x7C: "ö",
    0x7D: "ñ", 0x7E: "ü", 0x7F: "à",
}


def _gsm7_to_text(s):
    """GSM 7-bit 字符映射到可读文本 (转义字符用近似替代)."""
    out = []
    i = 0
    while i < len(s):
        c = ord(s[i])
        if c == 0x1B and i + 1 < len(s):  # 转义
            ec = ord(s[i + 1])
            out.append({0x0A: "\n", 0x14: "^", 0x28: "{", 0x29: "}",
                        0x2F: "\\", 0x3C: "[", 0x3D: "~", 0x3E: "]",
                        0x40: "|", 0x65: "€"}.get(ec, "?"))
            i += 2
        else:
            out.append(_GSM7_EXT.get(c, s[i] if 32 <= c < 127 else "?"))
            i += 1
    return "".join(out)


def _parse_udh(udh_bytes):
    """解析 UDH, 提取 concatenated SMS 信息.
    返回 (ref, total, seq) 或 (None, None, None)."""
    i = 0
    while i < len(udh_bytes):
        if i + 1 >= len(udh_bytes):
            break
        iei = udh_bytes[i]
        ie_len = udh_bytes[i + 1]
        ie_data = udh_bytes[i + 2:i + 2 + ie_len]
        if len(ie_data) < ie_len:
            break  # IE 数据被截断，放弃解析，避免 IndexError 掀翻调用方
        if iei == 0x00 and ie_len == 3:
            # 8-bit reference concatenated
            ref, total, seq = ie_data[0], ie_data[1], ie_data[2]
            return (ref, total, seq)
        elif iei == 0x08 and ie_len == 4:
            # 16-bit reference concatenated
            ref = (ie_data[0] << 8) | ie_data[1]
            total, seq = ie_data[2], ie_data[3]
            return (ref, total, seq)
        i += 2 + ie_len
    return (None, None, None)


def decode_pdu(pdu_hex):
    """解码 SMS-DELIVER PDU.
    返回 dict: sender, time, body, ref, total, seq, encoding.
    失败返回 None.
    本函数保证不抛异常：任何畸形 PDU 只返回 None，绝不掀翻 daemon。
    """
    try:
        pdu = bytes.fromhex(pdu_hex.strip())
    except ValueError:
        return None
    try:
        return _decode_deliver_pdu(pdu)
    except Exception:
        return None


def _decode_deliver_pdu(pdu):
    """decode_pdu 的实际解码体（异常由外层兜底返回 None）。"""
    if len(pdu) < 10:
        return None

    pos = 0
    # 1. SMSC
    smsc_len = pdu[pos]
    pos += 1 + smsc_len
    if pos >= len(pdu):
        return None

    # 2. First octet
    first = pdu[pos]
    mti = first & 0x03
    if mti != 0x00:
        # 只处理 SMS-DELIVER (MTI=00); SUBMIT/STATUS-REPORT 跳过
        return None
    udhi = bool(first & 0x40)
    pos += 1

    # 3. Sender address
    if pos + 2 > len(pdu):
        return None
    addr_len = pdu[pos]  # 半字节数
    toa = pdu[pos + 1]
    pos += 2
    addr_hex = pdu[pos:pos + (addr_len + 1) // 2].hex()
    sender, used = _decode_address(addr_hex, addr_len, toa)
    pos += (addr_len + 1) // 2

    # 4. PID (skip)
    pos += 1
    if pos >= len(pdu):
        return None

    # 5. DCS
    dcs = pdu[pos]
    pos += 1

    # 6. SCTS (7 bytes)
    if pos + 7 > len(pdu):
        return None
    scts_hex = pdu[pos:pos + 7].hex()
    timestamp = _decode_scts(scts_hex)
    pos += 7

    # 7. UDL + User Data
    if pos >= len(pdu):
        return None
    udl = pdu[pos]
    pos += 1
    user_data = pdu[pos:]

    # DCS 解码: bit1-0 = 00 GSM7, 10 UCS2
    dcs_group = dcs & 0x0C
    is_ucs2 = (dcs_group == 0x08)

    ref, total, seq = None, None, None
    body = ""

    if udhi and len(user_data) > 0:
        udh_len = user_data[0]
        udh = user_data[1:1 + udh_len]
        ref, total, seq = _parse_udh(udh)
        content = user_data[1 + udh_len:]
        if is_ucs2:
            # UCS2: UDL 为字节数
            try:
                body = content[:udl - 1 - udh_len].decode("utf-16-be")
            except Exception:
                body = ""
        else:
            # GSM 7-bit: UDL 为 septet 数, UDH 占 ceil((udh_len+1)*8/7) 个
            udh_septets = ((udh_len + 1) * 8 + 6) // 7
            body_septets = udl - udh_septets
            raw = _gsm7_unpack(content, body_septets)
            body = _gsm7_to_text(raw)
    else:
        if is_ucs2:
            try:
                body = user_data[:udl].decode("utf-16-be")
            except Exception:
                body = ""
        else:
            raw = _gsm7_unpack(user_data, udl)
            body = _gsm7_to_text(raw)

    return {
        "sender": sender,
        "time": timestamp,
        "body": body,
        "ref": ref,
        "total": total,
        "seq": seq,
        "encoding": "ucs2" if is_ucs2 else "gsm7",
        "is_concat": ref is not None,
    }
