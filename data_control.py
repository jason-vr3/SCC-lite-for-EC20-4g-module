#!/usr/bin/env python3
"""
SCC-lite - 蜂窝数据控制 (纯 QMI).

v0.5.5 (2026-10-09): 纯 QMI 路线.
  - ECM 模式已确认破坏语音 (ECM 下 ttyUSB2 无 AT 响应,
    Asterisk quectel0 "Not connec"), 故本版只做 QMI.
  - QMI 全链路 (2026-10-09 N1 实机验证):
    1) qmicli --wds-reset (清残留 bearer, 防 79 PolicyMismatch)
    2) qmicli --wda-set-data-format=raw-ip (模块侧)
    3) /sys/class/net/wwan0/qmi/raw_ip 写 Y (驱动侧, 需先 down)
    4) sysctl net.ipv6.conf.wwan0.disable_ipv6=0 (N1 默认禁用)
    5) qmicli --wds-start-network=apn=...,ip-type=4/6
    6) 按 QMI 下发地址配置 wwan0 (IPv4 用 /32, IPv6 用 /64)
    7) ping 验证连通性
  - IP 类型自动选择: 先试 IPv4, 不通再试 IPv6, 全程打 log.
  - IP 变化检测: 后台线程定期检查, 变化时页面提醒 + log.

模式切换: AT+QCFG="usbnet",<mode> + AT+CFUN=1,1 重启生效.
  0=PPP/QMI, 1=ECM, 2=MBIM, 3=RNDIS
  (本版数据控制只支持 QMI; ECM 切换按钮保留但数据功能走 QMI)
"""

import subprocess
import re
import logging
import os
import time

log = logging.getLogger("scc-lite.data")

# usbnet 模式名
USBNET_MODES = {0: "PPP/QMI", 1: "ECM", 2: "MBIM", 3: "RNDIS"}


