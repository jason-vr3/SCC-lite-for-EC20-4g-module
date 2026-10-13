#!/usr/bin/env python3
"""
SCC-lite QQ Bot WebSocket receiver.

Connects outbound to QQ's gateway (no public IP needed) and captures
sender openids from incoming messages. Openids are saved to a JSON file
that the web UI displays.

Protocol (per https://bot.q.qq.com/wiki/develop/api-v2/):
  1. GET https://bots.qq.com/app/getAppAccessToken -> access_token
  2. GET https://api.sgroup.qq.com/gateway/bot -> {"url": "wss://..."}
  3. WebSocket connect -> receive op:10 Hello (heartbeat_interval)
  4. Send op:2 Identify {"token": "QQBot <access_token>", "intents": ...}
  5. Receive op:0 Dispatch events; send op:1 heartbeat periodically
  6. On C2C_MESSAGE_CREATE: d.author.user_openid
     On GROUP_AT_MESSAGE_CREATE: d.author.member_openid, d.group_openid

Requires: websocket-client (pip install websocket-client) or python3-websocket
via apt. Falls back to disabled if not available.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("scc-lite.qq")

# Where captured openids are stored (web UI reads this).
# Default; Daemon overrides with config's data_dir at startup.
OPENID_FILE = "/opt/scc-lite-for-EC20-4g-module/data/qq_openids.json"


def set_openid_file(path):
    """Override the openid storage path (called by Daemon with data_dir)."""
    global OPENID_FILE
    OPENID_FILE = path

# Intents: GROUP_AND_C2C_EVENT (1<<25) covers C2C_MESSAGE_CREATE,
# GROUP_AT_MESSAGE_CREATE, FRIEND_ADD, etc.
INTENTS_C2C_GROUP = (1 << 25)

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False


def _load_openids():
    try:
        with open(OPENID_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"c2c": {}, "group": {}}


def _save_openid(kind, openid, name="", extra=None):
    """kind: 'c2c' or 'group'. Saves openid with metadata."""
    data = _load_openids()
    entry = data[kind].get(openid, {})
    entry.update({"name": name or entry.get("name", ""),
                  "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "extra": extra or entry.get("extra", {})})
    data[kind][openid] = entry
    os.makedirs(os.path.dirname(OPENID_FILE), exist_ok=True)
    tmp = OPENID_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OPENID_FILE)
    log.info("QQ openid captured [%s]: %s (%s)", kind, openid, name)


class QQReceiver(threading.Thread):
    """
    Background thread: WebSocket to QQ gateway, captures openids.
    Only runs when app_id and app_secret are configured.
    """

    def __init__(self, app_id, app_secret, get_token_fn):
        super().__init__(daemon=True, name="qq-receiver")
        self.app_id = app_id
        self.app_secret = app_secret
        self.get_token_fn = get_token_fn  # callable -> access_token
        self._stop = threading.Event()
        self._ws = None
        self._seq = None

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _get_gateway(self, token):
        import urllib.request
        req = urllib.request.Request(
            "https://api.sgroup.qq.com/gateway/bot",
            headers={"Authorization": f"QQBot {token}",
                     "X-Union-Appid": self.app_id})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
            return data.get("url", "wss://api.sgroup.qq.com/websocket")

    def _on_message(self, ws, message):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        op = payload.get("op")
        if op == 10:  # Hello
            interval = payload.get("d", {}).get("heartbeat_interval", 45000)
            self._heartbeat_interval = interval / 1000.0
            # Identify
            token = self.get_token_fn()
            if not token:
                log.warning("QQ WS: no access token, cannot identify")
                return
            ws.send(json.dumps({
                "op": 2,
                "d": {"token": f"QQBot {token}",
                      "intents": INTENTS_C2C_GROUP,
                      "shard": [0, 1],
                      "properties": {"$os": "linux",
                                     "$browser": "scc-lite",
                                     "$device": "scc-lite"}}}
            ))
            log.info("QQ WS: Identify sent")
        elif op == 11:  # Heartbeat ACK
            pass
        elif op == 0:  # Dispatch
            t = payload.get("t", "")
            s = payload.get("s")
            if s is not None:
                self._seq = s
            d = payload.get("d", {})
            self._handle_event(t, d)
        elif op == 7:  # Reconnect
            log.info("QQ WS: server asked reconnect")
            ws.close()
        elif op == 9:  # Invalid session
            log.warning("QQ WS: invalid session")

    def _handle_event(self, t, d):
        if t == "READY":
            log.info("QQ WS: ready, session %s",
                     d.get("session_id", "")[:8])
        elif t == "C2C_MESSAGE_CREATE":
            author = d.get("author", {})
            openid = author.get("user_openid", "")
            name = author.get("username", "")
            content = d.get("content", "")[:50]
            if openid:
                _save_openid("c2c", openid, name,
                             {"last_msg": content})
                log.info("QQ C2C msg from %s (%s): %s", name, openid, content)
        elif t == "GROUP_AT_MESSAGE_CREATE":
            author = d.get("author", {})
            member_openid = author.get("member_openid", "")
            group_openid = d.get("group_openid", "")
            name = author.get("username", "")
            content = d.get("content", "")[:50]
            if member_openid:
                _save_openid("group", member_openid, name,
                             {"group_openid": group_openid,
                              "last_msg": content})
                log.info("QQ group @ from %s (%s): %s",
                         name, member_openid, content)
        elif t == "FRIEND_ADD":
            author = d.get("author", {})
            openid = author.get("user_openid", "")
            if openid:
                _save_openid("c2c", openid, author.get("username", ""))
                log.info("QQ new friend: %s", openid)

    def _heartbeat_loop(self):
        while not self._stop.wait(getattr(self, "_heartbeat_interval", 45)):
            try:
                if self._ws and self._ws.connected:
                    self._ws.send(json.dumps({"op": 1, "d": self._seq}))
            except Exception as e:
                log.debug("QQ heartbeat failed: %s", e)
                break

    def run(self):
        if not HAS_WS:
            log.warning("QQ WS: websocket-client not installed, receiver disabled")
            return
        while not self._stop.is_set():
            try:
                token = self.get_token_fn()
                if not token:
                    log.warning("QQ WS: cannot get access token, retry in 60s")
                    self._stop.wait(60)
                    continue
                url = self._get_gateway(token)
                log.info("QQ WS: connecting to gateway")
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=lambda ws, e: log.warning("QQ WS error: %s", e),
                    on_close=lambda ws, *a: log.info("QQ WS closed"),
                )
                hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
                hb.start()
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning("QQ WS loop error: %s", e)
            if not self._stop.is_set():
                log.info("QQ WS: reconnecting in 10s")
                self._stop.wait(10)


def get_captured_openids():
    """Return captured openids for web UI."""
    return _load_openids()
