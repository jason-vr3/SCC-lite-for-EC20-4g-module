#!/usr/bin/env python3
"""
SCC-lite - Notification channels.

Each channel is opt-in: user fills credentials in web UI, enables manually.
All channels share a common interface: send(title, body, extra={}) -> bool.

Channels:
  - QQ Bot     : Tencent QQ 机器人开放平台
                 Docs: https://bot.q.qq.com/wiki/
                 Flow: app_id + app_secret -> access_token -> send message
  - Telegram   : Bot API
                 Docs: https://core.telegram.org/bots/api#sendmessage
                 POST https://api.telegram.org/bot<token>/sendMessage
  - Webhook    : Generic HTTP POST with JSON payload
  - Bark       : iOS push via Bark app
                 Docs: https://github.com/Finb/Bark
                 GET https://api.day.app/<key>/<title>/<body>
"""

import json
import logging
import urllib.request
import urllib.parse

log = logging.getLogger("scc-lite.notify")


def _http_post(url, data, headers=None, timeout=10):
    """POST JSON, return (status_code, response_text)."""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers or {
        "Content-Type": "application/json",
        "User-Agent": "SCC-lite/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except Exception as e:
        log.warning("POST %s failed: %s", url, e)
        return 0, str(e)


def _http_get(url, timeout=10):
    """GET, return (status_code, response_text)."""
    req = urllib.request.Request(url, headers={"User-Agent": "SCC-lite/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return 0, str(e)


class QQBot:
    """
    Tencent QQ 机器人.

    Setup (user fills in web UI):
      app_id     - 机器人 AppID (from https://q.qq.com)
      app_secret - 机器人 AppSecret
      openid     - 接收者的 OpenID (user or group openid)

    API flow (per https://bot.q.qq.com/wiki/):
      1. POST https://bots.qq.com/app/getAppAccessToken
         {"appId": ..., "clientSecret": ...} -> {"access_token": ...}
      2. POST https://api.sgroup.qq.com/direct/message/create
         (for C2C direct message)

    Note: QQ Bot API requires the bot to be approved and the user to have
    interacted with the bot first. Verify against current QQ docs if fails.
    """

    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

    def __init__(self, app_id="", app_secret="", openid=""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.openid = openid
        self._token = None

    def _get_token(self):
        status, text = _http_post(self.TOKEN_URL, {
            "appId": self.app_id,
            "clientSecret": self.app_secret,
        })
        if status == 200:
            try:
                data = json.loads(text)
                self._token = data.get("access_token")
                return self._token
            except json.JSONDecodeError:
                pass
        log.warning("QQ getAppAccessToken failed: %s %s", status, text[:200])
        return None

    def send(self, title, body, extra=None):
        if not (self.app_id and self.app_secret and self.openid):
            log.warning("QQ Bot not configured")
            return False
        token = self._token or self._get_token()
        if not token:
            return False
        # C2C message endpoint (v2 API).
        # Source: QQ Bot official docs api-v2 + VoHiveX notifications.go
        # (verified: old /direct/message/create returns 404)
        url = f"https://api.sgroup.qq.com/v2/users/{self.openid}/messages"
        content = f"{title}\n{body}" if title else body
        status, text = _http_post(url, {
            "msg_type": 0,  # 0 = text
            "content": content,
        }, headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.app_id,
            "User-Agent": "SCC-lite/1.0",
        })
        ok = status == 200
        if not ok:
            log.warning("QQ send failed: %s %s", status, text[:200])
            # Token may have expired; clear for retry next time
            self._token = None
        return ok


class TelegramBot:
    """
    Telegram Bot.

    Setup: create bot via @BotFather, get token. Get chat_id via
    https://api.telegram.org/bot<token>/getUpdates after sending /start.

    Docs: https://core.telegram.org/bots/api#sendmessage
    """

    def __init__(self, bot_token="", chat_id=""):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, title, body, extra=None):
        if not (self.bot_token and self.chat_id):
            log.warning("Telegram not configured")
            return False
        text = f"*{title}*\n{body}" if title else body
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        status, resp = _http_post(url, {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        })
        ok = status == 200
        if not ok:
            log.warning("Telegram send failed: %s %s", status, resp[:200])
        return ok


class Webhook:
    """
    Generic HTTP webhook. POSTs JSON payload to user-configured URL.

    Payload: {"title": ..., "body": ..., "source": "scc-lite", ...extra}
    Optional: headers dict, secret (sent as X-SCC-Secret header).
    """

    def __init__(self, url="", headers=None, secret=""):
        self.url = url
        self.headers = headers or {}
        self.secret = secret

    def send(self, title, body, extra=None):
        if not self.url:
            log.warning("Webhook not configured")
            return False
        payload = {"title": title, "body": body, "source": "scc-lite"}
        if extra:
            payload.update(extra)
        headers = dict(self.headers)
        if self.secret:
            headers["X-SCC-Secret"] = self.secret
        status, resp = _http_post(self.url, payload, headers=headers)
        ok = 200 <= status < 300
        if not ok:
            log.warning("Webhook failed: %s %s", status, resp[:200])
        return ok


class Bark:
    """
    Bark iOS push notifications.

    Setup: install Bark app, get device key.
    Docs: https://github.com/Finb/Bark

    GET https://api.day.app/<key>/<title>/<body>
    Custom server: replace api.day.app with your server.
    """

    def __init__(self, key="", server="https://api.day.app"):
        self.key = key
        self.server = server.rstrip("/")

    def send(self, title, body, extra=None):
        if not self.key:
            log.warning("Bark not configured")
            return False
        t = urllib.parse.quote(title or "SCC-lite")
        b = urllib.parse.quote(body or "")
        url = f"{self.server}/{self.key}/{t}/{b}"
        status, resp = _http_get(url)
        ok = status == 200
        if not ok:
            log.warning("Bark failed: %s %s", status, resp[:200])
        return ok


# ----------------------------------------------------------------------
# Dispatcher: reads config, sends via all enabled channels
# ----------------------------------------------------------------------
class Notifier:
    """
    Reads notification config dict, dispatches to enabled channels.

    Config format (from config.yaml or web UI):
      notifications:
        qq:       {enabled: bool, app_id: "", app_secret: "", openid: ""}
        telegram: {enabled: bool, bot_token: "", chat_id: ""}
        webhook:  {enabled: bool, url: "", headers: {}, secret: ""}
        bark:     {enabled: bool, key: "", server: ""}
    """

    def __init__(self, config=None):
        self.config = config or {}
        self._channels = {}

    def _get_channel(self, name):
        if name in self._channels:
            return self._channels[name]
        cfg = self.config.get(name, {})
        if not cfg.get("enabled"):
            return None
        if name == "qq":
            ch = QQBot(cfg.get("app_id", ""), cfg.get("app_secret", ""),
                       cfg.get("openid", ""))
        elif name == "telegram":
            ch = TelegramBot(cfg.get("bot_token", ""), cfg.get("chat_id", ""))
        elif name == "webhook":
            ch = Webhook(cfg.get("url", ""), cfg.get("headers", {}),
                         cfg.get("secret", ""))
        elif name == "bark":
            ch = Bark(cfg.get("key", ""), cfg.get("server", ""))
        else:
            return None
        self._channels[name] = ch
        return ch

    def reload(self, config):
        """Reload config (e.g. after web UI change). Clears cached channels."""
        self.config = config or {}
        self._channels = {}

    def send_sms_notification(self, sender, body, sms_time=""):
        """
        Notify all enabled channels about a new SMS.
        Message includes sender, time, and body for full context.
        Returns dict {channel: bool}.
        """
        content = (f"📩 短信\n发件人：{sender}\n时间：{sms_time or '未知'}\n"
                   f"内容：{body}")
        results = {}
        for name in ("qq", "telegram", "webhook", "bark"):
            ch = self._get_channel(name)
            if ch is None:
                continue
            try:
                results[name] = ch.send(
                    "短信通知", content,
                    extra={"sender": sender, "time": sms_time,
                           "type": "sms"})
            except Exception as e:
                log.warning("notify %s failed: %s", name, e)
                results[name] = False
        return results

    def send_to_channel(self, channel, sender, body, sms_time=""):
        """Send to a single channel. Returns bool. Used by retry queue."""
        ch = self._get_channel(channel)
        if ch is None:
            return False
        content = (f"📩 短信\n发件人：{sender}\n时间：{sms_time or '未知'}\n"
                   f"内容：{body}")
        try:
            return ch.send("短信通知", content,
                           extra={"sender": sender, "time": sms_time,
                                  "type": "sms"})
        except Exception as e:
            log.warning("notify %s failed: %s", channel, e)
            return False

    def test(self, channel):
        """Send test message to one channel. Returns bool."""
        ch = self._get_channel(channel)
        if ch is None:
            return False
        try:
            return ch.send("SCC-lite 测试", "这是一条测试通知，如果收到说明配置正确。")
        except Exception as e:
            log.warning("test %s failed: %s", channel, e)
            return False
