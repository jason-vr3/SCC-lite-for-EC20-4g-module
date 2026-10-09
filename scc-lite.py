#!/usr/bin/env python3
"""
SCC-lite (SMS Control Centre) - 短信守护进程.

短信控制中心 - 后台守护进程.

功能:
  - 轮询 4G 模块收取新短信 (AT+CMGL)
  - 存入 SQLite 数据库
  - 通过转发队列推送到启用的通知渠道 (QQ/Telegram/Webhook/Bark),
    支持失败重试与去重, 保证 at-least-once 投递.

Polls modem for incoming SMS via AT commands, stores in SQLite,
forwards to enabled notification channels via a persistent queue
with retry and dedup (at-least-once delivery).

用法 Usage:
    python3 scc-lite.py [--config /opt/scc-lite-for-EC20-4g-module/config.yaml]

作为 systemd 服务运行 (见 scc-lite.service).
Runs as systemd service (see scc-lite.service).
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
import yaml

VERSION = "0.5.5"  # 2026-10-08: ECM 数据 + APN 自动识别 + IPv6

from modem import Modem, ModemError, decode_ucs2, is_ucs2_hex
from notifications import Notifier

try:
    from qq_receiver import QQReceiver
    HAS_QQ_WS = True
except ImportError:
    HAS_QQ_WS = False
    QQReceiver = None

log = logging.getLogger("scc-lite")


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS sms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    modem_index INTEGER,          -- index on modem (for delete tracking)
    direction   TEXT NOT NULL,    -- 'in' or 'out'
    sender      TEXT,             -- sender number (in) or recipient (out)
    body        TEXT,
    sms_time    TEXT,             -- modem timestamp
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    forwarded   INTEGER DEFAULT 0 -- 1 if pushed to notifications
);
CREATE INDEX IF NOT EXISTS idx_sms_created ON sms(created_at);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
-- Forward queue: at-least-once delivery to notification channels.
-- One row per (sms, channel). UNIQUE prevents duplicate queueing.
CREATE TABLE IF NOT EXISTS forward_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sms_id     INTEGER NOT NULL,   -- FK to sms.id
    channel    TEXT NOT NULL,      -- qq / telegram / webhook / bark
    status     TEXT DEFAULT 'pending',  -- pending / sent / failed
    attempts   INTEGER DEFAULT 0,
    next_retry TIMESTAMP,          -- when to retry next
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sms_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_fwd_status ON forward_queue(status, next_retry);
"""


class Store:
    """SQLite 存储: 短信记录 + 转发队列 + 键值配置.

    SQLite storage for SMS records, forward queue, and key-value config.
    表结构:
      sms            - 短信收发记录
      forward_queue  - 转发队列 (at-least-once 投递, 去重+重试)
      kv             - 键值配置
    """

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def save_incoming(self, modem_index, sender, body, sms_time):
        # Avoid duplicates: check if same sender/time/body exists
        cur = self.db.execute(
            "SELECT id FROM sms WHERE direction='in' AND sender=? AND sms_time=? AND body=?",
            (sender, sms_time, body))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO sms (modem_index, direction, sender, body, sms_time)"
            " VALUES (?,?,?,?,?)",
            (modem_index, "in", sender, body, sms_time))
        self.db.commit()
        return cur.lastrowid

    def save_outgoing(self, recipient, body):
        cur = self.db.execute(
            "INSERT INTO sms (direction, sender, body, forwarded)"
            " VALUES ('out',?,?,1)",
            (recipient, body))
        self.db.commit()
        return cur.lastrowid

    def mark_forwarded(self, row_id):
        self.db.execute("UPDATE sms SET forwarded=1 WHERE id=?", (row_id,))
        self.db.commit()

    def get_recent(self, limit=100):
        cur = self.db.execute(
            "SELECT * FROM sms ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_kv(self, k, default=""):
        cur = self.db.execute("SELECT v FROM kv WHERE k=?", (k,))
        r = cur.fetchone()
        return r["v"] if r else default

    def set_kv(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)",
                        (k, v))
        self.db.commit()

    # ---- 转发队列 (at-least-once 投递) ----
    # ---- Forward queue (at-least-once delivery) ----
    def queue_forward(self, sms_id, channels):
        """入队: 每个渠道一条记录. INSERT OR IGNORE 天然去重.
        Enqueue one row per channel. INSERT OR IGNORE = dedup."""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for ch in channels:
            self.db.execute(
                "INSERT OR IGNORE INTO forward_queue"
                " (sms_id, channel, status, next_retry)"
                " VALUES (?,?,'pending',?)",
                (sms_id, ch, now))
        self.db.commit()

    def get_pending_forwards(self, limit=50):
        """Rows ready to (re)try: pending, or failed with next_retry due."""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cur = self.db.execute(
            """SELECT q.*, s.sender, s.body, s.sms_time
               FROM forward_queue q JOIN sms s ON s.id=q.sms_id
               WHERE q.status IN ('pending','failed')
                 AND q.next_retry <= ?
               ORDER BY q.id LIMIT ?""",
            (now, limit))
        return [dict(r) for r in cur.fetchall()]

    def mark_forward_sent(self, qid):
        self.db.execute(
            "UPDATE forward_queue SET status='sent' WHERE id=?", (qid,))
        self.db.commit()

    def mark_forward_failed(self, qid, attempts):
        """Exponential backoff: 1m, 5m, 15m. After 3 fails -> 'failed' (dead)."""
        backoff = [60, 300, 900]
        if attempts >= 3:
            self.db.execute(
                "UPDATE forward_queue SET status='failed', attempts=?"
                " WHERE id=?", (attempts, qid))
        else:
            nxt = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(time.time() + backoff[attempts]))
            self.db.execute(
                "UPDATE forward_queue SET attempts=?, next_retry=?"
                " WHERE id=?", (attempts, nxt, qid))
        self.db.commit()

    def count_pending_forwards(self):
        cur = self.db.execute(
            "SELECT COUNT(*) c FROM forward_queue"
            " WHERE status IN ('pending','failed')")
        return cur.fetchone()["c"]