def _run(cmd, timeout=30):
    """Run shell command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -2, "", f"command not found: {e}"


# ----------------------------------------------------------------------
# 模式查询与切换 (AT 指令, 需 Modem 实例)
# ----------------------------------------------------------------------
def get_usbnet_mode(modem):
    """
    查询当前 usbnet 模式.
    返回 (mode_int, mode_name), 如 (1, "ECM"); 失败返回 (None, "unknown").
    实机验证: AT+QCFG="usbnet" -> +QCFG: "usbnet",1
    """
    try:
        # modem.raw() 返回 (lines, ok), lines 是响应行列表
        resp, ok = modem.raw('AT+QCFG="usbnet"')
        resp_str = "\n".join(resp) if isinstance(resp, list) else str(resp)
        m = re.search(r'"usbnet",(\d)', resp_str)
        if m:
            mode = int(m.group(1))
            return mode, USBNET_MODES.get(mode, f"未知({mode})")
    except Exception as e:
        log.warning("get_usbnet_mode failed: %s", e)
    return None, "unknown"


def set_usbnet_mode(modem, mode):
    """
    设置 usbnet 模式 (0=QMI, 1=ECM, 2=MBIM, 3=RNDIS).
    注意: 需 AT+CFUN=1,1 重启才生效, 重启期间语音/短信中断约 1 分钟.
    返回 True/False. 仅下发指令, 不自动重启 (由调用方决定时机).
    """
    if mode not in USBNET_MODES:
        return False
    try:
        resp, ok = modem.raw(f'AT+QCFG="usbnet",{mode}')
        return ok
    except Exception as e:
        log.warning("set_usbnet_mode failed: %s", e)
        return False


def get_carrier_info(modem):
    """
    获取运营商信息: IMSI + COPS -> 查 APN 库.
    返回 {"carrier","apn","mcc","mnc","protocol","source",
           "imsi","operator"}; 识别失败则 apn 等为空.
    """
    from apn_db import detect_carrier
    imsi, operator = "", ""
    try:
        imsi = modem.get_imsi() or ""
    except Exception:
        pass
    try:
        operator = modem.get_operator() or ""
    except Exception:
        pass
    hit = detect_carrier(imsi=imsi, cops_name=operator)
    info = {"carrier": "", "apn": "", "mcc": "", "mnc": "",
            "protocol": "", "source": "", "imsi": imsi,
            "operator": operator}
    if hit:
        info.update({k: hit.get(k, "") for k in
                     ("carrier", "apn", "mcc", "mnc", "protocol",
                      "source")})
    return info


def provision_apn(modem, apn, pdp_type="IPV4V6"):
    """
    下发 APN 到模块: AT+CGDCONT=1,"<pdp_type>","<apn>".
    返回 True/False.
    注意: 重启后需重新下发 (EC20 不一定持久化, 2026-10-08 实测重启丢失).
    """
    if not apn:
        return False
    try:
        # pdp_type 仅允许 IP/IPV6/IPV4V6, 防注入
        if pdp_type not in ("IP", "IPV6", "IPV4V6"):
            pdp_type = "IPV4V6"
        # apn 只允许常规字符
        if not re.fullmatch(r"[A-Za-z0-9._-]+", apn):
            log.warning("非法 APN: %s", apn)
            return False
        resp, ok = modem.raw(f'AT+CGDCONT=1,"{pdp_type}","{apn}"')
        return ok
    except Exception as e:
        log.warning("provision_apn failed: %s", e)
        return False


# ----------------------------------------------------------------------
# ECM 模式已移除 (v0.5.5, 2026-10-09)
# ----------------------------------------------------------------------
# 原因: ECM 下 ttyUSB2 无 AT 响应, Asterisk quectel0 "Not connec",
#       语音中断. 本版纯 QMI.
# 旧 EcmDataControl 代码见 data_control.py.bak-ecm-20261009.

# ----------------------------------------------------------------------
# QMI 模式数据控制 (保留, 兼容旧逻辑)
# ----------------------------------------------------------------------
class DataControl:
    """
    QMI 数据连接管理 (v0.5.5 纯 QMI).

    全自动链路 (2026-10-09 N1 实机验证):
      1) qmicli --wds-reset                    # 清残留 bearer
      2) qmicli --wda-set-data-format=raw-ip   # 模块侧 raw-ip
      3) 驱动 /sys/.../qmi/raw_ip 写 Y          # 驱动侧 (需先 down)
      4) sysctl disable_ipv6=0                 # N1 默认禁用 IPv6
      5) qmicli --wds-start-network            # 建 bearer (自动选 IP 类型)
      6) 按 QMI 下发地址配 wwan0
      7) ping 验证

    IP 类型自动选择: 先 IPv4, 不通再试 IPv6. 全程打 log.
    IP 变化检测: 后台线程每 60 秒检查, 变化时记录 + 标记.

    用法:
        dc = DataControl(qmi_dev="/dev/cdc-wdm0", iface="wwan0", apn="cbnet")
        dc.start()   # 自动拨号
        dc.stop()    # 断开
    """

    # IP 变化检测间隔 (秒). 用户要求"不要太频繁".
    IP_CHECK_INTERVAL = 60

    def __init__(self, qmi_dev="/dev/cdc-wdm0", iface="wwan0", apn="",
                 auth=None, state_dir="/opt/scc-lite-for-EC20-4g-module/data"):
        self.qmi_dev = qmi_dev
        self.iface = iface
        self.apn = apn
        self.auth = auth
        self._state_file = os.path.join(state_dir, "qmi_handle")
        self._handle, self._cid = self._load_handle()
        # 当前生效的 IP 类型: 4 / 6 / None
        self._ip_type = None
        # IP 变化检测
        self._last_ipv4 = ""
        self._last_ipv6 = []
        self._ip_changed = False
        self._ip_change_msg = ""
        self._monitor_thread = None
        self._monitor_stop = False
        # 操作日志 (供 Web 页面展示)
        self._op_log = []
        self._op_log_max = 200

    # -- 操作日志 --
    def _olog(self, msg):
        """记录操作日志, 供 Web 页面展示分析."""
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        log.info("QMI: %s", msg)
        self._op_log.append(entry)
        if len(self._op_log) > self._op_log_max:
            self._op_log.pop(0)

    def get_op_log(self, last_n=50):
        """返回最近的操作日志."""
        return self._op_log[-last_n:]

    def clear_op_log(self):
        self._op_log.clear()

    # -- handle 持久化 --
    def _load_handle(self):
        try:
            with open(self._state_file) as f:
                parts = f.read().strip().split(",")
                if len(parts) == 2:
                    return parts[0], parts[1]
                elif len(parts) == 1 and parts[0]:
                    return parts[0], None
        except (FileNotFoundError, OSError):
            pass
        return None, None

    def _save_handle(self, handle, cid=None):
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                f.write(f"{handle},{cid or ''}")
        except OSError as e:
            log.warning("save handle failed: %s", e)

    def _clear_handle(self):
        self._handle = None
        self._cid = None
        self._ip_type = None
        try:
            os.remove(self._state_file)
        except OSError:
            pass

    # -- 底层步骤 (每步都打 log) --
    def _step_reset(self):
        """Step 1: 清理残留 bearer."""
        self._olog("Step 1: 清理残留 bearer (wds-reset)")
        rc, out, err = _run(
            ["qmicli", "-d", self.qmi_dev, "--wds-reset"], timeout=15)
        ok = rc == 0 and "Successfully" in out
        self._olog(f"  -> {'成功' if ok else '失败: ' + (err or out)[:100]}")
        return ok

    def _step_data_format(self):
        """Step 2: 模块侧设为 raw-ip."""
        self._olog("Step 2: 模块侧设置 raw-ip (wda-set-data-format)")
        rc, out, err = _run(
            ["qmicli", "-d", self.qmi_dev,
             "--wda-set-data-format=raw-ip"], timeout=15)
        ok = rc == 0
        self._olog(f"  -> {'成功' if ok else '失败: ' + (err or out)[:100]}")
        return ok

    def _step_driver_raw_ip(self):
        """Step 3: 驱动侧 raw_ip 写 Y (需先 down 接口)."""
        self._olog("Step 3: 驱动侧 raw_ip=Y")
        raw_ip_path = f"/sys/class/net/{self.iface}/qmi/raw_ip"
        try:
            cur = open(raw_ip_path).read().strip()
            self._olog(f"  当前值: {cur}")
            if cur == "Y":
                self._olog("  -> 已是 Y, 跳过")
                return True
        except OSError as e:
            self._olog(f"  -> 读取失败: {e}")
            return False
        _run(["ip", "link", "set", self.iface, "down"], timeout=10)
        try:
            with open(raw_ip_path, "w") as f:
                f.write("Y\n")
            self._olog("  -> 写入 Y 成功")
        except OSError as e:
            self._olog(f"  -> 写入失败: {e}")
            return False
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        return True

    def _step_enable_ipv6(self):
        """Step 4: 打开 wwan0 的 IPv6 (N1 默认禁用)."""
        self._olog("Step 4: 启用 wwan0 IPv6 (disable_ipv6=0)")
        rc, out, err = _run(
            ["sysctl", "-w",
             f"net.ipv6.conf.{self.iface}.disable_ipv6=0"], timeout=10)
        ok = rc == 0
        self._olog(f"  -> {'成功' if ok else '失败: ' + err[:100]}")
        return ok

    def _step_start_network(self, ip_type):
        """
        Step 5: 建 bearer.
        ip_type: 4 或 6.
        返回 (handle, cid) 或 (None, None).
        """
        self._olog(f"Step 5: 建立 bearer (ip-type={ip_type}, apn={self.apn or '(默认)'})")
        cmd = ["qmicli", "-d", self.qmi_dev]
        if self.apn:
            cmd.append(f"--wds-start-network=apn={self.apn},ip-type={ip_type}")
        else:
            cmd.append(f"--wds-start-network=ip-type={ip_type}")
        if self.auth:
            cmd.append(f"--wds-auth={self.auth}")
        cmd.append("--client-no-release-cid")
        rc, out, err = _run(cmd, timeout=60)
        if rc != 0:
            self._olog(f"  -> 失败: {(err or out)[:200]}")
            return None, None
        m = re.search(r"Packet data handle:\s*'(\d+)'", out)
        c = re.search(r"CID:\s*'(\d+)'", out)
        if not m:
            self._olog(f"  -> 未解析到 handle: {out[:200]}")
            return None, None
        handle, cid = m.group(1), (c.group(1) if c else None)
        self._olog(f"  -> 成功 handle={handle} cid={cid}")
        return handle, cid

    def _step_get_settings(self):
        """Step 6a: 从 QMI 获取地址/网关/DNS."""
        self._olog("Step 6a: 获取 QMI 下发地址")
        cmd = ["qmicli", "-d", self.qmi_dev, "--wds-get-current-settings"]
        if self._cid:
            cmd += ["--client-cid=" + self._cid, "--client-no-release-cid"]
        rc, out, err = _run(cmd, timeout=15)
        if rc != 0:
            self._olog(f"  -> 失败: {err[:100]}")
            return {}
        info = {}
        patterns = [
            ("ipv4", r"IPv4 address:\s*([\d.]+)/(\d+)"),
            ("ipv4_gw", r"IPv4 gateway address:\s*([\d.]+)"),
            ("ipv6", r"IPv6 address:\s*([0-9a-fA-F:]+)/(\d+)"),
            ("ipv6_gw", r"IPv6 gateway address:\s*([0-9a-fA-F:]+)"),
            ("mtu", r"MTU:\s*(\d+)"),
        ]
        for key, pat in patterns:
            m = re.search(pat, out)
            if m:
                if m.lastindex == 2:
                    info[key] = m.group(1) + f"/{m.group(2)}"
                else:
                    info[key] = m.group(1)
        self._olog(f"  -> {info}")
        return info

    def _step_config_iface(self, info, ip_type):
        """Step 6b: 按 QMI 地址配置 wwan0."""
        self._olog(f"Step 6b: 配置 {self.iface}")
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        if ip_type == 4 and info.get("ipv4"):
            # IPv4 用 /32 (实测 /30 报 Address already assigned)
            ip = info["ipv4"].split("/")[0] + "/32"
            self._olog(f"  添加 IPv4: {ip}")
            rc, _, err = _run(
                ["ip", "-4", "addr", "add", ip, "dev", self.iface],
                timeout=10)
            if rc != 0 and "already" not in err and "exists" not in err:
                self._olog(f"  -> 失败: {err[:100]}")
                return False
            self._olog("  -> 成功")
            return True
        elif ip_type == 6 and info.get("ipv6"):
            ip = info["ipv6"]
            self._olog(f"  添加 IPv6: {ip}")
            rc, _, err = _run(
                ["ip", "-6", "addr", "add", ip, "dev", self.iface],
                timeout=10)
            if rc != 0 and "already" not in err and "exists" not in err:
                self._olog(f"  -> 失败: {err[:100]}")
                return False
            self._olog("  -> 成功")
            return True
        self._olog("  -> 无可用地址")
        return False

    def _step_verify(self, ip_type):
        """Step 7: ping 验证连通性 (不设默认路由, 用 -I)."""
        target = "114.114.114.114" if ip_type == 4 else "2400:3200::1"
        self._olog(f"Step 7: ping 验证 ({target})")
        r = self.ping_test(target, count=3)
        self._olog(f"  -> {'通' if r['ok'] else '不通'} "
                   f"(丢包 {r['loss_pct']}%, 平均 {r['avg_ms']}ms)")
        return r["ok"]

    # -- 主流程 --
    def _try_ip_type(self, ip_type):
        """尝试一种 IP 类型, 返回 True/False. 全程 log."""
        self._olog(f"===== 尝试 IPv{ip_type} =====")
        if not self._step_reset():
            return False
        if not self._step_data_format():
            return False
        if not self._step_driver_raw_ip():
            return False
        if ip_type == 6:
            self._step_enable_ipv6()  # 失败不致命, 继续
        handle, cid = self._step_start_network(ip_type)
        if not handle:
            return False
        self._handle, self._cid = handle, cid
        self._save_handle(handle, cid)
        time.sleep(2)
        info = self._step_get_settings()
        if not self._step_config_iface(info, ip_type):
            return False
        if not self._step_verify(ip_type):
            self._olog(f"IPv{ip_type} ping 不通, 放弃")
            return False
        self._ip_type = ip_type
        self._olog(f"===== IPv{ip_type} 连接成功 =====")
        return True

    def start(self):
        """
        QMI 自动拨号: 先试 IPv4, 不通再试 IPv6.
        返回 True/False. 全程打 log, 可通过 get_op_log() 查看.
        """
        self._olog("开始 QMI 自动拨号")
        self._ip_changed = False
        # 先试 IPv4 (最通用)
        if self._try_ip_type(4):
            self._start_monitor()
            return True
        self._olog("IPv4 失败, 尝试 IPv6")
        # 清理 IPv4 的 bearer 再试 IPv6
        self._stop_bearer()
        if self._try_ip_type(6):
            self._start_monitor()
            return True
        self._olog("IPv4/IPv6 均失败")
        return False

    def _stop_bearer(self):
        """断开当前 bearer (内部用)."""
        if self._handle:
            cmd = ["qmicli", "-d", self.qmi_dev,
                   f"--wds-stop-network={self._handle}"]
            if self._cid:
                cmd.append(f"--client-cid={self._cid}")
            _run(cmd, timeout=30)
        self._clear_handle()

    def stop(self):
        """关闭上网: 断 bearer + down 接口."""
        self._olog("关闭 QMI 连接")
        self._stop_monitor()
        self._stop_bearer()
        _run(["ip", "link", "set", self.iface, "down"], timeout=10)
        self._olog("已关闭")
        return True

    def is_connected(self):
        if not self._handle:
            self._handle, self._cid = self._load_handle()
            if not self._handle:
                return False
        cmd = ["qmicli", "-d", self.qmi_dev,
               "--wds-get-packet-service-status"]
        if self._cid:
            cmd.append(f"--client-cid={self._cid}")
        rc, out, _ = _run(cmd, timeout=15)
        return rc == 0 and "'connected'" in out

    # -- 地址查询 --
    def get_ipv4(self):
        rc, out, _ = _run(["ip", "-4", "addr", "show", self.iface],
                          timeout=10)
        if rc != 0:
            return ""
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else ""

    def get_ipv6(self):
        rc, out, _ = _run(["ip", "-6", "addr", "show", self.iface],
                          timeout=10)
        if rc != 0:
            return []
        return re.findall(r"inet6\s+([0-9a-fA-F:]+)/\d+\s+scope global",
                          out)

    # -- IP 变化检测 (后台线程) --
    def _start_monitor(self):
        """启动 IP 变化监控线程."""
        self._stop_monitor()
        self._last_ipv4 = self.get_ipv4()
        self._last_ipv6 = self.get_ipv6()
        self._ip_changed = False
        self._monitor_stop = False
        import threading
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        self._olog(f"IP 监控已启动 (间隔 {self.IP_CHECK_INTERVAL}s)")

    def _stop_monitor(self):
        self._monitor_stop = True
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)
        self._monitor_thread = None

    def _monitor_loop(self):
        """监控循环: 检查 IP 变化."""
        while not self._monitor_stop:
            time.sleep(self.IP_CHECK_INTERVAL)
            if self._monitor_stop:
                break
            try:
                cur_v4 = self.get_ipv4()
                cur_v6 = self.get_ipv6()
                changed = []
                if cur_v4 != self._last_ipv4:
                    changed.append(
                        f"IPv4: {self._last_ipv4 or '(无)'} -> {cur_v4 or '(无)'}")
                if set(cur_v6) != set(self._last_ipv6):
                    changed.append(f"IPv6: {self._last_ipv6} -> {cur_v6}")
                if changed:
                    msg = "; ".join(changed)
                    self._ip_changed = True
                    self._ip_change_msg = msg
                    self._olog(f"IP 变化: {msg}")
                    log.warning("QMI IP changed: %s", msg)
                self._last_ipv4 = cur_v4
                self._last_ipv6 = cur_v6
            except Exception as e:
                log.warning("IP monitor error: %s", e)

    def get_ip_change_info(self):
        """返回 IP 变化信息 (页面提醒用)."""
        return {"changed": self._ip_changed, "msg": self._ip_change_msg}

    def clear_ip_change_flag(self):
        """清除 IP 变化标记 (用户已读)."""
        self._ip_changed = False
        self._ip_change_msg = ""

    # -- 状态 --
    def get_status(self):
        bearer = self.is_connected()
        # v0.5.6: 永远返回网卡真实 IP (bearer 断了也显示, 仪表盘与蜂窝页一致).
        # connected = bearer 已建立 或 网卡上有 IP (有 IP 即视为"已上网",
        # 允许点"关闭上网"清理; ping 通不通由 check_internet 单独判断).
        ipv4 = self.get_ipv4()
        ipv6 = self.get_ipv6()
        connected = bearer or bool(ipv4) or bool(ipv6)
        return {
            "mode": "qmi",
            "mode_name": "QMI",
            "connected": connected,
            "bearer": bearer,
            "iface": self.iface,
            "ip_type": self._ip_type,
            "ipv4": ipv4,
            "ipv6": ipv6,
            # 兼容旧字段
            "ip": ipv4 or (ipv6[0] if ipv6 else ""),
            # IP 变化提醒
            "ip_changed": self._ip_changed,
            "ip_change_msg": self._ip_change_msg,
        }

    def ping_test(self, target, count=3, timeout=10):
        """Ping 测试 (自动选 -4/-6, 用 -I 指定接口, 不改路由)."""
        is_v6 = ":" in target
        cmd = (["ping", "-6"] if is_v6 else ["ping"]) + \
              ["-c", str(count), "-W", "3", "-I", self.iface, target]
        rc, out, err = _run(cmd, timeout=timeout + 10)
        result = {"ok": rc == 0, "avg_ms": 0.0, "loss_pct": 100.0,
                  "raw": out or err}
        m = re.search(r"(\d+)% packet loss", out)
        if m:
            result["loss_pct"] = float(m.group(1))
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
        if m:
            result["avg_ms"] = float(m.group(1))
        return result

    def check_internet(self):
        """
        三段式连通性检查 (v0.5.5):
          1. bearer: QMI bearer 是否建立
          2. ip: wwan0 是否拿到 IP (v4/v6 分开)
          3. internet: v4/v6 公网 ping 是否通 (两个都测)
        返回 dict, 供页面分段显示绿/红.
        """
        result = {
            "bearer": False,
            "bearer_detail": "",
            "has_ipv4": False,
            "has_ipv6": False,
            "ipv4": "",
            "ipv6": [],
            "internet_v4": None,  # None=未测, True=通, False=不通
            "internet_v6": None,
            "v4_detail": {},
            "v6_detail": {},
        }
        # 1. bearer
        result["bearer"] = self.is_connected()
        if self._handle:
            result["bearer_detail"] = f"handle={self._handle}"
        # 2. IP (v0.5.6: 不依赖 bearer, 永远读网卡真实地址;
        #    bearer 断但网卡有 IP 时, 照样显示并允许 ping 测)
        v4 = self.get_ipv4()
        v6 = self.get_ipv6()
        result["ipv4"] = v4
        result["ipv6"] = v6
        result["has_ipv4"] = bool(v4)
        result["has_ipv6"] = bool(v6)
        # 3. 互联网 (两个都测, 各 2 个包快测)
        if result["has_ipv4"]:
            r = self.ping_test("114.114.114.114", count=2, timeout=8)
            result["internet_v4"] = r["ok"]
            result["v4_detail"] = r
        if result["has_ipv6"]:
            r = self.ping_test("2400:3200::1", count=2, timeout=8)
            result["internet_v6"] = r["ok"]
            result["v6_detail"] = r
        return result



# ----------------------------------------------------------------------
# 工厂: v0.5.5 纯 QMI
# ----------------------------------------------------------------------
def get_data_controller(config, modem_factory=None, at_port="/dev/ttyUSB3"):
    """
    v0.5.5: 纯 QMI, 忽略 mode 配置 (ECM 已移除).
    config["data"] 示例:
      {"qmi_dev": "/dev/cdc-wdm0", "wwan_iface": "wwan0", "apn": "cbnet"}
    """
    d = config.get("data", {}) if isinstance(config, dict) else {}
    return DataControl(
        qmi_dev=d.get("qmi_dev", "/dev/cdc-wdm0"),
        iface=d.get("wwan_iface", d.get("iface", "wwan0")),
        apn=d.get("apn", ""))
