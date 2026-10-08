#!/usr/bin/env python3
"""
SCC-lite (SMS Control Centre) - 4G 模块 AT 指令封装.

短信控制中心 - 4G模块AT命令封装层.

本模块封装所有与 Quectel EC20/EC25 等模块通信的 AT 指令.
所有 AT 指令均为标准 3GPP TS 27.007 指令或 Quectel 专用指令,
出处见各方法文档:
  - 3GPP TS 27.007 (AT command set for User Equipment)
  - Quectel EC2x&EG2x&EG9x&EM05 Series AT Commands Manual
  - Quectel EC2x&EG2x&EG9x&EM05 Series QCFG AT Commands Manual (for QCFG)

实测环境: Quectel EC20 (EC20CEHDLGR06A05M1G), /dev/ttyUSB3, 115200 baud.
Tested on: Quectel EC20 (EC20CEHDLGR06A05M1G) via /dev/ttyUSB3, 115200 baud.
"""

import serial
import time
import re
import logging
import fcntl
import os

log = logging.getLogger("scc-lite.modem")

# 文件锁: 防止 daemon 和 Web UI 同时操作串口导致冲突
# Lock file to prevent daemon and web UI from using modem simultaneously
MODEM_LOCK = "/tmp/scc-lite-modem.lock"


class ModemError(Exception):
    """Raised when modem returns ERROR or times out."""
    pass