# ----------------------------------------------------------------------
# Daemon
# ----------------------------------------------------------------------
class Daemon:
    def __init__(self, config):
        self.cfg = config
        modem_cfg = config.get("modem", {})
        self.modem = Modem(
            port=modem_cfg.get("port", "/dev/ttyUSB3"),
            baudrate=modem_cfg.get("baudrate", 115200),
            timeout=modem_cfg.get("timeout", 10),
        )
        sms_cfg = config.get("sms", {})
        self.poll_interval = sms_cfg.get("poll_interval", 15)
        self.delete_after_forward = sms_cfg.get("delete_after_forward", True)

        data_path = config.get("data_dir", "/opt/scc-lite-for-EC20-4g-module/data")
        os.makedirs(data_path, exist_ok=True)
        self.store = Store(os.path.join(data_path, "scc-lite.db"))
        self.notifier = Notifier(config.get("notifications", {}))

        # Track seen modem indexes to avoid re-processing
        self._seen = set()

    def reload_notifications(self, notif_cfg):
        self.notifier.reload(notif_cfg)

    def _decode_body(self, raw):
        """Decode SMS body: UCS2 hex -> text if applicable."""
        raw = (raw or "").strip()
        if is_ucs2_hex(raw):
            return decode_ucs2(raw)
        return raw

    def poll_once(self):
        """One SMS poll cycle. Returns number of new messages."""
        # Open modem (acquires lock), poll, then close (releases lock)
        # so web UI can use the modem between polls.
        try:
            self.modem.open()
        except ModemError as e:
            log.warning("poll: cannot open modem: %s", e)
            return 0
        try:
            try:
                msgs = self.modem.sms_list("all")
                log.info("poll: modem returned %d messages", len(msgs))
            except ModemError as e:
                log.warning("sms_list failed: %s", e)
                return 0

            new_count = 0
            for m in msgs:
                idx = m["index"]
                # Only process unread incoming; skip already-seen
                # (modem returns uppercase stat like "REC READ")
                if m["stat"].upper() not in ("REC UNREAD", "REC READ"):
                    continue
                key = (m["sender"], m["time"], m["body"][:32])
                if key in self._seen:
                    continue
                self._seen.add(key)

                body = self._decode_body(m["body"])
                row_id = self.store.save_incoming(
                    idx, m["sender"], body, m["time"])
                log.info("new SMS from %s: %s", m["sender"], body[:60])
                new_count += 1

                # Queue for at-least-once forwarding (dedup via UNIQUE)
                channels = [n for n in ("qq", "telegram", "webhook", "bark")
                            if self.notifier._get_channel(n) is not None]
                if channels:
                    self.store.queue_forward(row_id, channels)
                    log.info("queued forward for sms %d via %s",
                             row_id, channels)

                # Delete from modem to prevent SIM filling up
                if self.delete_after_forward:
                    try:
                        self.modem.sms_delete(idx)
                    except ModemError as e:
                        log.warning("delete idx %d failed: %s", idx, e)

            # Bound memory: keep last 5000 keys
            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
            return new_count
        finally:
            # Always release the modem lock
            try:
                self.modem.close()
            except:
                pass

    def process_forward_queue(self):
        """Background worker: (re)try pending forwards with backoff."""
        try:
            rows = self.store.get_pending_forwards()
        except Exception as e:
            log.warning("forward queue read failed: %s", e)
            return
        for r in rows:
            ok = self.notifier.send_to_channel(
                r["channel"], r["sender"], r["body"], r["sms_time"] or "")
            if ok:
                self.store.mark_forward_sent(r["id"])
                # Mark sms forwarded if at least one channel succeeded
                self.store.mark_forwarded(r["sms_id"])
                log.info("forwarded sms %d via %s", r["sms_id"], r["channel"])
            else:
                attempts = (r["attempts"] or 0) + 1
                self.store.mark_forward_failed(r["id"], attempts)
                log.warning("forward sms %d via %s failed (attempt %d)",
                            r["sms_id"], r["channel"], attempts)

    def run(self):
        log.info("SCC-lite daemon starting, port=%s interval=%ds",
                 self.modem.port, self.poll_interval)
        # Prime _seen with existing messages; also save to DB so UI can see them
        # (mark as forwarded to avoid re-notifying on every restart)
        # Open/close modem for prime (releases lock after)
        try:
            self.modem.open()
            log.info("modem opened: %s", self.modem.port)
            try:
                imei = self.modem.get_imei()
                log.info("modem IMEI: %s", imei)
            except ModemError as e:
                log.warning("get_imei failed: %s", e)
            # Enable instant new-SMS URC (+CMTI) per open-source practice
            # (gammu, macsatcom). Polling remains as fallback.
            try:
                self.modem.set_cnmi(2, 1, 0, 0, 0)
                log.info("CNMI set to 2,1,0,0,0 (+CMTI URC enabled)")
            except ModemError as e:
                log.warning("set_cnmi failed: %s", e)
            for m in self.modem.sms_list("all"):
                key = (m["sender"], m["time"], m["body"][:32])
                self._seen.add(key)
                body = self._decode_body(m["body"])
                # save_incoming returns existing id if duplicate
                row_id = self.store.save_incoming(
                    m["index"], m["sender"], body, m["time"])
                # Mark as forwarded so restart doesn't re-notify
                self.store.mark_forwarded(row_id)
            log.info("primed %d existing messages", len(self._seen))
        except ModemError as e:
            log.warning("prime failed: %s", e)
        finally:
            try:
                self.modem.close()
            except:
                pass

        # Start QQ WebSocket receiver if configured (captures openids)
        qq_cfg = self.cfg.get("notifications", {}).get("qq", {})
        if HAS_QQ_WS and qq_cfg.get("enabled") and qq_cfg.get("app_id") and qq_cfg.get("app_secret"):
            try:
                from notifications import QQBot
                import qq_receiver
                # openid 存到 data_dir，和 SQLite 放一起
                qq_receiver.set_openid_file(
                    os.path.join(data_path, "qq_openids.json"))
                bot = QQBot(qq_cfg.get("app_id"), qq_cfg.get("app_secret"), "")
                receiver = QQReceiver(qq_cfg.get("app_id"), qq_cfg.get("app_secret"),
                                      bot._get_token)
                receiver.start()
                log.info("QQ WebSocket receiver started")
            except Exception as e:
                log.warning("QQ receiver failed to start: %s", e)
        elif qq_cfg.get("enabled"):
            log.info("QQ receiver not started (need websocket-client: pip install websocket-client)")

        # Startup recovery: process any pending forwards from before restart
        pending = self.store.count_pending_forwards()
        if pending:
            log.info("startup: %d pending forwards to retry", pending)
            self.process_forward_queue()

        while True:
            try:
                # Hot-reload notification config if web UI changed it
                try:
                    mtime = os.path.getmtime(self.config_path)
                    if mtime != self.config_mtime:
                        self.config_mtime = mtime
                        new_cfg = load_config(self.config_path)
                        self.notifier.reload(new_cfg.get("notifications", {}))
                        log.info("notification config reloaded")
                except Exception as e:
                    log.debug("config reload check failed: %s", e)
                n = self.poll_once()
                if n:
                    log.info("poll: %d new", n)
                # Process forward queue (retries) each cycle
                self.process_forward_queue()
            except Exception as e:
                log.warning("poll cycle error: %s", e)
            time.sleep(self.poll_interval)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
DEFAULT_CONFIG = {
    "modem": {"port": "/dev/ttyUSB3", "baudrate": 115200, "timeout": 10},
    "sms": {"poll_interval": 15, "delete_after_forward": True},
    "data_dir": "/opt/scc-lite-for-EC20-4g-module/data",
    "web": {"port": 7577, "username": "admin", "password": "admin"},
    "data": {"qmi_dev": "/dev/cdc-wdm0", "iface": "wwan0", "apn": ""},
    "notifications": {
        "qq": {"enabled": False, "app_id": "", "app_secret": "", "openid": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "webhook": {"enabled": False, "url": "", "headers": {}, "secret": ""},
        "bark": {"enabled": False, "key": "", "server": "https://api.day.app"},
    },
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        # Deep-merge one level
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    return cfg


def main():
    ap = argparse.ArgumentParser(description="SCC-lite SMS daemon")
    ap.add_argument("--config", default="/opt/scc-lite-for-EC20-4g-module/config.yaml")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = load_config(args.config)
    d = Daemon(config)
    d.config_path = args.config
    d.config_mtime = os.path.getmtime(args.config) if os.path.exists(args.config) else 0
    d.run()


if __name__ == "__main__":
    main()
