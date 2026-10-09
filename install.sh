#!/bin/bash
# SCC-lite quick install (systemd version)
# Usage: sudo bash install.sh
# Installs to /opt/scc-lite-for-EC20-4g-module, enables systemd services.
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="/opt/scc-lite-for-EC20-4g-module"

echo "==> Installing SCC-lite to $DST"
mkdir -p "$DST/data"
cp "$SRC/modem.py" "$SRC/notifications.py" "$SRC/data_control.py" \
   "$SRC/ec20_data.py" "$SRC/at_cheatsheet.py" "$SRC/qq_receiver.py" \
   "$SRC/scc-lite.py" "$SRC/scc-web.py" "$SRC/apn_db.py" \
   "$SRC/apns-conf.xml" "$SRC/requirements.txt" "$DST/"
[ -f "$DST/config.yaml" ] || cp "$SRC/config.yaml.example" "$DST/config.yaml"
chmod +x "$DST/scc-lite.py" "$DST/scc-web.py" "$DST/qq_receiver.py"

echo "==> Python dependencies (prefer apt, fallback pip)"
if apt-get install -y python3-serial python3-flask python3-yaml python3-websocket 2>/dev/null; then
  echo "    installed via apt"
else
  echo "    apt failed, trying pip..."
  pip3 install --break-system-packages -r "$DST/requirements.txt"
fi

# libqmi for data control (Debian/Ubuntu). Skip if already present.
if ! command -v qmicli >/dev/null 2>&1; then
  echo "==> Installing libqmi-utils"
  apt-get update && apt-get install -y libqmi-utils iproute2 iputils-ping
fi

echo "==> systemd services"
cp "$SRC/scc-lite.service" "$SRC/scc-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scc-lite.service scc-web.service

echo "==> Done. Web UI: http://<host>:7577 (admin/admin - CHANGE IT)"
echo "    Edit $DST/config.yaml then: systemctl restart scc-lite scc-web"