class Modem:
    """
    AT command interface to a cellular modem via serial port.

    Usage:
        m = Modem("/dev/ttyUSB3")
        m.open()
        print(m.get_signal())   # {"rssi": 25, "ber": 99}
        m.close()
    """

    def __init__(self, port="/dev/ttyUSB3", baudrate=115200, timeout=5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self._lock_f = None

    def open(self):
        """Open serial port (with file lock to prevent concurrent access)."""
        # Acquire exclusive lock - blocks if another process holds it
        self._lock_f = open(MODEM_LOCK, "w")
        try:
            fcntl.flock(self._lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            self._lock_f.close()
            self._lock_f = None
            raise ModemError("modem busy (locked by another process)")
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        # Flush stale data
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # Basic AT handshake (3GPP TS 27.007 §5.1)
        try:
            self._cmd("AT")
        except ModemError:
            self.close()
            raise

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None
        if self._lock_f:
            try:
                fcntl.flock(self._lock_f, fcntl.LOCK_UN)
            except:
                pass
            self._lock_f.close()
            self._lock_f = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Low-level AT command
    # ------------------------------------------------------------------
    def _cmd(self, cmd, wait_time=0.3, expect_ok=True):
        """
        Send one AT command, return list of response lines (excluding echo/OK).

        Source: 3GPP TS 27.007 §4 - command line format "AT<cmd><CR>".
        """
        if not self.ser or not self.ser.is_open:
            raise ModemError("serial port not open")

        log.debug("TX: %s", cmd)
        # Clear any stale input before sending CMGS
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode())
        time.sleep(wait_time)

        lines = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self.ser.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if line == cmd:      # echo
                continue
            log.debug("RX: %s", line)
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith("+CME ERROR") \
                    or line.startswith("+CMS ERROR"):
                if expect_ok:
                    raise ModemError(f"{cmd} -> {line}")
                lines.append(line)
                return lines
            lines.append(line)
        # Timeout without OK
        if expect_ok:
            raise ModemError(f"{cmd} -> timeout (no OK)")
        return lines

    def _cmd_prompt(self, cmd, data, ctrl_z=True):
        """
        Send command expecting '>' prompt (e.g. AT+CMGS), then send data.

        Source: 3GPP TS 27.007 §7.7 (AT+CMGS) - modem returns '>' then
        accepts message text terminated by Ctrl+Z (0x1A).
        """
        if not self.ser or not self.ser.is_open:
            raise ModemError("serial port not open")

        log.debug("TX: %s", cmd)
        # Clear stale input to prevent previous command echo leaking into SMS
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode())

        # Wait for '>' prompt
        deadline = time.time() + self.timeout
        got_prompt = False
        while time.time() < deadline:
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            log.debug("RX: %s", line)
            if ">" in line:
                got_prompt = True
                break
            if line == "ERROR" or "ERROR" in line:
                raise ModemError(f"{cmd} -> prompt failed: {line}")
        if not got_prompt:
            raise ModemError(f"{cmd} -> no '>' prompt")

        # Send data + Ctrl+Z
        # (buffer already clean from waiting for prompt)
        self.ser.write(data.encode())
        if ctrl_z:
            self.ser.write(b"\x1a")

        # Wait for +CMGS: <mr> and OK
        lines = []
        deadline = time.time() + 30  # SMS send can take a while
        while time.time() < deadline:
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            log.debug("RX: %s", line)
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith("+CMS ERROR"):
                raise ModemError(f"SMS send failed: {line}")
            lines.append(line)
        raise ModemError("SMS send timeout")

    # ------------------------------------------------------------------
    # Device info
    # Source: 3GPP TS 27.007
    # ------------------------------------------------------------------
    def get_imei(self):
        """AT+CGSN - Request International Mobile Equipment Identity (IMEI)."""
        lines = self._cmd("AT+CGSN")
        for line in lines:
            if re.fullmatch(r"\d{15}", line):
                return line
        raise ModemError("AT+CGSN: no IMEI found")

    def get_imsi(self):
        """AT+CIMI - Request International Mobile Subscriber Identity (IMSI)."""
        lines = self._cmd("AT+CIMI")
        for line in lines:
            if re.fullmatch(r"\d{14,15}", line):
                return line
        raise ModemError("AT+CIMI: no IMSI found")

    def get_iccid(self):
        """
        AT+QCCID - Show ICCID of SIM card.
        Source: Quectel EC2x AT Commands Manual (QCCID is Quectel-specific;
        generic 3GPP uses AT+CCID on some modules).
        Falls back to AT+CCID if QCCID fails.
        """
        try:
            lines = self._cmd("AT+QCCID")
        except ModemError:
            lines = self._cmd("AT+CCID")
        for line in lines:
            m = re.search(r"(\d{19,20})", line)
            if m:
                return m.group(1)
        raise ModemError("no ICCID found")

    def get_signal(self):
        """
        AT+CSQ - Signal Quality.
        Returns {"rssi": int, "ber": int}.
        Source: 3GPP TS 27.007 §8.5. RSSI 0-31 (99=unknown), BER 0-7 (99=unknown).
        """
        lines = self._cmd("AT+CSQ")
        for line in lines:
            m = re.match(r"\+CSQ:\s*(\d+),(\d+)", line)
            if m:
                return {"rssi": int(m.group(1)), "ber": int(m.group(2))}
        raise ModemError("AT+CSQ: no signal data")

    def get_registration(self):
        """
        AT+CREG? - Network registration status (circuit-switched).
        Returns {"n": int, "stat": int} where stat: 0=not registered,
        1=registered home, 2=searching, 3=denied, 5=registered roaming.
        Source: 3GPP TS 27.007 §7.2.
        """
        lines = self._cmd("AT+CREG?")
        for line in lines:
            m = re.match(r"\+CREG:\s*(\d+),(\d+)", line)
            if m:
                return {"n": int(m.group(1)), "stat": int(m.group(2))}
        raise ModemError("AT+CREG?: no data")

    def get_operator(self):
        """
        AT+COPS? - Query current operator.
        Returns {"mode": int, "format": int, "oper": str} or {} if none.
        Source: 3GPP TS 27.007 §7.3.
        """
        lines = self._cmd("AT+COPS?")
        for line in lines:
            m = re.match(r'\+COPS:\s*(\d+),(\d+),"([^"]*)"', line)
            if m:
                return {"mode": int(m.group(1)),
                        "format": int(m.group(2)),
                        "oper": m.group(3)}
        return {}

    # ------------------------------------------------------------------
    # Radio / flight mode
    # Source: 3GPP TS 27.007 §7.11 (AT+CFUN)
    # ------------------------------------------------------------------
    def set_flight_mode(self, enable):
        """
        AT+CFUN=<fun> - Set phone functionality.
        fun=0: minimum functionality (flight mode / RF off).
        fun=1: full functionality (default).
        Source: 3GPP TS 27.007 §7.11.
        """
        fun = 0 if enable else 1
        self._cmd(f"AT+CFUN={fun}")
        # CFUN change takes a moment; give modem time to settle
        time.sleep(3 if enable else 5)

    def set_cnmi(self, mode=2, mt=1, bm=0, ds=0, bfr=0):
        """
        AT+CNMI - New message indications to TE.
        mode=2: forward URCs directly even if link busy.
        mt=1: store SMS and send +CMTI: "SM",<index> URC (instant notify).
        Source: 3GPP TS 27.005 §3.4.1.
        Recommended by open-source practice (gammu, macsatcom/sms-gateway-sim7600):
        AT+CNMI=2,1,0,0,0 for fastest incoming SMS detection.
        Polling AT+CMGL remains as fallback (URCs can be missed).
        """
        self._cmd(f"AT+CNMI={mode},{mt},{bm},{ds},{bfr}")

    def get_flight_mode(self):
        """
        AT+CFUN? - Query phone functionality.
        Returns True if in flight mode (fun=0), False otherwise.
        Source: 3GPP TS 27.007 §7.11.
        """
        lines = self._cmd("AT+CFUN?")
        for line in lines:
            m = re.match(r"\+CFUN:\s*(\d+)", line)
            if m:
                return int(m.group(1)) == 0
        raise ModemError("AT+CFUN?: no data")

    # ------------------------------------------------------------------
    # SMS
    # Source: 3GPP TS 27.007 §7 (SMS AT commands)
    # ------------------------------------------------------------------
    def sms_init(self):
        """
        Prepare modem for SMS text-mode operation:
          AT+CMGF=1      - SMS message format: 1 = text mode (§7.7)
          AT+CSCS="GSM"  - TE character set (§5.5); use "UCS2" for Chinese
        """
        self._cmd("AT+CMGF=1")
        self._cmd('AT+CSCS="GSM"')

    def sms_list(self, stat="all"):
        """
        AT+CMGL=<stat> - List SMS messages.
        stat: "rec unread", "rec read", "sto unsent", "sto sent", "all".
        Returns list of dicts: {"index": int, "stat": str, "sender": str,
                                "time": str, "body": str}.
        Source: 3GPP TS 27.007 §7.7.

        Note: EC20 is case-sensitive; use lowercase stat ("all" not "ALL").
        Note: body may be UCS2 hex if message contains non-GSM characters.
        Use decode_ucs2() to convert.
        """
        # Ensure text mode (lowercase for EC20 case-sensitivity)
        self._cmd("at+cmgf=1")
        # EC20 requires lowercase command and stat value
        lines = self._cmd(f'at+cmgl="{stat.lower()}"')
        msgs = []
        i = 0
        while i < len(lines):
            # EC20 format: +CMGL: idx,"STAT","sender",[alpha,]"time"
            # alpha may be empty (,,) or "" - handle both
            m = re.match(r'\+CMGL:\s*(\d+),"([^"]+)","([^"]*)",(.*)', lines[i])
            if m and i + 1 < len(lines):
                idx, stat_v, sender, rest = m.groups()
                # Extract time: last quoted string in rest
                tm = re.search(r'"([^"]*)"\s*$', rest.strip())
                time_v = tm.group(1) if tm else ""
                msgs.append({
                    "index": int(idx),
                    "stat": stat_v,
                    "sender": sender,
                    "time": time_v,
                    "body": lines[i + 1],
                })
                i += 2
            else:
                i += 1
        return msgs

    def sms_read(self, index):
        """
        AT+CMGR=<index> - Read SMS message at index.
        Source: 3GPP TS 27.007 §7.7.
        """
        self._cmd("at+cmgf=1")
        lines = self._cmd(f"at+cmgr={index}")
        if len(lines) >= 2:
            m = re.match(
                r'\+CMGR:\s*"([^"]+)","([^"]*)","[^"]*","([^"]*)"',
                lines[0])
            if m:
                return {"stat": m.group(1), "sender": m.group(2),
                        "time": m.group(3), "body": lines[1]}
        raise ModemError(f"AT+CMGR={index}: parse failed")

    def sms_delete(self, index):
        """
        AT+CMGD=<index> - Delete SMS at index.
        Source: 3GPP TS 27.007 §7.7.
        """
        self._cmd(f"at+cmgd={index}")

    def sms_delete_all(self):
        """
        Delete all SMS. Iterates AT+CMGL="ALL" then AT+CMGD per index.
        (AT+CMGD=1,4 "delete all" is supported on some modules but not
        universally; per-index delete is portable.)
        """
        for msg in self.sms_list("ALL"):
            try:
                self.sms_delete(msg["index"])
            except ModemError as e:
                log.warning("delete index %s failed: %s", msg["index"], e)

    def sms_send(self, number, text):
        """
        AT+CMGS - Send SMS in text mode.
        Long messages (>70 UCS2 / >160 GSM chars) are auto-split and sent
        via Quectel AT+QCMGS (text-mode concatenated SMS, module builds UDH).
        Returns message reference (mr) of last segment on success.

        Sources:
          - 3GPP TS 27.007 §7.7 (AT+CMGS)
          - Quectel EC2x&EG9x&EG2x-G&EM05 AT Commands Manual V2.0 §9.17
            (AT+QCMGS=<da>[,<toda>],<uid>,<msg_seg>,<msg_total>)
          - Open-source: macsatcom/sms-gateway-sim7600 (UCS2 hex pattern)
        """
        is_ucs2 = any(ord(c) > 127 for c in text)
        # Single-segment limits; multi-segment limits (6-byte UDH overhead)
        if is_ucs2:
            single_max, seg_max = 70, 67
        else:
            single_max, seg_max = 160, 153

        if len(text) <= single_max:
            return self._sms_send_single(number, text, is_ucs2)

        # Long SMS: split into segments, max 7 per Quectel docs
        import random
        segments = [text[i:i+seg_max] for i in range(0, len(text), seg_max)]
        if len(segments) > 7:
            raise ModemError(f"message too long: {len(segments)} segments > 7 max")
        uid = random.randint(0, 255)
        total = len(segments)
        last_mr = 0
        for idx, seg in enumerate(segments, start=1):
            mr = self._sms_send_qcmgs(number, seg, is_ucs2, uid, idx, total)
            last_mr = mr
        return last_mr

    def _sms_send_single(self, number, text, is_ucs2):
        """Send a single-segment SMS via AT+CMGS."""
        self._cmd("at+cmgf=1")
        if is_ucs2:
            self._cmd('at+cscs="UCS2"')
            # Force DCS=8 (UCS2) per Quectel AT notes + open-source practice
            try:
                self._cmd("at+csmp=17,167,0,8")
            except ModemError:
                pass
            number_hex = text_to_ucs2(number)
            text_hex = text_to_ucs2(text)
            lines = self._cmd_prompt(f'at+cmgs="{number_hex}"', text_hex)
            self._cmd('at+cscs="GSM"')
            try:
                self._cmd("at+csmp=17,167,0,0")
            except ModemError:
                pass
        else:
            self._cmd('at+cscs="GSM"')
            try:
                self._cmd("at+csmp=17,167,0,0")
            except ModemError:
                pass
            lines = self._cmd_prompt(f'at+cmgs="{number}"', text)
        for line in lines:
            m = re.match(r"\+CMGS:\s*(\d+)", line)
            if m:
                return int(m.group(1))
        return 0

    def _sms_send_qcmgs(self, number, text, is_ucs2, uid, seg, total):
        """
        Quectel AT+QCMGS - Send one segment of a concatenated SMS.
        AT+QCMGS=<da>[,<toda>],<uid>,<msg_seg>,<msg_total>
        Module builds the UDH automatically. Execute once per segment.
        """
        self._cmd("at+cmgf=1")
        if is_ucs2:
            self._cmd('at+cscs="UCS2"')
            try:
                self._cmd("at+csmp=17,167,0,8")
            except ModemError:
                pass
            number_hex = text_to_ucs2(number)
            text_hex = text_to_ucs2(text)
            # QCMGS with hex-encoded number under UCS2
            lines = self._cmd_prompt(
                f'at+qcmgs="{number_hex}",{uid},{seg},{total}', text_hex)
            self._cmd('at+cscs="GSM"')
            try:
                self._cmd("at+csmp=17,167,0,0")
            except ModemError:
                pass
        else:
            self._cmd('at+cscs="GSM"')
            try:
                self._cmd("at+csmp=17,167,0,0")
            except ModemError:
                pass
            lines = self._cmd_prompt(
                f'at+qcmgs="{number}",{uid},{seg},{total}', text)
        for line in lines:
            # QCMGS returns +QCMGS: <mr> (or +CMGS: on some firmware)
            m = re.match(r"\+Q?CMGS:\s*(\d+)", line)
            if m:
                return int(m.group(1))
        return 0

    # ------------------------------------------------------------------
    # USSD
    # Source: 3GPP TS 27.007 §7.15 (AT+CUSD)
    # ------------------------------------------------------------------
    def ussd_send(self, code, timeout=20):
        """
        AT+CUSD=1,"<code>",15 - Send USSD request.
        Returns the USSD response string (may need multiple reads for
        interactive sessions; this returns the first response).

        Source: 3GPP TS 27.007 §7.15.
        <n>=1: enable result presentation. <dcs>=15: default alphabet.
        """
        # UCS2-encode the USSD code for compatibility
        code_hex = text_to_ucs2(code)
        old_timeout = self.timeout
        self.timeout = timeout
        try:
            lines = self._cmd(f'AT+CUSD=1,"{code_hex}",15')
        finally:
            self.timeout = old_timeout
        for line in lines:
            m = re.match(r'\+CUSD:\s*\d+,"([0-9A-Fa-f]+)",\d+', line)
            if m:
                return ucs2_to_text(m.group(1))
            # Plain-text fallback
            m2 = re.match(r'\+CUSD:\s*\d+,"([^"]*)"', line)
            if m2:
                return m2.group(2)
        return ""

    def ussd_cancel(self):
        """
        AT+CUSD=2 - Cancel USSD session.
        Source: 3GPP TS 27.007 §7.15.
        """
        self._cmd("AT+CUSD=2")

    # ------------------------------------------------------------------
    # Raw AT passthrough (for AT terminal feature)
    # ------------------------------------------------------------------
    def raw(self, cmd):
        """
        Send raw AT command, return (response_lines, ok_bool).
        Used by the web AT terminal. Never raises on ERROR.
        """
        try:
            lines = self._cmd(cmd, expect_ok=True)
            return lines, True
        except ModemError as e:
            return [str(e)], False


# ----------------------------------------------------------------------
# UCS2 helpers
# Source: 3GPP TS 23.038 - UCS2 is UTF-16BE for SMS.
# ----------------------------------------------------------------------
def text_to_ucs2(text):
    """Encode Python str to UCS2 hex string (UTF-16BE)."""
    return text.encode("utf-16-be").hex().upper()


def ucs2_to_text(hexstr):
    """Decode UCS2 hex string to Python str. Returns original on failure."""
    try:
        # Strip whitespace that some modems insert
        hexstr = re.sub(r"\s+", "", hexstr)
        return bytes.fromhex(hexstr).decode("utf-16-be")
    except Exception:
        return hexstr


def decode_ucs2(hexstr):
    """Alias for ucs2_to_text (public API)."""
    return ucs2_to_text(hexstr)


def is_ucs2_hex(s):
    """Heuristic: string looks like UCS2 hex (even length, hex chars)."""
    s = s.strip()
    return len(s) >= 4 and len(s) % 2 == 0 and \
        re.fullmatch(r"[0-9A-Fa-f]+", s) is not None
