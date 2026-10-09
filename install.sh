#!/bin/bash
# SCC-lite quick install (systemd version)
# 支持: Debian/Ubuntu, Fedora/RHEL/CentOS, Arch, openSUSE
# Usage: sudo bash install.sh
# Installs to /opt/scc-lite-for-EC20-4g-module, enables systemd services.
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="/opt/scc-lite-for-EC20-4g-module"

# ------------------------------------------------------------------
# 包管理器检测 + 包名映射 (各发行版包名不同, 集中在此处便于调整)
# ------------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    PM="apt-get"
    PM_UPDATE="apt-get update"
    PM_INSTALL="apt-get install -y"
    PKG_PY_SERIAL="python3-serial"
    PKG_PY_FLASK="python3-flask"
    PKG_PY_YAML="python3-yaml"
    PKG_PY_WS="python3-websocket"      # 若仓库无此包, 自动转 pip
    PKG_QMI="libqmi-utils"             # qmicli
    PKG_IPROUTE="iproute2"             # ip 命令
    PKG_PING="iputils-ping"            # ping 命令
    PKG_SAMBA="samba"                  # smbd/nmbd, smbpasswd
    PKG_EXFAT="exfatprogs"             # exfat 工具
    PKG_NTFS="ntfs-3g"                # ntfs 读写
elif command -v dnf >/dev/null 2>&1; then
    PM="dnf"
    PM_UPDATE="dnf check-update || true"
    PM_INSTALL="dnf install -y"
    PKG_PY_SERIAL="python3-pyserial"
    PKG_PY_FLASK="python3-flask"
    PKG_PY_YAML="python3-pyyaml"
    PKG_PY_WS="python3-websocket-client"
    PKG_QMI="libqmi"
    PKG_IPROUTE="iproute"
    PKG_PING="iputils"
    PKG_SAMBA="samba"
    PKG_EXFAT="exfatprogs"
    PKG_NTFS="ntfs-3g"
elif command -v yum >/dev/null 2>&1; then
    PM="yum"
    PM_UPDATE="yum check-update || true"
    PM_INSTALL="yum install -y"
    PKG_PY_SERIAL="python3-pyserial"
    PKG_PY_FLASK="python3-flask"
    PKG_PY_YAML="python3-pyyaml"
    PKG_PY_WS="python3-websocket-client"
    PKG_QMI="libqmi"
    PKG_IPROUTE="iproute"
    PKG_PING="iputils"
    PKG_SAMBA="samba"
    PKG_EXFAT="exfatprogs"
    PKG_NTFS="ntfs-3g"
elif command -v pacman >/dev/null 2>&1; then
    PM="pacman"
    PM_UPDATE="pacman -Sy"
    PM_INSTALL="pacman -S --noconfirm"
    PKG_PY_SERIAL="python-pyserial"
    PKG_PY_FLASK="python-flask"
    PKG_PY_YAML="python-yaml"
    PKG_PY_WS="python-websocket-client"
    PKG_QMI="libqmi"
    PKG_IPROUTE="iproute2"
    PKG_PING="iputils"
    PKG_SAMBA="samba"
    PKG_EXFAT="exfatprogs"
    PKG_NTFS="ntfs-3g"
elif command -v zypper >/dev/null 2>&1; then
    PM="zypper"
    PM_UPDATE="zypper refresh"
    PM_INSTALL="zypper install -y"
    PKG_PY_SERIAL="python3-pyserial"
    PKG_PY_FLASK="python3-Flask"
    PKG_PY_YAML="python3-PyYAML"
    PKG_PY_WS="python3-websocket-client"
    PKG_QMI="libqmi"
    PKG_IPROUTE="iproute2"
    PKG_PING="iputils"
    PKG_SAMBA="samba"
    PKG_EXFAT="exfatprogs"
    PKG_NTFS="ntfs-3g"
else
    echo "!! 未识别的包管理器, 请手动安装依赖后重试"
    echo "   需要: python3, pyserial, flask, pyyaml, websocket-client,"
    echo "         qmicli(libqmi), iproute2, ping, samba, exfatprogs, ntfs-3g"
    exit 1
fi

echo "==> 包管理器: $PM"

# ------------------------------------------------------------------
# 拷文件
# ------------------------------------------------------------------
echo "==> Installing SCC-lite to $DST"
mkdir -p "$DST/data"
cp "$SRC/modem.py" "$SRC/notifications.py" "$SRC/data_control.py" \
   "$SRC/ec20_data.py" "$SRC/at_cheatsheet.py" "$SRC/qq_receiver.py" \
   "$SRC/scc-lite.py" "$SRC/scc-web.py" "$SRC/apn_db.py" \
   "$SRC/apns-conf.xml" "$SRC/requirements.txt" "$DST/"
[ -f "$DST/config.yaml" ] || cp "$SRC/config.yaml.example" "$DST/config.yaml"
chmod +x "$DST/scc-lite.py" "$DST/scc-web.py" "$DST/qq_receiver.py"

# ------------------------------------------------------------------
# Python 依赖 (优先系统包, 失败转 pip)
# ------------------------------------------------------------------
echo "==> Python dependencies (prefer $PM, fallback pip)"
if $PM_INSTALL $PKG_PY_SERIAL $PKG_PY_FLASK $PKG_PY_YAML $PKG_PY_WS 2>/dev/null; then
  echo "    installed via $PM"
else
  echo "    $PM failed, trying pip..."
  pip3 install --break-system-packages -r "$DST/requirements.txt"
fi

# ------------------------------------------------------------------
# 系统工具: qmicli (蜂窝数据) / iproute2 / ping
# ------------------------------------------------------------------
if ! command -v qmicli >/dev/null 2>&1; then
  echo "==> Installing $PKG_QMI (qmicli)"
  $PM_UPDATE && $PM_INSTALL $PKG_QMI
fi
for _cmd in ip ping; do
  if ! command -v $_cmd >/dev/null 2>&1; then
    echo "==> Installing ${_cmd} related package"
    case $_cmd in
      ip) $PM_INSTALL $PKG_IPROUTE ;;
      ping) $PM_INSTALL $PKG_PING ;;
    esac
  fi
done

# ------------------------------------------------------------------
# Samba + 文件系统工具 (USB 挂载/Samba 共享, v0.5.6+)
# vfat(FAT32) 内核自带无需安装; exfat/ntfs 需要用户态工具
# ------------------------------------------------------------------
echo "==> Installing Samba and filesystem tools"
$PM_INSTALL $PKG_SAMBA $PKG_EXFAT $PKG_NTFS || {
  echo "!! 部分包安装失败, USB/Samba 功能可能受限, 可手动安装后重试"
}

# ------------------------------------------------------------------
# systemd 服务
# ------------------------------------------------------------------
echo "==> systemd services"
cp "$SRC/scc-lite.service" "$SRC/scc-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scc-lite.service scc-web.service

echo "==> Done. Web UI: http://<host>:7577 (admin/admin - CHANGE IT)"
echo "    Edit $DST/config.yaml then: systemctl restart scc-lite scc-web"
