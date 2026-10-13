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

VERSION = "0.6.0"  # v0.6.0: 多模块 + 适配层 + 自助扫描

from modem import Modem, ModemError, decode_ucs2, is_ucs2_hex
from notifications import Notifier

try:
    from modem_profiles import get_profile, detect
    HAS_PROFILES = True
except ImportError:
    HAS_PROFILES = False
    get_profile = None
    detect = None

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
    forwarded   INTEGER DEFAULT 0, -- 1 if pushed to notifications
    module_id   TEXT DEFAULT '',  -- v0.6.0: 所属模块 IMEI, 多模块隔离
    read        INTEGER DEFAULT 0 -- v0.6.0: 1=用户已读 (Web 标记)
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
-- v0.6.0: 模块注册表 (IMEI 主键, 端口可变自动重绑)
CREATE TABLE IF NOT EXISTS modules (
    imei      TEXT PRIMARY KEY,
    alias     TEXT DEFAULT '',
    port      TEXT DEFAULT '',
    profile   TEXT DEFAULT 'EC20Profile',
    model     TEXT DEFAULT '',   -- v0.6.0: ATI 识别到的型号
    enabled   INTEGER DEFAULT 1,
    added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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
        # v0.6.0 迁移: 老库加 module_id 列 / modules 表
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """老版本数据库迁移 (幂等)."""
        cols = [r["name"] for r in
                self.db.execute("PRAGMA table_info(sms)").fetchall()]
        if "module_id" not in cols:
            self.db.execute("ALTER TABLE sms ADD COLUMN module_id TEXT DEFAULT ''")
            log.info("migrated: sms.module_id added")
        if "read" not in cols:
            self.db.execute("ALTER TABLE sms ADD COLUMN read INTEGER DEFAULT 0")
            log.info("migrated: sms.read added")
        if "imsi" not in cols:
            self.db.execute("ALTER TABLE sms ADD COLUMN imsi TEXT DEFAULT ''")
            log.info("migrated: sms.imsi added")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_module ON sms(module_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_imsi ON sms(imsi)")
        # v0.6.1: IMSI->号码映射表 (用户自助维护, 多 IMSI 可同号/补卡)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS sim_numbers (
                imsi TEXT PRIMARY KEY,
                number TEXT DEFAULT '',
                alias TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # v0.6.1: 模块当前 IMSI (worker 每轮写, web 直接查"号码现在在哪个模块")
        mcols = [r[1] for r in self.db.execute(
            "PRAGMA table_info(modules)").fetchall()]
        if "current_imsi" not in mcols:
            self.db.execute(
                "ALTER TABLE modules ADD COLUMN current_imsi TEXT DEFAULT ''")
            log.info("migrated: modules.current_imsi added")

    def save_incoming(self, modem_index, sender, body, sms_time,
                      module_id="", imsi=""):
        # v0.6.1: 去重键加入 imsi (多卡同收一条群发不误杀)
        cur = self.db.execute(
            "SELECT id FROM sms WHERE direction='in' AND sender=? AND sms_time=? AND body=?"
            " AND COALESCE(imsi,'')=COALESCE(?, '')",
            (sender, sms_time, body, imsi))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO sms (modem_index, direction, sender, body, sms_time, module_id, imsi)"
            " VALUES (?,?,?,?,?,?,?)",
            (modem_index, "in", sender, body, sms_time, module_id, imsi))
        self.db.commit()
        return cur.lastrowid

    def save_outgoing(self, recipient, body, module_id="", imsi=""):
        cur = self.db.execute(
            "INSERT INTO sms (direction, sender, body, forwarded, module_id, imsi)"
            " VALUES ('out',?,?,1,?,?)",
            (recipient, body, module_id, imsi))
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

    # ---- v0.6.0: 模块注册表 (IMEI 主键) ----
    def upsert_module(self, imei, alias="", port="", profile="EC20Profile",
                      enabled=1, model=""):
        """新增或更新模块 (端口变化自动重绑)."""
        if not imei:
            return
        if not alias:
            alias = f"{profile.replace('Profile', '')}-{imei[-4:]}"
        cols = [r["name"] for r in
                self.db.execute("PRAGMA table_info(modules)").fetchall()]
        if "model" not in cols:
            self.db.execute("ALTER TABLE modules ADD COLUMN model TEXT DEFAULT ''")
        self.db.execute(
            "INSERT INTO modules (imei, alias, port, profile, model, enabled)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(imei) DO UPDATE SET"
            " alias=excluded.alias, port=excluded.port,"
            " profile=excluded.profile, model=excluded.model,"
            " enabled=excluded.enabled",
            (imei, alias, port, profile, model, enabled))
        self.db.commit()

    def get_modules(self, only_enabled=False):
        q = "SELECT * FROM modules"
        if only_enabled:
            q += " WHERE enabled=1"
        q += " ORDER BY added_at"
        return [dict(r) for r in self.db.execute(q).fetchall()]

    def set_module_enabled(self, imei, enabled):
        self.db.execute("UPDATE modules SET enabled=? WHERE imei=?",
                        (1 if enabled else 0, imei))
        self.db.commit()

    def delete_module(self, imei):
        """删除模块 (历史短信保留, 只停轮询)."""
        self.db.execute("DELETE FROM modules WHERE imei=?", (imei,))
        self.db.commit()

    def set_module_alias(self, imei, alias):
        self.db.execute("UPDATE modules SET alias=? WHERE imei=?",
                        (alias, imei))
        self.db.commit()

    def backfill_module_id(self):
        """单启用模块时, 把 module_id 为空的历史短信归属到该模块.

        v0.5.9 升上来的老短信 module_id 为空, 会被按模块过滤隐藏,
        看起来像"丢失". 幂等, 无空值时零开销.
        返回回填条数.
        """
        mods = self.get_modules(only_enabled=True)
        if len(mods) != 1 or not mods[0].get("imei"):
            return 0
        imei = mods[0]["imei"]
        cur = self.db.execute(
            "UPDATE sms SET module_id=? WHERE module_id='' OR module_id IS NULL",
            (imei,))
        n = cur.rowcount
        if n:
            log.info("backfilled %d sms to module %s", n, imei)
        self.db.commit()
        return n

    # ---- v0.6.1: IMSI 为纲 ----
    def get_sim_numbers(self):
        cur = self.db.execute(
            "SELECT imsi, number, alias, updated_at FROM sim_numbers ORDER BY number, imsi")
        return [dict(r) for r in cur.fetchall()]

    def ensure_sim_number(self, imsi):
        """新 IMSI 自动发现: 无则插入空号码待填. 幂等."""
        if not imsi:
            return None
        self.db.execute(
            "INSERT OR IGNORE INTO sim_numbers (imsi) VALUES (?)", (imsi,))
        self.db.commit()
        cur = self.db.execute(
            "SELECT imsi, number, alias FROM sim_numbers WHERE imsi=?", (imsi,))
        return dict(cur.fetchone())

    def upsert_sim_number(self, imsi, number="", alias=""):
        self.db.execute(
            """INSERT INTO sim_numbers (imsi, number, alias, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(imsi) DO UPDATE SET
                 number=excluded.number, alias=excluded.alias,
                 updated_at=CURRENT_TIMESTAMP""",
            (imsi, number.strip(), alias.strip()))
        self.db.commit()

    def delete_sim_number(self, imsi):
        self.db.execute("DELETE FROM sim_numbers WHERE imsi=?", (imsi,))
        self.db.commit()

    def set_module_current_imsi(self, imei, imsi):
        """worker 每轮写当前 IMSI, web 查"号码现在在哪个模块"."""
        self.db.execute("UPDATE modules SET current_imsi=? WHERE imei=?",
                        (imsi or "", imei))
        self.db.commit()

    def backfill_imsi(self):
        """老短信 imsi 回填 (fallback, 幂等).

        只有"唯一 IMSI"时才自动回填 (单卡用户零歧义).
        多 IMSI/换过卡时不猜, 由用户在页面手动归属.
        返回回填条数.
        """
        cur = self.db.execute(
            "SELECT COUNT(*) c FROM sms WHERE imsi='' OR imsi IS NULL")
        if not cur.fetchone()["c"]:
            return 0
        # 候选 IMSI: sim_numbers 去重后
        rows = self.db.execute(
            "SELECT DISTINCT imsi FROM sim_numbers WHERE imsi<>''").fetchall()
        imsis = [r["imsi"] for r in rows]
        if len(imsis) != 1:
            return 0
        cur = self.db.execute(
            "UPDATE sms SET imsi=? WHERE imsi='' OR imsi IS NULL", (imsis[0],))
        n = cur.rowcount
        if n:
            log.info("backfilled %d sms imsi to %s", n, imsis[0])
        self.db.commit()
        return n

    def imsis_for_number(self, number):
        """某号码名下所有 IMSI (补卡合并)."""
        rows = self.db.execute(
            "SELECT imsi FROM sim_numbers WHERE number=?", (number,)).fetchall()
        return [r["imsi"] for r in rows]


# ----------------------------------------------------------------------
# Daemon
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# v0.6.0: 多模块架构
# Daemon (supervisor) 管理多个 ModuleWorker, 每个模块独立线程轮询.
# 模块以 IMEI 为唯一标识, 端口变化自动重绑.
# ----------------------------------------------------------------------
class ModuleWorker:
    """单模块轮询 worker.

    每个模块一个实例, 跑在独立线程:
      - 独立 Modem / _seen / _concat_buf (长短信重组缓冲)
      - 短信存库时带 module_id=IMEI, 各模块收件箱隔离
    """

    def __init__(self, mod_cfg, store, notifier, data_path, sms_cfg):
        self.imei = mod_cfg.get("imei", "")
        self.alias = mod_cfg.get("alias") or self.imei[-4:]
        self.port = mod_cfg.get("port", "/dev/ttyUSB3")
        self.profile_name = mod_cfg.get("profile", "EC20Profile")
        self.modem = Modem(
            port=self.port,
            baudrate=mod_cfg.get("baudrate", 115200),
            timeout=mod_cfg.get("timeout", 10),
        )
        self.store = store
        self.notifier = notifier
        self.data_path = data_path
        self.poll_interval = sms_cfg.get("poll_interval", 15)
        self.delete_after_forward = sms_cfg.get("delete_after_forward", True)
        self._seen = set()
        self._concat_buf = {}
        self._stop = False
        self._profile = None
        # v0.6.1: IMSI 为纲. current_imsi 跟 SIM 卡走 (热插拔自动跟上).
        # _imsi_window: 最近 3 轮读取成败滑动窗口, >=2 失败则置空.
        from collections import deque
        self.current_imsi = ""
        self._imsi_window = deque(maxlen=3)

    def label(self):
        return f"{self.alias}({self.port})"

    def stop(self):
        self._stop = True

    def _load_profile(self):
        """按配置加载适配 profile (检测失败则 fallback EC20)."""
        if not HAS_PROFILES:
            return None
        try:
            cls = get_profile("")
            # 配置里存的是类名, 如 EC20Profile
            import modem_profiles
            cls = getattr(modem_profiles, self.profile_name,
                          modem_profiles.DEFAULT_PROFILE)
            self._profile = cls()
            return self._profile
        except Exception as e:
            log.warning("[%s] profile 加载失败: %s", self.label(), e)
            return None

    def _resolve_imei(self):
        """运行时读 IMEI: 补全空 IMEI / 校验端口是否换了模块."""
        try:
            imei = self.modem.get_imei()
        except ModemError:
            return
        if not imei:
            return
        if not self.imei:
            # 首次: 回填 IMEI
            self.imei = imei
            self.store.upsert_module(imei, alias=self.alias,
                                     port=self.port,
                                     profile=self.profile_name,
                                     enabled=1)
            log.info("[%s] IMEI 回填: %s", self.label(), imei)
            # v0.6.0-fix: 老短信 module_id 为空会被过滤隐藏, 回填归属
            try:
                self.store.backfill_module_id()
            except Exception as e:
                log.warning("[%s] backfill failed: %s", self.label(), e)
        elif imei != self.imei:
            log.warning("[%s] 端口上模块变了: 配置 %s, 实际 %s",
                        self.label(), self.imei, imei)

    def run(self):
        log.info("[%s] worker starting, IMEI=%s", self.label(), self.imei or "?")
        self._load_profile()
        # prime
        try:
            self.modem.open()
            log.info("[%s] modem opened", self.label())
            self._resolve_imei()
            self._refresh_imsi()
            try:
                self.modem.set_cnmi(2, 1, 0, 0, 0)
            except ModemError as e:
                log.warning("[%s] set_cnmi failed: %s", self.label(), e)
            for m in self.modem.sms_list_pdu(4):
                dec = m.get("decoded")
                if not dec:
                    continue
                key = (dec["sender"], dec["time"], dec["body"][:32])
                self._seen.add(key)
                row_id = self.store.save_incoming(
                    m["index"], dec["sender"], dec["body"], dec["time"],
                    module_id=self.imei)
                self.store.mark_forwarded(row_id)
            log.info("[%s] primed %d existing messages", self.label(),
                     len(self._seen))
        except ModemError as e:
            log.warning("[%s] prime failed: %s", self.label(), e)
        finally:
            try:
                self.modem.close()
            except Exception:
                pass
        # poll loop
        while not self._stop:
            try:
                n = self.poll_once()
                if n:
                    log.info("[%s] poll: %d new", self.label(), n)
            except Exception as e:
                log.warning("[%s] poll cycle error: %s", self.label(), e)
            # 可中断的 sleep
            for _ in range(self.poll_interval * 2):
                if self._stop:
                    break
                time.sleep(0.5)
        log.info("[%s] worker stopped", self.label())

    def _refresh_imsi(self):
        """v0.6.1: 每轮跟大轮询读 IMSI (不额外增加读取机制).

        3 轮滑动窗口容错: 单次偶发读不到用缓存, 3 轮内 >=2 次读不到则置空.
        SIM 热插拔自动跟上, 无需重启. 新 IMSI 自动发现进 sim_numbers 待填号码.
        """
        imsi = ""
        try:
            imsi = (self.modem.get_imsi() or "").strip()
        except Exception:
            imsi = ""
        self._imsi_window.append(bool(imsi))
        if imsi:
            if imsi != self.current_imsi:
                log.info("[%s] SIM IMSI: %s -> %s", self.label(),
                         self.current_imsi or "(无)", imsi)
                self.current_imsi = imsi
            # v0.6.0-fix: 每轮都 ensure (INSERT OR IGNORE, 自愈误删)
            try:
                self.store.ensure_sim_number(imsi)
            except Exception as e:
                log.warning("[%s] ensure_sim_number failed: %s",
                            self.label(), e)
        elif sum(1 for ok in self._imsi_window if not ok) >= 2:
            if self.current_imsi:
                log.warning("[%s] IMSI 连续读不到, 置空", self.label())
                self.current_imsi = ""
        # 写回 modules.current_imsi, web 查"号码现在在哪个模块"
        if self.imei:
            try:
                self.store.set_module_current_imsi(self.imei, self.current_imsi)
            except Exception:
                pass

    def poll_once(self):
        """One SMS poll cycle. Returns number of new messages."""
        import time as _time
        try:
            self.modem.open()
        except ModemError as e:
            log.warning("[%s] poll: cannot open modem: %s", self.label(), e)
            return 0
        try:
            self._refresh_imsi()
            try:
                msgs = self.modem.sms_list_pdu(4)
            except ModemError as e:
                log.warning("[%s] sms_list_pdu failed: %s", self.label(), e)
                return 0

            new_count = 0
            for m in msgs:
                idx = m["index"]
                if str(m["stat"]) not in ("0", "1"):
                    continue
                dec = m.get("decoded")
                if not dec:
                    log.warning("[%s] poll: idx=%d PDU 解析失败, 跳过",
                                self.label(), idx)
                    continue
                key = (dec["sender"], dec["time"], dec["body"][:32])
                if key in self._seen:
                    continue
                self._seen.add(key)

                if dec.get("is_concat"):
                    self._concat_add(idx, dec)
                else:
                    row_id = self.store.save_incoming(
                        idx, dec["sender"], dec["body"], dec["time"],
                        module_id=self.imei, imsi=self.current_imsi)
                    log.info("[%s] new SMS from %s: %s",
                             self.label(), dec["sender"], dec["body"][:60])
                    new_count += 1
                    self._queue_forward(row_id)
                    self._try_delete(idx)

            new_count += self._concat_flush(_time.time())

            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
            return new_count
        finally:
            try:
                self.modem.close()
            except Exception:
                pass

    def _concat_add(self, idx, dec):
        """长短信分片进缓冲."""
        import time as _time
        key = (dec["sender"], dec["ref"])
        buf = self._concat_buf.get(key)
        if buf is None:
            buf = {"total": dec["total"], "parts": {},
                   "indices": [], "time": dec["time"],
                   "first_seen": _time.time()}
            self._concat_buf[key] = buf
        if dec["total"] > buf["total"]:
            buf["total"] = dec["total"]
        if dec["seq"] not in buf["parts"]:
            buf["parts"][dec["seq"]] = dec["body"]
            buf["indices"].append(idx)
            log.info("[%s] concat part %d/%d from %s (ref %s)",
                     self.label(), dec["seq"], dec["total"],
                     dec["sender"], dec["ref"])
        self._try_delete(idx)

    def _concat_flush(self, now):
        """检查缓冲, 完整的重组存库. 返回新增条数."""
        done = 0
        stale_keys = []
        for key, buf in self._concat_buf.items():
            total = buf["total"]
            parts = buf["parts"]
            complete = (len(parts) == total and
                        all(s in parts for s in range(1, total + 1)))
            expired = (now - buf["first_seen"] > 86400)
            if not (complete or expired):
                continue
            sender = key[0]
            if complete:
                body = "".join(parts[s] for s in range(1, total + 1))
                log.info("[%s] concat complete from %s: %d parts",
                         self.label(), sender, total)
            else:
                seqs = sorted(parts.keys())
                body = "".join(parts[s] for s in seqs)
                body += f" [分片不完整 {len(seqs)}/{total}]"
                log.warning("[%s] concat incomplete from %s: %d/%d",
                            self.label(), sender, len(seqs), total)
            row_id = self.store.save_incoming(
                -1, sender, body, buf["time"], module_id=self.imei,
                imsi=self.current_imsi)
            done += 1
            self._queue_forward(row_id)
            stale_keys.append(key)
        for k in stale_keys:
            del self._concat_buf[k]
        return done

    def _queue_forward(self, row_id):
        channels = [n for n in ("qq", "telegram", "webhook", "bark")
                    if self.notifier._get_channel(n) is not None]
        if channels:
            self.store.queue_forward(row_id, channels)

    def _try_delete(self, idx):
        if not self.delete_after_forward:
            return
        try:
            self.modem.sms_delete(idx)
        except ModemError as e:
            log.warning("[%s] delete idx %d failed: %s",
                        self.label(), idx, e)


class Daemon:
    """Supervisor: 管理多个 ModuleWorker + 集中处理转发队列.

    v0.6.0: 配置从单 modem 改为 modules[] 列表, 自动迁移老配置.
    """

    def __init__(self, config):
        self.cfg = config
        sms_cfg = config.get("sms", {})
        self.poll_interval = sms_cfg.get("poll_interval", 15)

        data_path = config.get("data_dir", "/opt/scc-lite-for-EC20-4g-module/data")
        os.makedirs(data_path, exist_ok=True)
        self.data_path = data_path
        self.store = Store(os.path.join(data_path, "scc-lite.db"))
        self.notifier = Notifier(config.get("notifications", {}))

        # 配置迁移: 老 modem: -> modules 注册表
        self._migrate_config(config)
        self.workers = {}  # imei -> (ModuleWorker, Thread)

    def _migrate_config(self, config):
        """v0.6.0 迁移: config 里 modules[] -> DB 注册表;
        老 modem: 单端口 -> 记为 legacy_port, 由 worker 运行时读 IMEI 后回填."""
        modules = config.get("modules")
        if modules:
            for m in modules:
                if not m.get("imei"):
                    continue
                self.store.upsert_module(
                    m.get("imei"), alias=m.get("alias", ""),
                    port=m.get("port", "/dev/ttyUSB3"),
                    profile=m.get("profile", "EC20Profile"),
                    enabled=1 if m.get("enabled", True) else 0)
            log.info("migrated %d modules from config", len(modules))
            self._legacy_port = ""
            return
        # 老配置: 单 modem, 端口暂存, worker 运行时回填 IMEI
        modem_cfg = config.get("modem", {})
        self._legacy_port = modem_cfg.get("port", "/dev/ttyUSB3")
        if not self.store.get_modules():
            log.info("legacy modem port %s will be registered on first run",
                     self._legacy_port)

    def reload_notifications(self, notif_cfg):
        self.notifier.reload(notif_cfg)

    def process_forward_queue(self):
        """集中处理转发队列 (多模块共享)."""
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
                self.store.mark_forwarded(r["sms_id"])
                log.info("forwarded sms %d via %s", r["sms_id"], r["channel"])
            else:
                attempts = (r["attempts"] or 0) + 1
                self.store.mark_forward_failed(r["id"], attempts)
                log.warning("forward sms %d via %s failed (attempt %d)",
                            r["sms_id"], r["channel"], attempts)

    def _spawn_workers(self):
        import threading
        mods = self.store.get_modules(only_enabled=True)
        if not mods and getattr(self, "_legacy_port", ""):
            # 老配置迁移: 临时模块, IMEI 由 worker 运行时回填入库
            mods = [{"imei": "", "alias": "", "port": self._legacy_port,
                     "baudrate": 115200, "timeout": 10,
                     "profile": "EC20Profile", "enabled": 1}]
        for mod in mods:
            imei = mod["imei"] or mod["port"]  # 未回填 IMEI 前用端口做 key
            if imei in self.workers:
                continue
            sms_cfg = self.cfg.get("sms", {})
            w = ModuleWorker(mod, self.store, self.notifier,
                             self.data_path, sms_cfg)
            t = threading.Thread(target=w.run, daemon=True,
                                 name=f"scc-mod-{imei[-4:]}")
            t.start()
            self.workers[imei] = (w, t)
            log.info("worker started for %s", w.label())

    def run(self):
        log.info("SCC-lite daemon starting (v%s), multi-module mode",
                 VERSION)
        self._spawn_workers()
        if not self.workers:
            log.warning("no modules enabled, daemon idle")

        # Start QQ WebSocket receiver if configured (captures openids)
        qq_cfg = self.cfg.get("notifications", {}).get("qq", {})
        if HAS_QQ_WS and qq_cfg.get("enabled") and qq_cfg.get("app_id") and qq_cfg.get("app_secret"):
            try:
                from notifications import QQBot
                import qq_receiver
                qq_receiver.set_openid_file(
                    os.path.join(self.data_path, "qq_openids.json"))
                bot = QQBot(qq_cfg.get("app_id"), qq_cfg.get("app_secret"), "")
                receiver = QQReceiver(qq_cfg.get("app_id"), qq_cfg.get("app_secret"),
                                      bot._get_token)
                receiver.start()
                log.info("QQ WebSocket receiver started")
            except Exception as e:
                log.warning("QQ receiver failed to start: %s", e)
        elif qq_cfg.get("enabled"):
            log.info("QQ receiver not started (need websocket-client: pip install websocket-client)")

        pending = self.store.count_pending_forwards()
        if pending:
            log.info("startup: %d pending forwards to retry", pending)
            self.process_forward_queue()

        while True:
            try:
                try:
                    mtime = os.path.getmtime(self.config_path)
                    if mtime != self.config_mtime:
                        self.config_mtime = mtime
                        new_cfg = load_config(self.config_path)
                        self.notifier.reload(new_cfg.get("notifications", {}))
                        # 新加的模块: 补起 worker
                        self._migrate_config(new_cfg)
                        self._spawn_workers()
                        log.info("config reloaded")
                except Exception as e:
                    log.debug("config reload check failed: %s", e)
                self.process_forward_queue()
            except Exception as e:
                log.warning("supervisor cycle error: %s", e)
            # v0.6.0-fix: 周期性回填空 module_id (幂等, 无空值时零开销)
            # v0.6.1: 老短信 imsi 回填 (fallback, 单 IMSI 才自动)
            try:
                self.store.backfill_module_id()
                self.store.backfill_imsi()
            except Exception:
                pass
            time.sleep(self.poll_interval)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
DEFAULT_CONFIG = {
    # v0.6.0: 多模块. 老 modem: 单端口配置自动迁移为 modules[0].
    # modules: [{imei, alias, port, baudrate, profile, enabled}]
    "modules": [],
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
