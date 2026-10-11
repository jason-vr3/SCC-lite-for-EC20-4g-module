#!/usr/bin/env python3
"""
SCC-lite - 蜂窝数据控制 (纯 QMI).

v0.5.8 (2026-10-11): IPv4 改走 DHCP (udhcpc + 自定义禁路由脚本).
  - QMI 全链路 (2026-10-11 N1 实机验证):
    1) qmicli --wds-reset (清残留 bearer, 防 79 PolicyMismatch)
    2) qmicli --wda-set-data-format=raw-ip (模块侧)
    3) /sys/class/net/wwan0/qmi/raw_ip 写 Y (驱动侧, 需先 down)
    4) sysctl net.ipv6.conf.wwan0.disable_ipv6=0 (N1 默认禁用, 仅 IPv6 需要)
    5) qmicli --wds-start-network=apn=...,ip-type=4/6
    6) IPv4: udhcpc -s udhcpc-noroute.sh 从模块 DHCP 取地址
       (只配 IP/MTU, 绝不加路由, 铁律; 不再解析 qmicli 输出)
       IPv6: 按 QMI 下发地址配置 wwan0 (单行 CIDR, 如 xxxx::1/64)
    7) ping 验证连通性
  - 依赖: udhcpc (apt install udhcpc; install.sh 自动安装)
  - 关闭: 先 kill udhcpc, 再清 IP/down 接口, 最后断 bearer.

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
        # v0.5.7: 超时后强制杀残留进程, 避免 qmicli 卡死占住 QMI
        try:
            if cmd and cmd[0] == "qmicli":
                subprocess.run(["pkill", "-9", "qmicli"],
                               capture_output=True, timeout=5)
        except Exception:
            pass
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -2, "", f"command not found: {e}"


def _qmi_run(cmd, timeout=30, retries=2):
    """
    v0.5.7: 健壮的 qmicli 执行器.
    QMI 接口偶发卡死, 失败时自动 pkill + wds-reset 后重试.
    返回 (returncode, stdout, stderr). 全部重试失败返回最后一次结果.
    """
    last = (-1, "", "not run")
    for attempt in range(retries + 1):
        rc, out, err = _run(cmd, timeout=timeout)
        if rc == 0:
            return rc, out, err
        last = (rc, out, err)
        if attempt < retries:
            log.warning("qmicli 失败 (尝试 %d/%d): %s, 重置 QMI 后重试",
                        attempt + 1, retries + 1, (err or out)[:100])
            # 杀残留 + 重置 WDS
            try:
                subprocess.run(["pkill", "-9", "qmicli"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(2)
            try:
                # 从 cmd 里提取 qmi_dev
                dev = "/dev/cdc-wdm0"
                for i, a in enumerate(cmd):
                    if a == "-d" and i + 1 < len(cmd):
                        dev = cmd[i + 1]
                        break
                subprocess.run(["qmicli", "-d", dev, "--wds-reset"],
                               capture_output=True, timeout=20)
            except Exception:
                pass
            time.sleep(2)
    return last


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
                 auth=None, state_dir="/opt/scc-lite-for-EC20-4g-module/data",
                 modem_factory=None):
        self.qmi_dev = qmi_dev
        self.iface = iface
        self.apn = apn
        self.auth = auth
        # v0.5.8: IMSI 自动识别用的 modem 工厂 (后台线程并行识别)
        self._modem_factory = modem_factory
        # 后台识别出的 APN (线程写入, 主流程读取)
        self._detected_apn = ""
        self._detected_carrier = ""
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
        # udhcpc 前台进程 (续租用)
        self._udhcpc_proc = None
        self._udhcpc_logf = None
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
        self._olog("Step 1: 清理残留 bearer")
        self._olog(f"  $ qmicli -d {self.qmi_dev} --wds-reset")
        rc, out, err = _qmi_run(["qmicli", "-d", self.qmi_dev, "--wds-reset"], timeout=15)
        ok = rc == 0 and "Successfully" in out
        self._olog(f"  -> {'成功' if ok else '失败: ' + (err or out)[:100]}")
        return ok

    def _step_data_format(self):
        """Step 2: 模块侧设为 raw-ip (已是则跳过)."""
        self._olog("Step 2: 模块侧设置 raw-ip")
        self._olog(f"  $ qmicli -d {self.qmi_dev} --wda-get-data-format")
        rc, out, _ = _qmi_run(["qmicli", "-d", self.qmi_dev,
             "--wda-get-data-format"], timeout=15)
        if rc == 0 and "raw-ip" in out.lower():
            self._olog("  -> 已是 raw-ip, 跳过")
            return True
        self._olog(f"  $ qmicli -d {self.qmi_dev} --wda-set-data-format=raw-ip")
        rc, out, err = _qmi_run(["qmicli", "-d", self.qmi_dev,
             "--wda-set-data-format=raw-ip"], timeout=15)
        ok = rc == 0
        self._olog(f"  -> {'成功' if ok else '失败: ' + (err or out)[:100]}")
        if not ok:
            # 设置失败不致命: DHCP 步骤会实测数据通道, 不通则拨号失败
            self._olog("  -> 警告: 继续 (数据通道可能已就绪, 由 DHCP 验证)")
        return True

    def _step_driver_raw_ip(self):
        """Step 3: 驱动侧 raw_ip 写 Y (需先 down 接口)."""
        self._olog("Step 3: 驱动侧 raw_ip=Y")
        raw_ip_path = f"/sys/class/net/{self.iface}/qmi/raw_ip"
        self._olog(f"  $ cat {raw_ip_path}")
        try:
            cur = open(raw_ip_path).read().strip()
            self._olog(f"  当前值: {cur}")
            if cur == "Y":
                self._olog("  -> 已是 Y, 跳过")
                return True
        except OSError as e:
            self._olog(f"  -> 读取失败: {e}")
            return False
        self._olog(f"  $ ip link set {self.iface} down")
        _run(["ip", "link", "set", self.iface, "down"], timeout=10)
        try:
            self._olog(f"  $ echo Y > {raw_ip_path}")
            with open(raw_ip_path, "w") as f:
                f.write("Y\n")
            self._olog("  -> 写入 Y 成功")
        except OSError as e:
            self._olog(f"  -> 写入失败: {e}")
            return False
        self._olog(f"  $ ip link set {self.iface} up")
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        return True

    def _step_enable_ipv6(self):
        """Step 4: 打开 wwan0 的 IPv6 (N1 默认禁用)."""
        self._olog("Step 4: 启用 wwan0 IPv6")
        self._olog(f"  $ sysctl -w net.ipv6.conf.{self.iface}.disable_ipv6=0")
        rc, out, err = _run(
            ["sysctl", "-w",
             f"net.ipv6.conf.{self.iface}.disable_ipv6=0"], timeout=10)
        ok = rc == 0
        self._olog(f"  -> {'成功' if ok else '失败: ' + err[:100]}")
        return ok

    def _step_start_network(self, ip_type, apn):
        """Step 5: 建 bearer (尽力而为, 失败不致命).
        apn: None=不带 apn 参数, 字符串=指定 APN.
        返回 (handle, cid) 或 (None, None).
        v0.5.8: PolicyMismatch/失败都不判死, DHCP 是硬标准."""
        use_apn = apn
        self._olog(f"Step 5: 建立 bearer (ip-type={ip_type}, "
                   f"apn={use_apn or '(无)'})")
        cmd = ["qmicli", "-d", self.qmi_dev]
        if use_apn:
            cmd.append(f"--wds-start-network=apn={use_apn},ip-type={ip_type}")
        else:
            cmd.append(f"--wds-start-network=ip-type={ip_type}")
        if self.auth:
            cmd.append(f"--wds-auth={self.auth}")
        cmd.append("--client-no-release-cid")
        self._olog(f"  $ {' '.join(cmd)}")
        rc, out, err = _qmi_run(cmd, timeout=60)
        if rc != 0:
            self._olog(f"  -> 失败 (继续 DHCP): {((err or out) or '')[:200]}")
            # 尝试解析 CID (PolicyMismatch 时也会分配 CID)
            c = re.search(r"CID:\s*'(\d+)'", out or "")
            return None, (c.group(1) if c else None)
        m = re.search(r"Packet data handle:\s*'(\d+)'", out)
        c = re.search(r"CID:\s*'(\d+)'", out)
        if not m:
            self._olog(f"  -> 未解析到 handle: {out[:200]}")
            return None, (c.group(1) if c else None)
        handle, cid = m.group(1), (c.group(1) if c else None)
        self._olog(f"  -> 成功 handle={handle} cid={cid}")
        return handle, cid

    def _step_get_settings(self):
        """Step 6a (仅 IPv6 用): 从 QMI 获取地址/网关.
        v0.5.8: IPv4 已改走 DHCP, 此函数不再解析 IPv4."""
        self._olog("Step 6a: 获取 QMI 下发地址 (IPv6)")
        cmd = ["qmicli", "-d", self.qmi_dev, "--wds-get-current-settings"]
        if self._cid:
            cmd += ["--client-cid=" + self._cid, "--client-no-release-cid"]
        self._olog(f"  $ {' '.join(cmd)}")
        rc, out, err = _qmi_run(cmd, timeout=15)
        if rc != 0:
            self._olog(f"  -> 失败: {err[:100]}")
            return {}
        info = {}
        # IPv6 是单行 CIDR 格式: IPv6 address: xxxx::1/64
        patterns = [
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
        """Step 6b (仅 IPv6 用): 按 QMI 地址配置 wwan0.
        v0.5.8: IPv4 已改走 DHCP, 不再经此函数."""
        self._olog(f"Step 6b: 配置 {self.iface} (IPv6)")
        self._olog(f"  $ ip link set {self.iface} up")
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        if ip_type == 6 and info.get("ipv6"):
            ip = info["ipv6"]
            self._olog(f"  $ ip -6 addr add {ip} dev {self.iface}")
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

    # -- DHCP (v0.5.8, IPv4 专用) --
    UDHCPC_PIDFILE = "/run/udhcpc-wwan0.pid"
    UDHCPC_DNSFILE = "/run/udhcpc-wwan0.dns"

    def _udhcpc_script(self):
        """自定义 DHCP 脚本路径 (与 data_control.py 同目录)."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "udhcpc-noroute.sh")

    def _kill_udhcpc(self):
        """杀掉 wwan0 的 udhcpc 残留进程 (Popen 对象 + pidfile + pkill 兜底)."""
        # 先杀 Popen 对象 (前台模式)
        proc = getattr(self, "_udhcpc_proc", None)
        if proc and proc.poll() is None:
            try:
                self._olog(f"  $ kill -15 {proc.pid} (Popen)")
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._udhcpc_proc = None
        lf = getattr(self, "_udhcpc_logf", None)
        if lf:
            try:
                lf.close()
            except Exception:
                pass
            self._udhcpc_logf = None
        try:
            with open(self.UDHCPC_PIDFILE) as f:
                pid = int(f.read().strip())
            self._olog(f"  $ kill -15 {pid}")
            os.kill(pid, 15)
            self._olog(f"  已停止 udhcpc (pid {pid})")
        except (OSError, ValueError):
            pass
        self._olog(f"  $ pkill -f 'udhcpc.*-i {self.iface}'")
        _run(["pkill", "-f", f"udhcpc.*-i {self.iface}"], timeout=5)
        try:
            os.unlink(self.UDHCPC_PIDFILE)
        except OSError:
            pass

    def _clean_wwan_routes(self):
        """防御性清理 wwan0 的默认路由 (铁律).
        v0.5.8: 拨号时无论路由从何而来, 一律检查并清除."""
        rc, out, _ = _run(
            ["ip", "route", "show", "default", "dev", self.iface],
            timeout=10)
        if out.strip():
            for line in out.strip().splitlines():
                self._olog(f"  发现 wwan0 默认路由残留, 清除: {line.strip()}")
            self._olog(f"  $ ip route del default dev {self.iface}")
            _run(["ip", "route", "del", "default", "dev", self.iface],
                 timeout=10)
        # 确认清干净
        rc, out, _ = _run(
            ["ip", "route", "show", "default", "dev", self.iface],
            timeout=10)
        if out.strip():
            self._olog(f"  !! 路由清理失败, 仍有: {out.strip()}")
            return False
        self._olog("  wwan0 默认路由检查通过 (无残留)")
        return True

    def _read_udhcpc_dns(self):
        """读取 DHCP 下发的 DNS (脚本写入, 仅展示用)."""
        try:
            with open(self.UDHCPC_DNSFILE) as f:
                return [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return []

    def _detect_apn_async(self):
        """后台线程: IMSI 自动识别 APN (与拨号并行, 不阻塞主流程).
        结果写入 self._detected_apn / self._detected_carrier.
        串口忙时重试 3 次."""
        import time as _time
        self._detected_apn = ""
        self._detected_carrier = ""
        if not self._modem_factory:
            return
        for attempt in range(3):
            try:
                m = self._modem_factory()
                try:
                    info = get_carrier_info(m)
                    self._detected_apn = (info.get("apn") or "").strip()
                    self._detected_carrier = info.get("carrier", "")
                    if self._detected_apn:
                        self._olog(f"  IMSI 识别: {self._detected_carrier} -> "
                                   f"{self._detected_apn} (已记录)")
                        return
                    self._olog("  IMSI 识别: 未命中 APN 库")
                    return
                finally:
                    try:
                        m.close()
                    except Exception:
                        pass
            except Exception as e:
                self._olog(f"  IMSI 识别异常 (尝试 {attempt+1}/3): {e}")
                _time.sleep(2)
        self._olog("  IMSI 识别失败, 无 fallback APN")

    def _step_dhcp(self):
        """Step 6 (IPv4): udhcpc 从模块 DHCP 获取地址.
        自定义脚本只配 IP/MTU, 绝不加路由 (铁律), 不动系统 DNS.
        v0.5.8-fix: 用 -f 前台模式 (与手动验证一致), 不再用 -b.
        (-b 后台 fork 在软件上下文中不稳定, 手动 -f 次次成功.)"""
        import shutil
        self._olog("Step 6: DHCP 获取地址 (udhcpc)")
        if not shutil.which("udhcpc"):
            self._olog("  -> 失败: 未找到 udhcpc, 请执行 apt install udhcpc")
            return False
        script = self._udhcpc_script()
        if not os.path.isfile(script):
            self._olog(f"  -> 失败: 缺少脚本 {script}")
            return False
        if not os.access(script, os.X_OK):
            try:
                os.chmod(script, 0o755)
                self._olog(f"  脚本无执行权限, 已自动 chmod +x: {script}")
            except OSError as e:
                self._olog(f"  -> 失败: 脚本无执行权限且无法修复 {script}: {e}")
                return False
        # 先清理残留实例, 防止旧进程干扰
        self._kill_udhcpc()
        # v0.5.8-fix2: wds-reset 可能真清掉 bearer, 接口变 DOWN,
        # udhcpc 会报 "Network is down". 先确保 UP.
        self._olog(f"  $ ip link set {self.iface} up")
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        # -f 前台, -n 拿不到 lease 就退出, 不加 -q (保持运行以续租)
        # 输出记到日志文件, 便于排障 (不再 DEVNULL)
        logf = "/tmp/udhcpc-wwan0.log"
        cmd = ["udhcpc", "-f", "-n", "-i", self.iface, "-s", script,
               "-p", self.UDHCPC_PIDFILE]
        self._olog(f"  $ {' '.join(cmd)} > {logf} 2>&1")
        try:
            lf = open(logf, "w")
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            self._udhcpc_proc = proc
            self._udhcpc_logf = lf
        except OSError as e:
            self._olog(f"  -> 启动失败: {e}")
            return False
        # 轮询等 IP (最多 25s)
        deadline = time.time() + 25
        while time.time() < deadline:
            # -n 模式下拿不到 lease 会自己退出, 提前判死
            if proc.poll() is not None:
                self._olog(f"  -> udhcpc 已退出 (rc={proc.returncode}), "
                           f"见 {logf}")
                try:
                    self._olog("  udhcpc 输出:\n" +
                               open(logf).read()[-500:])
                except OSError:
                    pass
                return False
            v4 = self.get_ipv4()
            if v4:
                self._olog(f"  -> 成功: {self.iface} {v4}")
                dns = self._read_udhcpc_dns()
                if dns:
                    self._olog(f"  -> DNS: {', '.join(dns)}")
                # v0.5.8: DHCP 后再次防御性清理 wwan0 默认路由 (铁律)
                self._clean_wwan_routes()
                return True
            time.sleep(1)
        self._olog("  -> 失败: 25s 内未获取到 IPv4")
        try:
            self._olog("  udhcpc 输出:\n" + open(logf).read()[-500:])
        except OSError:
            pass
        self._kill_udhcpc()
        return False

    def _step_verify(self, ip_type):
        """Step 7: ping 验证连通性 (不设默认路由, 用 -I)."""
        target = "223.5.5.5" if ip_type == 4 else "2400:3200::1"
        ver = "-4" if ip_type == 4 else "-6"
        self._olog(f"Step 7: ping 验证 ({target})")
        self._olog(f"  $ ping {ver} -c 3 -W 3 -I {self.iface} {target}")
        r = self.ping_test(target, count=3)
        self._olog(f"  -> {'通' if r['ok'] else '不通'} "
                   f"(丢包 {r['loss_pct']}%, 平均 {r['avg_ms']}ms)")
        return r["ok"]

    # -- 主流程 (v0.5.8 重写: 成功标准只看 wwan0 有没有 IP) --
    def _try_ip_type(self, ip_type):
        """尝试一种 IP 类型, 返回 True/False. 全程 log.
        IPv4: 先无 APN 拨号 (IMSI 识别在后台线程并行),
              DHCP 25 秒拿不到 IP 才用识别出的 APN 重拨.
        bearer 建立失败 (如 PolicyMismatch) 不判死刑,
        DHCP 拿到 IP 才是硬标准 (2026-10-11 N1 实测结论)."""
        import threading
        self._olog(f"===== 尝试 IPv{ip_type} =====")
        self._clean_wwan_routes()
        self._step_reset()  # 尽力而为, 失败不致命
        # 注: 不再调 _step_data_format (模块侧 raw-ip 是持久化设置,
        # 重复设置报 InvalidOperation(70), 手工流程也从未需要它)
        if not self._step_driver_raw_ip():
            return False
        if ip_type == 6:
            self._step_enable_ipv6()  # 失败不致命, 继续
            return self._dial_with_apn(ip_type, self.apn)
        # IPv4: 后台线程并行做 IMSI 识别 (不阻塞拨号)
        self._detected_apn = ""
        self._detected_carrier = ""
        t = threading.Thread(target=self._detect_apn_async, daemon=True)
        t.start()
        # 第一次: 不带 APN 拨号
        self._olog("--- 第一次: 不带 APN 拨号 ---")
        if self._dial_with_apn(ip_type, None):
            t.join(timeout=5)
            if self._detected_apn:
                self._olog(f"  备用 APN 已就绪: {self._detected_apn} (本次未用上)")
            return True
        # 第二次: 等识别完成, 用 APN 重拨 (只用检测值, 不用 stale 的 self.apn)
        self._olog("--- 无 APN 未拿到 IP, 等待 IMSI 识别 ---")
        t.join(timeout=10)
        apn = self._detected_apn
        if not apn:
            self._olog("  -> IMSI 未识别出 APN, 拨号失败")
            return False
        self._olog(f"--- 第二次: 用 APN={apn} 重拨 ---")
        # 更新配置的 APN (下次直接用, 与 v0.5.7 自动写回逻辑一致)
        if apn != self.apn:
            self.apn = apn
        return self._dial_with_apn(ip_type, apn)

    def _dial_with_apn(self, ip_type, apn):
        """单次拨号: bearer (尽力) + 地址获取. 返回 True/False."""
        # 建 bearer: 尽力而为. 有 handle 存下来用于干净断开,
        # 无 handle (PolicyMismatch) 也继续, DHCP 靠已有 bearer 照样通.
        handle, cid = self._step_start_network(ip_type, apn)
        if handle:
            self._handle, self._cid = handle, cid
            self._save_handle(handle, cid)
        else:
            self._olog("  -> 无 handle, 继续 DHCP")
            self._handle, self._cid = None, cid
        time.sleep(2)
        if ip_type == 4:
            # v0.5.8: IPv4 走 DHCP (udhcpc + 自定义禁路由脚本),
            # 不再解析 qmicli 输出
            if not self._step_dhcp():
                return False
        else:
            info = self._step_get_settings()
            if not self._step_config_iface(info, ip_type):
                return False
        # v0.5.7: 拿到 IP 即成功, ping 只记录不决定成败
        # (用户: 拨号后 wwan0 有什么 IP 就显示什么, ping 是"测试连通性"的事)
        ping_ok = self._step_verify(ip_type)
        if not ping_ok:
            self._olog(f"IPv{ip_type} 已配置但 ping 不通, 保留连接由用户手动测试")
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
        """断开 bearer (尽力而为, 失败不致命).
        有 handle 用 stop-network, 无 handle 用 wds-reset 兜底."""
        if self._handle:
            cmd = ["qmicli", "-d", self.qmi_dev,
                   f"--wds-stop-network={self._handle}"]
            if self._cid:
                cmd.append(f"--client-cid={self._cid}")
            _qmi_run(cmd, timeout=30)
        else:
            self._olog("  无 handle, 用 wds-reset 兜底")
            _qmi_run(["qmicli", "-d", self.qmi_dev, "--wds-reset"],
                     timeout=30)
        self._clear_handle()

    def stop(self):
        """关闭上网: 停 udhcpc, 清 IP (立即显示断开), 再慢慢断 bearer."""
        self._olog("关闭 QMI 连接")
        self._stop_monitor()
        # v0.5.8: 先停 udhcpc, 防止续租把 IP 加回来
        self._kill_udhcpc()
        # v0.5.7: 先清 IP/down 接口, 让状态立即显示"未连接"
        # qmicli 可能卡死, 放后面慢慢处理
        self._olog(f"  $ ip addr flush dev {self.iface}")
        _run(["ip", "addr", "flush", "dev", self.iface], timeout=10)
        self._olog(f"  $ ip link set {self.iface} down")
        _run(["ip", "link", "set", self.iface, "down"], timeout=10)
        self._olog("  网卡 IP 已清, 状态更新为未连接")
        # 再断 bearer (可能慢, 不影响状态显示)
        self._stop_bearer()
        self._olog("已关闭")
        return True

    def is_connected(self):
        """v0.5.8 重写: 只看 wwan0 有没有 IP, 不查 QMI bearer 状态.
        (QMI bearer 状态不可靠: 卡死的 bearer 会误报 connected,
        导致 Web 显示"已连接"但无 IP. 2026-10-11 教训.)"""
        return bool(self.get_ipv4() or self.get_ipv6())

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
        # v0.5.8: connected 只看网卡有没有 IP (bearer 状态不可靠).
        # 有 IP 即"已连接", 无 IP 即"未连接", 拨号后有什么显示什么.
        ipv4 = self.get_ipv4()
        ipv6 = self.get_ipv6()
        connected = bool(ipv4 or ipv6)
        return {
            "mode": "qmi",
            "mode_name": "QMI",
            "connected": connected,
            "bearer": connected,
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
          3. internet: 按实际 IP 类型 ping 测试 (只测有的, 快速)
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
        # 2. IP 先行 (v0.5.6: 不依赖 bearer, 永远读网卡真实地址)
        # v0.5.7: 先读 IP, 有 IP 就不查 bearer (qmicli 可能卡死)
        v4 = self.get_ipv4()
        v6 = self.get_ipv6()
        result["ipv4"] = v4
        result["ipv6"] = v6
        result["has_ipv4"] = bool(v4)
        result["has_ipv6"] = bool(v6)
        # 1. bearer (有 IP 时跳过, 避免 qmicli 卡死拖慢测试)
        if result["has_ipv4"] or result["has_ipv6"]:
            result["bearer"] = True
            result["bearer_detail"] = "有 IP, 跳过 bearer 检查"
        else:
            result["bearer"] = self.is_connected()
            if self._handle:
                result["bearer_detail"] = f"handle={self._handle}"
        # 3. 互联网: 只测实际有的 IP 类型, 2 包快速测试 (v0.5.7 优化速度)
        if result["has_ipv4"]:
            r = self.ping_test("223.5.5.5", count=2, timeout=5)
            result["internet_v4"] = r["ok"]
            result["v4_detail"] = r
        if result["has_ipv6"]:
            # v0.5.7: 如果已有 IPv4 且通, IPv6 测试可选 (省时间)
            # 为保持兼容仍测, 但用更短超时
            r = self.ping_test("2400:3200::1", count=2, timeout=5)
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
        apn=d.get("apn", ""),
        modem_factory=modem_factory)
