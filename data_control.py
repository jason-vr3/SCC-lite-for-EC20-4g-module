#!/usr/bin/env python3
"""
SCC-lite - Cellular data (QMI) control.

Manages QMI data connection via libqmi-utils (qmicli).
Tested on: Quectel EC20, /dev/cdc-wdm0, wwan0 interface.

QMI commands used (from libqmi-utils documentation and Quectel guides):
  - qmicli -d /dev/cdc-wdm0 --wds-start-network=APN
      Start data connection. Returns handle + IP details.
  - qmicli -d /dev/cdc-wdm0 --wds-stop-network=<handle>
      Stop data connection.
  - qmicli -d /dev/cdc-wdm0 --wds-get-packet-service-status
      Check if data bearer is connected.

Network interface:
  - wwan0 must be in raw-ip mode for Quectel modules:
      ip link set wwan0 down
      echo Y > /sys/class/net/wwan0/qmi/raw_ip  (or use qmicli --set-expected-data-format)
      ip link set wwan0 up
  - IP assigned via qmicli output or DHCP (udhcpc/dhclient on wwan0).

Reference: Quectel Linux USB driver guide, libqmi documentation.
"""

import subprocess
import re
import logging
import time

log = logging.getLogger("scc-lite.data")


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


class DataControl:
    """
    QMI data connection manager.

    Usage:
        dc = DataControl(qmi_dev="/dev/cdc-wdm0", iface="wwan0", apn="3gnet")
        dc.start()          # connect
        print(dc.get_ip())  # "10.x.x.x"
        print(dc.ping_test("114.114.114.114", count=3))  # {"avg_ms": ...}
        dc.stop()           # disconnect
    """

    def __init__(self, qmi_dev="/dev/cdc-wdm0", iface="wwan0", apn="",
                 auth=None):
        self.qmi_dev = qmi_dev
        self.iface = iface
        self.apn = apn
        self.auth = auth  # e.g. "PAP,username,password" or None
        self._handle = None  # WDS packet data handle from start-network

    # ------------------------------------------------------------------
    # Connection control
    # ------------------------------------------------------------------
    def start(self):
        """
        Start QMI data connection.

        Steps (verified on EC20 + N1):
          1. Ensure wwan0 is raw-ip mode.
          2. qmicli --wds-start-network with APN.
          3. Parse handle from output for later stop.
          4. Bring up wwan0 and get IP (via qmicli IP details or dhclient).

        Returns True on success.
        """
        # Step 1: raw-ip mode (Quectel requirement)
        self._ensure_raw_ip()

        # Step 2: start network
        cmd = ["qmicli", "-d", self.qmi_dev,
               f"--wds-start-network={self.apn}" if self.apn
               else "--wds-start-network"]
        if self.auth:
            cmd.append(f"--wds-auth={self.auth}")
        # Use verbose to get IP details
        cmd.append("--client-no-release-cid")

        rc, out, err = _run(cmd, timeout=60)
        if rc != 0:
            log.error("wds-start-network failed: %s %s", out, err)
            return False

        # Parse handle: output contains "Packet data handle: '1234567890'"
        m = re.search(r"Packet data handle:\s*'(\d+)'", out)
        if m:
            self._handle = m.group(1)
            log.info("data connected, handle=%s", self._handle)
        else:
            log.warning("connected but no handle parsed")

        # Step 3: bring up interface and get IP
        time.sleep(2)
        self._iface_up()
        return True

    def stop(self):
        """
        Stop QMI data connection.
        Uses stored handle from start(). If no handle, tries generic stop.
        """
        if self._handle:
            cmd = ["qmicli", "-d", self.qmi_dev,
                   f"--wds-stop-network={self._handle}"]
            # Release the client CID we held
            cmd.append("--client-cid-release")
            rc, out, err = _run(cmd, timeout=30)
            self._handle = None
            if rc != 0:
                log.warning("wds-stop-network failed: %s %s", out, err)
                return False
            log.info("data disconnected")
            return True
        log.warning("no active handle to stop")
        return False

    def is_connected(self):
        """
        Check packet service status via QMI.
        Returns True if bearer is connected.
        """
        cmd = ["qmicli", "-d", self.qmi_dev,
               "--wds-get-packet-service-status"]
        rc, out, err = _run(cmd, timeout=15)
        if rc != 0:
            return False
        # Output contains "Connection status: 'connected'"
        return "'connected'" in out

    # ------------------------------------------------------------------
    # IP and interface
    # ------------------------------------------------------------------
    def _ensure_raw_ip(self):
        """Set wwan0 to raw-ip mode (required for Quectel)."""
        # Method 1: sysfs (if driver exposes it)
        import os
        raw_ip_path = f"/sys/class/net/{self.iface}/qmi/raw_ip"
        try:
            with open(raw_ip_path, "w") as f:
                f.write("Y\n")
            log.debug("set raw_ip via sysfs")
            return
        except (FileNotFoundError, PermissionError, OSError):
            pass
        # Method 2: qmicli (newer libqmi)
        cmd = ["qmicli", "-d", self.qmi_dev,
               "--set-expected-data-format=raw-ip"]
        _run(cmd, timeout=15)

    def _iface_up(self):
        """Bring wwan0 up and try to get IP via DHCP."""
        _run(["ip", "link", "set", self.iface, "up"], timeout=10)
        time.sleep(1)
        # Try DHCP (udhcpc is common on embedded, dhclient on desktop)
        for dhcp in (["udhcpc", "-i", self.iface, "-q", "-n"],
                     ["dhclient", self.iface]):
            rc, _, _ = _run(dhcp, timeout=20)
            if rc == 0:
                break

    def get_ip(self):
        """
        Get IPv4 address of wwan0 interface.
        Returns str IP or "" if none.
        """
        rc, out, _ = _run(["ip", "-4", "addr", "show", self.iface],
                          timeout=10)
        if rc != 0:
            return ""
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else ""

    def get_status(self):
        """
        Combined status dict for web UI:
        {"connected": bool, "ip": str, "iface": str}
        """
        connected = self.is_connected()
        ip = self.get_ip() if connected else ""
        return {"connected": connected, "ip": ip, "iface": self.iface}

    # ------------------------------------------------------------------
    # Ping test
    # ------------------------------------------------------------------
    def ping_test(self, target, count=3, timeout=10):
        """
        Ping target N times, return {"ok": bool, "avg_ms": float,
        "loss_pct": float, "raw": str}.

        Uses system ping. Binds to wwan0 if connected (so test goes
        via cellular, not LAN).

        User requirement: ping 3 times, show average, static display
        (no continuous ping to save data).
        """
        cmd = ["ping", "-c", str(count), "-W", "3"]
        # Bind to cellular interface if we have an IP
        ip = self.get_ip()
        if ip:
            cmd += ["-I", self.iface]
        cmd.append(target)

        rc, out, err = _run(cmd, timeout=timeout + 10)
        result = {"ok": rc == 0, "avg_ms": 0.0, "loss_pct": 100.0,
                  "raw": out or err}

        # Parse: "3 packets transmitted, 3 received, 0% packet loss"
        m = re.search(r"(\d+)% packet loss", out)
        if m:
            result["loss_pct"] = float(m.group(1))
        # Parse: "rtt min/avg/max/mdev = 23.1/25.4/28.9/2.1 ms"
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
        if m:
            result["avg_ms"] = float(m.group(1))
        else:
            # BusyBox ping format: "round-trip min/avg/max = 23.1/25.4/28.9 ms"
            m = re.search(r"round-trip min/avg/max = [\d.]+/([\d.]+)/", out)
            if m:
                result["avg_ms"] = float(m.group(1))

        return result
