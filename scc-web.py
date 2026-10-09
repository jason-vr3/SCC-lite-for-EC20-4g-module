#!/usr/bin/env python3
"""
SCC-lite (SMS Control Centre) - Web UI.

Flask app providing:
  - SMS inbox / send
  - Device status (signal, registration, IMEI/IMSI/ICCID)
  - Cellular data control (QMI on/off, IP, ping test)
  - Flight mode toggle
  - USSD terminal
  - AT command terminal
  - Notification channel settings (QQ/Telegram/Webhook/Bark)

Usage:
    python3 scc-web.py [--config /opt/scc-lite-for-EC20-4g-module/config.yaml]

Auth: HTTP Basic Auth (username/password from config).
"""

import argparse
import functools
import logging
import os
import re
import sqlite3
import yaml

VERSION = "0.5.6"
from flask import Flask, request, jsonify, render_template_string, Response

from modem import Modem, ModemError
from data_control import (get_data_controller, get_usbnet_mode,
                            get_carrier_info, provision_apn,
                            set_usbnet_mode, USBNET_MODES)
from notifications import Notifier
from ec20_data import EC20_COMMON, EC20_VARIANTS, FIRMWARE_NOTE, APPLICATIONS
from at_cheatsheet import search as at_search, by_category as at_by_cat, CATEGORIES

log = logging.getLogger("scc-lite.web")
app = Flask(__name__)

CONFIG_PATH = "/opt/scc-lite-for-EC20-4g-module/config.yaml"
config = {}
notifier = Notifier({})


def load_config():
    global config, notifier
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
    notifier.reload(config.get("notifications", {}))


def save_config():
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config, f, allow_unicode=True, default_flow_style=False)
    notifier.reload(config.get("notifications", {}))


def get_modem():
    m = Modem(
        port=config.get("modem", {}).get("port", "/dev/ttyUSB3"),
        baudrate=config.get("modem", {}).get("baudrate", 115200),
        timeout=10,
    )
    m.open()
    return m


def get_store():
    data_dir = config.get("data_dir", "/opt/scc-lite-for-EC20-4g-module/data")
    path = os.path.join(data_dir, "scc-lite.db")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def get_data_ctl():
    # modem_factory: 每次新建 Modem, 避免与短信模块抢串口
    def _factory():
        m = get_modem()
        return m
    at_port = config.get("modem", {}).get("port", "/dev/ttyUSB3")
    return get_data_controller(config, modem_factory=_factory,
                               at_port=at_port)


def _get_modem_for_data():
    """数据相关 AT 操作用的 Modem (用完即关)."""
    m = get_modem()
    return m


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------
def check_auth(username, password):
    w = config.get("web", {})
    return username == w.get("username", "admin") and \
        password == w.get("password", "admin")


def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="SCC-lite"'})
        # v0.5.6: 记录最后活跃时间 (用于缓存系统的有人/无人判断)
        _cache_touch()
        return f(*args, **kwargs)
    return decorated


# ----------------------------------------------------------------------
# v0.5.6 缓存系统: 内存 dict + 后台刷新线程, 低资源占用
#
# 设计:
# - API 优先返回缓存, 登录/进页面秒开, 不用等 AT/ QMI 扫描
# - 后台线程按活跃度切换频率: 有人用时勤刷, 无人时懒刷
# - 各端点独立间隔 (队列式, 非突发):
#     system: shell 命令, 轻量, 频率最高
#     data:   qmicli, 中量
#     device: AT 指令, 最慢且与 scc-lite daemon 抢串口, 频率减半, 错峰执行
# - "刷新"按钮带 ?fresh=1 强制实时扫描, 绕过缓存
# - 以下间隔均可按需调整 (秒)
# ----------------------------------------------------------------------
CACHE_ACTIVE_INTERVAL = 30    # 有人用时, system/data 刷新间隔 (秒)
CACHE_IDLE_INTERVAL = 300     # 无人时, system/data 刷新间隔 (秒, 5 分钟)
CACHE_IDLE_TIMEOUT = 300      # N 秒无认证请求则判无人 (5 分钟)
# device (AT 扫描) 独立间隔: 比上面减半频率, 减少串口争用
CACHE_DEVICE_ACTIVE = 60      # 有人用时, device 刷新间隔 (秒)
CACHE_DEVICE_IDLE = 600       # 无人时, device 刷新间隔 (秒, 10 分钟)

_cache = {}                   # key -> {"ts": float, "data": dict}
_cache_last_active = 0.0      # 最后认证请求时间戳
_cache_lock = None            # 延迟初始化 threading.Lock
_cache_thread = None


def _cache_lock_get():
    global _cache_lock
    if _cache_lock is None:
        import threading
        _cache_lock = threading.Lock()
    return _cache_lock


def _cache_touch():
    """记录活跃时间 (每次认证 API 调用)."""
    global _cache_last_active
    _cache_last_active = __import__("time").time()


def _cache_is_active():
    """5 分钟内有请求则算有人."""
    import time
    return (time.time() - _cache_last_active) < CACHE_IDLE_TIMEOUT


def _cache_get(key):
    with _cache_lock_get():
        e = _cache.get(key)
        return e["data"] if e else None


def _cache_set(key, data):
    import time
    with _cache_lock_get():
        _cache[key] = {"ts": time.time(), "data": data}


def _cache_invalidate(key=None):
    """清缓存 (key=None 则全清)."""
    with _cache_lock_get():
        if key:
            _cache.pop(key, None)
        else:
            _cache.clear()


def _cache_refresh_all():
    """后台刷新: 队列式, 各端点按独立间隔错峰更新, 单项失败不影响其他."""
    import logging
    import time
    log = logging.getLogger("scc-lite.web")
    active = _cache_is_active()
    now = time.time()
    # (key, 扫描函数, 活跃间隔, 空闲间隔)
    jobs = (
        ("system", _scan_system, CACHE_ACTIVE_INTERVAL, CACHE_IDLE_INTERVAL),
        ("data", _scan_data, CACHE_ACTIVE_INTERVAL, CACHE_IDLE_INTERVAL),
        ("device", _scan_device, CACHE_DEVICE_ACTIVE, CACHE_DEVICE_IDLE),
    )
    for key, fn, int_active, int_idle in jobs:
        interval = int_active if active else int_idle
        with _cache_lock_get():
            ts = _cache.get(key, {}).get("ts", 0)
        if now - ts < interval:
            continue  # 还没到该端点的刷新时间, 跳过 (错峰)
        try:
            _cache_set(key, fn())
        except Exception as e:
            log.warning("cache refresh %s failed: %s", key, e)


def _cache_loop():
    """后台线程: 短周期 tick, 由各端点独立间隔决定是否真刷 (队列式错峰)."""
    import time
    import logging
    log = logging.getLogger("scc-lite.web")
    log.info("cache thread started (tick=10s, system/data active=%ds idle=%ds, "
             "device active=%ds idle=%ds)",
             CACHE_ACTIVE_INTERVAL, CACHE_IDLE_INTERVAL,
             CACHE_DEVICE_ACTIVE, CACHE_DEVICE_IDLE)
    while True:
        time.sleep(10)  # tick 固定 10 秒, 轻量; 真正刷不刷由各端点间隔决定
        try:
            _cache_refresh_all()
        except Exception as e:
            log.warning("cache loop error: %s", e)


def _cache_start():
    """启动后台刷新线程 (daemon, 随主进程退出)."""
    global _cache_thread
    if _cache_thread and _cache_thread.is_alive():
        return
    import threading
    # 启动时先刷一次, 让登录进来就有数据
    try:
        _cache_refresh_all()
    except Exception:
        pass
    _cache_thread = threading.Thread(target=_cache_loop, daemon=True,
                                     name="scc-cache")
    _cache_thread.start()


# ----------------------------------------------------------------------
# HTML template (single-file, compact)
# ----------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCC-lite 短信中心</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--green:#8bc34a;--green-dark:#689f38;--bg:#f5f7f4;--card:#fff;
 --text:#2e3a2e;--muted:#7a8a7a;--border:#e4eae4}
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 background:var(--bg);color:var(--text);font-size:14px;display:flex;min-height:100vh}
/* Sidebar */
.sidebar{width:210px;background:#fff;border-right:1px solid var(--border);
 display:flex;flex-direction:column;position:fixed;top:0;bottom:0;left:0;z-index:10}
.sidebar .logo{padding:18px 16px;font-size:20px;font-weight:800;color:var(--green-dark);
 border-bottom:1px solid var(--border)}
.sidebar .logo small{display:block;font-size:11px;color:var(--muted);font-weight:400}
.sidebar nav{flex:1;overflow-y:auto;padding:10px 8px}
.sidebar nav button{display:flex;align-items:center;gap:10px;width:100%;border:0;
 background:none;padding:11px 12px;font-size:14px;cursor:pointer;color:#4a5a4a;
 border-radius:12px;margin-bottom:2px;text-align:left}
.sidebar nav button:hover{background:#f0f5ef}
.sidebar nav button.active{background:var(--green);color:#fff;font-weight:600}
.sidebar nav button .ico{font-size:17px;width:24px;text-align:center}
.sidebar .user{padding:12px 16px;border-top:1px solid var(--border);font-size:13px;
 display:flex;justify-content:space-between;align-items:center}
.sidebar .user a{color:var(--muted);text-decoration:none}
/* Main */
.main{flex:1;margin-left:210px;padding:20px;max-width:1100px}
.main h1.page-title{font-size:24px;margin-bottom:4px}
.main .page-sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.card{background:var(--card);border-radius:16px;padding:18px;margin-bottom:14px;
 box-shadow:0 1px 4px rgba(0,0,0,.05)}
.card h2{font-size:16px;margin-bottom:12px}
.card h3{font-size:14px;margin:12px 0 8px;color:#4a5a4a}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input,select,textarea{border:1px solid #d0d8d0;border-radius:10px;padding:9px 12px;
 font-size:14px;width:100%;background:#fbfdfb}
input:focus{outline:2px solid var(--green);border-color:var(--green)}
input[type=checkbox]{width:auto}
button.btn{background:var(--green);color:#fff;border:0;border-radius:20px;
 padding:8px 22px;font-size:14px;cursor:pointer;font-weight:600}
button.btn:hover{background:var(--green-dark)}
button.btn.danger{background:#e57373}
button.btn.ghost{background:#eef3ee;color:#4a5a4a}
button.btn:disabled{background:#ccc}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:500}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px}
.badge.ok{background:#e8f5e9;color:#2e7d32}
.badge.bad{background:#ffebee;color:#c62828}
.badge.info{background:#e8f5e9;color:var(--green-dark)}
pre{background:#263238;color:#c3e88d;border-radius:12px;padding:12px;overflow-x:auto;
 font-size:12px;max-height:500px;overflow-y:auto}
.hidden{display:none}
label{font-size:13px;color:var(--muted)}
.msg{border-left:3px solid var(--green);padding:10px 12px;margin-bottom:8px;
 background:#fafcfa;border-radius:0 12px 12px 0}
.msg.out{border-color:#66bb6a}
.msg .meta{font-size:12px;color:var(--muted);margin-bottom:4px}
/* iOS-style chat bubbles */
.bubble{max-width:75%;padding:10px 14px;border-radius:18px;margin-bottom:8px;word-wrap:break-word;font-size:14px;line-height:1.4}
.bubble.in{background:#e9e9eb;color:#000;align-self:flex-start;border-bottom-left-radius:4px}
.bubble.out{background:#0a84ff;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.bubble .time{font-size:10px;opacity:0.7;margin-top:4px}
.contact-item{padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer}
.contact-item:hover{background:#f0f5ef}
.contact-item.active{background:#e8f5e9}
.contact-item .num{font-weight:600;font-size:14px}
.contact-item .preview{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.kv{display:grid;grid-template-columns:120px 1fr;gap:6px 10px;font-size:13px}
.kv dt{color:var(--muted)}.kv dd{margin:0;word-break:break-all}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.stat{background:var(--card);border-radius:16px;padding:16px;text-align:center;
 box-shadow:0 1px 4px rgba(0,0,0,.05)}
.stat .num{font-size:26px;font-weight:700;color:var(--green-dark)}
.stat .lbl{font-size:12px;color:var(--muted);margin-top:4px}
@media(max-width:768px){.sidebar{width:64px}.sidebar .logo small,.sidebar nav button span.txt,.sidebar .user span{display:none}.main{margin-left:64px}}
</style></head>
<body>
<aside class="sidebar">
<div class="logo">📡 SCC-lite<small>SMS Control Centre v{{ version }}</small></div>
<nav id="tabs">
<button data-t="dash" class="active"><span class="ico">📊</span><span class="txt">仪表盘</span></button>
<button data-t="sms"><span class="ico">💬</span><span class="txt">短信中心</span></button>
<button data-t="device"><span class="ico">📱</span><span class="txt">设备管理</span></button>
<button data-t="data"><span class="ico">🌐</span><span class="txt">蜂窝网络</span></button>
<button data-t="ussd"><span class="ico">⌨️</span><span class="txt">USSD</span></button>
<button data-t="at"><span class="ico">💻</span><span class="txt">AT 终端</span></button>
<button data-t="ec20"><span class="ico">📖</span><span class="txt">EC20 资料</span></button>
<button data-t="atdoc"><span class="ico">📚</span><span class="txt">AT 速查</span></button>
<button data-t="logs"><span class="ico">📋</span><span class="txt">实时日志</span></button>
<button data-t="notify"><span class="ico">🔔</span><span class="txt">消息推送</span></button>
</nav>
<div class="user"><span id="user-label" onclick="openAccountModal()" style="cursor:pointer" title="点击修改账户">👤 Admin</span><a href="/logout">退出</a></div>
<!-- 修改账户模态框 -->
<div id="account-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);z-index:99;align-items:center;justify-content:center">
<div style="background:#fff;border-radius:8px;padding:24px;width:320px">
<h3 style="margin-bottom:16px">修改登录账户+密码</h3>
<div style="margin-bottom:12px"><label style="font-size:13px">用户名</label><input id="acc-username" style="width:100%;margin-top:4px"></div>
<div style="margin-bottom:12px"><label style="font-size:13px">旧密码 <span style="color:#999">(验证身份)</span></label><input id="acc-oldpw" type="password" style="width:100%;margin-top:4px"></div>
<div style="margin-bottom:12px"><label style="font-size:13px">新密码 <span style="color:#999">(至少4位)</span></label><input id="acc-newpw" type="password" style="width:100%;margin-top:4px"></div>
<div style="margin-bottom:12px"><label style="font-size:13px">确认新密码</label><input id="acc-newpw2" type="password" style="width:100%;margin-top:4px"></div>
<div style="font-size:12px;color:#666;margin-bottom:16px">改用户名时必须同时设新密码（至少4位），不能为空</div>
<div id="acc-msg" style="font-size:13px;color:#c00;margin-bottom:12px"></div>
<div style="display:flex;gap:8px;justify-content:flex-end">
<button class="btn ghost" onclick="closeAccountModal()">取消</button>
<button class="btn" onclick="submitAccountChange()">保存</button></div></div></div>
</aside>
<div class="main">
<!-- Dashboard -->
<section id="t-dash">
<h1 class="page-title">仪表盘</h1><p class="page-sub">实时查看设备状态与短信</p>
<div class="stats">
<div class="stat"><div class="num" id="st-signal">-</div><div class="lbl">信号 RSSI</div></div>
<div class="stat"><div class="num" id="st-reg">-</div><div class="lbl">网络注册</div></div>
<div class="stat"><div class="num" id="st-sms">-</div><div class="lbl">短信总数</div></div>
<div class="stat"><div class="num" id="st-data">-</div><div class="lbl">蜂窝网络</div></div>
</div>
<div style="display:flex;gap:14px;flex-wrap:wrap">
<div class="card" style="flex:1;min-width:280px"><h2>本地设备信息 <button class="btn ghost" onclick="loadDashSys(1)">刷新</button></h2>
<div id="dash-sys">加载中…</div></div>
<div class="card" style="flex:1;min-width:280px"><h2>4G 模块 <button class="btn ghost" onclick="loadDashModem()">刷新</button></h2>
<div id="dash-modem">加载中…</div>
<p class="muted">点击模块可跳转到设备管理页查看详情</p></div>
<div class="card" style="flex:1;min-width:280px"><h2>💾 USB 外接设备 <button class="btn ghost" onclick="loadUsb()">刷新</button></h2>
<div id="samba-status" style="font-size:12px;color:var(--muted);margin-bottom:6px">Samba 状态: 检查中…</div>
<div id="usb-list" style="font-size:13px">加载中…</div>
<div style="margin-top:8px;font-size:12px;color:var(--muted)">💡 挂载后点"创建Samba共享"，PC 用 Web 登录的账密访问共享，文件互传。</div></div>
</div>
</section>
<!-- SMS (iOS style) -->
<section id="t-sms" class="hidden">
<h1 class="page-title">短信中心</h1><p class="page-sub">iOS 风格会话</p>
<div class="card" style="padding:12px">
<div class="row">
<button class="btn ghost" onclick="exportBackup()">📥 全量导出</button>
<button class="btn ghost" onclick="$('import-file').click()">📤 全量导入</button>
<input type="file" id="import-file" accept=".json" style="display:none" onchange="importBackup(this)">
<span id="backup-status" style="font-size:12px"></span>
</div>
</div>
<div class="card" style="padding:0;overflow:hidden">
<div style="display:flex;min-height:500px">
<!-- 左：联系人列表 -->
<div id="sms-contacts" style="width:280px;border-right:1px solid var(--border);overflow-y:auto;max-height:600px">
<div style="padding:12px;border-bottom:1px solid var(--border)">
<input id="sms-to" placeholder="新号码，如 13800138000" style="margin-bottom:8px">
<button class="btn" onclick="newConversation()" style="width:100%">＋ 新会话</button>
</div>
<div id="contact-list">加载中…</div>
</div>
<!-- 右：会话气泡 -->
<div style="flex:1;display:flex;flex-direction:column">
<div id="conv-header" style="padding:12px 16px;border-bottom:1px solid var(--border);font-weight:600;display:flex;justify-content:space-between;align-items:center">
<span id="conv-title">选择联系人</span>
<span>
<button class="btn ghost" id="btn-export-contact" onclick="exportContact()" style="display:none;font-size:12px;padding:4px 12px">导出</button>
<button class="btn danger" id="btn-del-contact" onclick="deleteContact()" style="display:none;font-size:12px;padding:4px 12px">删除会话</button>
</span>
</div>
<div id="conv-messages" style="flex:1;overflow-y:auto;padding:16px;max-height:450px;background:#f8faf8">
<div style="text-align:center;color:var(--muted);padding:40px">← 选择左侧联系人查看会话</div>
</div>
<div style="padding:12px;border-top:1px solid var(--border)">
<div style="display:flex;gap:8px;align-items:flex-end">
<textarea id="sms-text" placeholder="输入短信内容…" rows="2" style="flex:1;resize:none;overflow-y:hidden" oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px';updateCharCount()"></textarea>
<button class="btn" onclick="sendSms()">发送</button>
</div>
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
<span id="char-count" style="font-size:12px;color:var(--muted)">0/70</span>
<span id="sms-send-status" style="font-size:12px"></span>
</div>
</div>
<div style="padding:0 12px 8px"><button class="btn ghost" onclick="loadSms()" style="float:right;font-size:12px;padding:4px 12px">刷新</button></div>
</div>
</div>
</div>
</section>
<!-- Device -->
<section id="t-device" class="hidden">
<h1 class="page-title">设备管理</h1><p class="page-sub">模组状态与射频控制</p>
<div class="card"><h2>设备状态 <button class="btn ghost" onclick="loadDevice(1)">刷新</button></h2>
<div id="device-info">加载中…</div></div>
<div class="card"><h2>飞行模式</h2>
<div class="row"><span id="flight-status">未知</span>
<button class="btn" onclick="setFlight(true)">开启飞行模式</button>
<button class="btn ghost" onclick="setFlight(false)">关闭飞行模式</button></div></div>
</section>
<!-- Data -->
<section id="t-data" class="hidden">
<h1 class="page-title">蜂窝网络</h1><p class="page-sub">QMI 数据连接管理 (v0.5.5 纯 QMI)</p>
<div class="card"><h2>数据连接 <button class="btn ghost" onclick="loadData(1)">刷新</button></h2>
<div id="data-info">加载中…</div>
<div id="data-stages" style="margin-top:12px"></div>
<div class="row" style="margin-top:8px">
<button class="btn" id="btn-data-on" onclick="dataOn()">开启上网</button>
<button class="btn danger" id="btn-data-off" onclick="dataOff()">关闭上网</button>
<button class="btn ghost" id="btn-conn-test" onclick="testConnectivity()">测试连通性</button></div>
<div id="conn-result" style="margin-top:8px"></div></div>
<div class="card"><h2>QMI 操作日志 <button class="btn ghost" onclick="toggleOpLog()">展开/折叠</button>
<button class="btn ghost" onclick="loadOpLog()">刷新</button></h2>
<div id="oplog-box" style="display:none"><pre id="oplog-output" style="max-height:300px;overflow-y:auto">点击刷新查看…</pre></div></div>
<div class="card"><h2>运营商与 APN <button class="btn ghost" onclick="loadCarrier()">识别</button></h2>
<div id="carrier-info">点击"识别"自动检测…</div>
<div class="row" style="margin-top:8px"><input id="apn-input" placeholder="手动填写 APN，如 cbnet" style="flex:2">
<button class="btn" onclick="setApn()">下发 APN</button></div>
<div class="row" style="margin-top:8px"><span>USB 网络模式：</span><b id="usbnet-mode">-</b>
<button class="btn ghost" onclick="switchMode()">切换 ECM/QMI</button></div></div>
<div class="card"><h2>Ping 测试</h2>
<div class="row"><input id="ping-target" value="2400:3200::1" style="flex:2">
<button class="btn" onclick="pingTest()">Ping 3 次</button></div>
<div id="ping-result"></div>
<p class="muted">注：广电 IPv4 被拦截，请用 IPv6 目标测试</p></div>
<div class="card"><h2>实现说明 <button class="btn ghost" onclick="copyImpl()">复制指令</button></h2>
<pre id="impl-notes">加载中…</pre></div>
</section>
<!-- USSD -->
<section id="t-ussd" class="hidden">
<h1 class="page-title">USSD</h1><p class="page-sub">交互式运营商指令（如查余额）</p>
<div class="card"><h2>USSD 会话</h2>
<div class="row"><input id="ussd-code" placeholder="如 *101#" style="flex:2">
<button class="btn" onclick="ussdSend()">发送</button>
<button class="btn ghost" onclick="ussdCancel()">取消会话</button></div>
<pre id="ussd-result">等待输入…</pre></div>
</section>
<!-- AT terminal -->
<section id="t-at" class="hidden">
<h1 class="page-title">AT 终端</h1><p class="page-sub">直接执行 AT 指令</p>
<div class="card"><h2>AT 命令</h2>
<div class="row"><input id="at-cmd" placeholder="如 AT+CSQ" style="flex:3">
<button class="btn" onclick="atSend()">执行</button></div>
<pre id="at-result">等待输入…</pre></div>
</section>
<!-- EC20 资料 -->
<section id="t-ec20" class="hidden">
<h1 class="page-title">EC20 资料</h1><p class="page-sub">EC20 全系规格速查</p>
<div class="card"><h2>EC20 全系 <button class="btn ghost" onclick="loadEc20()">刷新</button></h2>
<div id="ec20-info">加载中…</div></div>
</section>
<!-- AT 速查 -->
<section id="t-atdoc" class="hidden">
<h1 class="page-title">AT 速查</h1><p class="page-sub">EC20 指令速查（标注出处）</p>
<div class="card"><h2>指令搜索</h2>
<div class="row"><input id="atdoc-q" placeholder="输入关键字，如 SMS / CREG / QCFG" style="flex:3">
<button class="btn" onclick="loadAtdoc()">搜索</button></div>
<div class="row" id="atdoc-cats"></div>
<div id="atdoc-list">加载中…</div>
<p style="font-size:12px;color:var(--muted);margin-top:8px">出处：[3GPP]=3GPP TS 27.007，[Q]=Quectel AT 手册，[QCFG]=Quectel QCFG 手册</p></div>
</section>
<!-- 实时日志 -->
<section id="t-logs" class="hidden">
<h1 class="page-title">实时日志</h1><p class="page-sub">守护进程与 Web 日志</p>
<div class="card"><h2>日志
<button class="btn ghost" onclick="loadLogs()">刷新</button>
<label style="margin-left:8px"><input type="checkbox" id="log-auto" checked onchange="toggleLogAuto()"> 自动刷新(5s)</label></h2>
<div class="row">
<button class="btn ghost" onclick="logService='scc-lite';loadLogs()">守护进程</button>
<button class="btn ghost" onclick="logService='scc-web';loadLogs()">Web</button>
<button class="btn ghost" onclick="logService='qmi';loadLogs()">QMI 拨号</button>
<span id="log-service-label" style="font-size:13px;color:var(--muted)">scc-lite</span></div>
<pre id="log-output">加载中…</pre></div>
</section>
<!-- Notify -->
<section id="t-notify" class="hidden">
<h1 class="page-title">消息推送</h1><p class="page-sub">通知渠道按需启用</p>
<div class="card"><h2>通知渠道</h2><div id="notify-form">加载中…</div>
<div class="row"><button class="btn" onclick="saveNotify()">保存</button>
<span id="notify-status"></span></div></div>
</section>
</div>
<script>
const $=id=>document.getElementById(id);
function goTab(t){const b=document.querySelector(`#tabs button[data-t="${t}"]`);if(b)b.click();}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('active'));
 b.classList.add('active');
 document.querySelectorAll('.main>section').forEach(s=>s.classList.add('hidden'));
 $('t-'+b.dataset.t).classList.remove('hidden');
 ({dash:loadDash,sms:loadSms,device:loadDevice,data:loadData,notify:loadNotify,ec20:loadEc20,atdoc:loadAtdoc,logs:loadLogs}[b.dataset.t]||(()=>{}))();
});
async function api(p,o={}){const r=await fetch(p,{...o,headers:{'Content-Type':'application/json',...(o.headers||{})}});
 if(r.status===401){location.reload();return null} return r.json()}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function loadDash(){const [dev,sms,data]=await Promise.all([api('/api/device'),api('/api/sms'),api('/api/data')]);
 if(dev){$('st-signal').textContent=dev.signal?.rssi??'-';
  const rs={0:'未注册',1:'已注册',2:'搜索中',3:'被拒',5:'漫游'};$('st-reg').textContent=rs[dev.reg?.stat]??'-';}
 if(sms)$('st-sms').textContent=sms.messages.length;
 if(data)$('st-data').textContent=data.connected?((data.ipv6&&data.ipv6[0])||data.ip||'已连接'):'未连接';
 loadDashSys();loadDashModem();loadUsb();}
function bar(pct){const c=pct>80?'#e53935':(pct>60?'#fb8c00':'#8bc34a');
 return `<div style="background:#eee;border-radius:4px;height:8px;flex:1"><div style="background:${c};height:8px;border-radius:4px;width:${Math.min(pct,100)}%"></div></div>`;}
async function loadDashSys(fresh){const d=await api('/api/system'+(fresh?'?fresh=1':''));if(!d)return;
 const ifs=d.interfaces||[];
 const defIf=ifs.find(i=>i.is_default)||ifs.find(i=>i.name!=='lo')||{};
 const defIp=[...(defIf.ipv4||[]),...(defIf.ipv6||[])].join('<br>')||'-';
 const allIfs=ifs.filter(i=>i.name!=='lo').map(i=>
  `<div style="font-size:12px;padding:6px 0;border-bottom:1px solid var(--border)">`+
  `<b>${esc(i.name)}</b>${i.is_default?' <span class="badge ok">默认</span>':''}<br>`+
  `IPv4: ${i.ipv4.map(esc).join(', ')||'-'}<br>`+
  `IPv6: ${i.ipv6.map(esc).join(', ')||'-'}<br>`+
  `<span style="color:var(--muted)">MAC ${esc(i.mac)} · ↓${esc(i.rx)} ↑${esc(i.tx)}</span></div>`
 ).join('')||'无';
 $('dash-sys').innerHTML=`<dl class="kv">
 <dt>CPU</dt><dd>${esc(d.cpu_model||'-')} (${d.cpu_cores}核)</dd>
 <dt>CPU 占用</dt><dd style="display:flex;gap:8px;align-items:center">${bar(d.cpu_usage||0)}<span>${d.cpu_usage??'-'}%</span></dd>
 <dt>内存</dt><dd style="display:flex;gap:8px;align-items:center">${bar(d.mem_usage||0)}<span>${d.mem_used_mb}/${d.mem_total_mb} MB (${d.mem_usage??'-'}%)</span></dd>
 <dt>磁盘 /</dt><dd style="display:flex;gap:8px;align-items:center">${bar(d.disk_usage||0)}<span>${d.disk_used_gb}/${d.disk_total_gb} GB (${d.disk_usage??'-'}%)</span></dd>
 <dt>主网卡</dt><dd>${esc(defIf.name||'-')} (${defIp})</dd>
 <dt>网关</dt><dd>${esc(d.gateway||'-')}</dd>
 <dt>DNS</dt><dd>${(d.dns||[]).map(esc).join('<br>')||'-'}</dd>
 <dt>运行时间</dt><dd>${esc(d.uptime||'-')}</dd>
 <dt>负载</dt><dd>${(d.loadavg||[]).join(' ')||'-'}</dd></dl>
 <div style="margin-top:8px"><a href="javascript:void(0)" onclick="toggleIfs()" style="font-size:13px">展开所有网卡 (${ifs.filter(i=>i.name!=='lo').length}) ▾</a>
 <div id="ifs-detail" style="display:none;margin-top:4px">${allIfs}</div></div>
`;}
function toggleIfs(){const b=$('ifs-detail');b.style.display=b.style.display==='none'?'block':'none';}
// USB 管理 (v0.5.6)
async function loadUsb(){
 const smb=await api('/api/system/samba/status');
 if(smb){$('samba-status').innerHTML='Samba 状态: '+(smb.installed?(smb.running?'<span class="badge ok">运行中</span>':'<span class="badge bad">未运行</span>'):'<span style="color:#999">未安装 (sudo bash install.sh)</span>');}
 const d=await api('/api/system/usb');if(!d)return;
 const devs=d.devices||[];
 if(!devs.length){$('usb-list').innerHTML='未检测到 USB 存储设备';return;}
 const shares={};(smb&&smb.shares||[]).forEach(x=>shares[x.path]=x.name);
 $('usb-list').innerHTML=devs.map(v=>{
  const mp=v.mountpoint;
  const sh=mp?shares[mp]:null;
  let btn='';
  if(!mp)btn=`<button class="btn" style="font-size:12px;padding:2px 8px" onclick="usbMount('${esc(v.dev)}')">挂载</button>`;
  else btn=`<button class="btn ghost" style="font-size:12px;padding:2px 8px" onclick="usbUmount('${esc(v.dev)}')">卸载</button>`;
  let smb='';
  if(mp&&!sh)smb=` <button class="btn ghost" style="font-size:12px;padding:2px 8px" onclick="sambaShare('${esc(mp)}')">创建Samba共享</button>`;
  if(sh)smb=` <span class="badge ok">已共享: ${esc(sh)}</span>`;
  return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
   <b>${esc(v.label||v.name)}</b> <span style="color:var(--muted)">${esc(v.dev)} · ${esc(v.size)} · ${esc(v.fstype||'?')}</span><br>
   <span style="color:var(--muted)">${mp?'挂载点: '+esc(mp):'未挂载'}</span><br>${btn}${smb}</div>`;
 }).join('');
}
async function usbMount(dev){const d=await api('/api/system/usb/mount',{method:'POST',body:JSON.stringify({dev})});
 if(d&&d.ok)loadUsb();else alert('挂载失败: '+(d&&d.error||'未知'));}
async function usbUmount(dev){if(!confirm('卸载将同时删除其 Samba 共享，确定？'))return;
 const d=await api('/api/system/usb/umount',{method:'POST',body:JSON.stringify({dev})});
 if(d&&d.ok)loadUsb();else alert('卸载失败: '+(d&&d.error||'未知'));}
async function sambaShare(path){const d=await api('/api/system/samba/share',{method:'POST',body:JSON.stringify({path})});
 if(d&&d.ok){alert('共享已创建！\n'+d.hint);loadUsb();}else alert('创建失败: '+(d&&d.error||'未知'));}
async function loadDashModem(){const d=await api('/api/device');if(!d)return;
 const rs={0:'未注册',1:'已注册(本地)',2:'搜索中',3:'被拒绝',5:'已注册(漫游)'}[d.reg?.stat]??'-';
 $('dash-modem').innerHTML=`<div onclick="goTab('device')" style="cursor:pointer"><dl class="kv">
 <dt>型号</dt><dd>${esc(d.model||'-')}</dd>
 <dt>IMEI</dt><dd>${esc(d.imei||'-')}</dd>
 <dt>IMSI</dt><dd>${esc(d.imsi||'-')}</dd>
 <dt>信号</dt><dd>RSSI ${d.signal?.rssi??'-'}</dd>
 <dt>注册</dt><dd>${rs}</dd>
 <dt>运营商</dt><dd>${esc(d.operator?.oper||'-')}</dd>
 <dt>串口</dt><dd>${esc(d.port||'-')}</dd></dl></div>`;}
let smsMessages=[],currentContact=null;
async function loadSms(){const d=await api('/api/sms');if(!d)return;
 smsMessages=d.messages;
 renderContacts();
 if(currentContact)renderConversation(currentContact);}
function getContacts(){
 const map=new Map();
 for(const m of smsMessages){
  const num=m.direction==='in'?m.sender:(m.sender||'');
  if(!num)continue;
  if(!map.has(num))map.set(num,{num,last:m,preview:m.body?.slice(0,30)});
  else{const c=map.get(num);
   if((m.id||0)>(c.last.id||0)){c.last=m;c.preview=m.body?.slice(0,30);}}}
 return [...map.values()].sort((a,b)=>(b.last.id||0)-(a.last.id||0));
}
function renderContacts(){
 const cs=getContacts();
 $('contact-list').innerHTML=cs.length?cs.map(c=>
  `<div class="contact-item ${currentContact===c.num?'active':''}" onclick="selectContact('${esc(c.num)}')">
   <div class="num">${esc(c.num)}</div>
   <div class="preview">${esc(c.preview||'')}</div></div>`).join('')
  :'<div style="padding:20px;color:var(--muted);text-align:center">暂无会话</div>';
}
function selectContact(num){currentContact=num;renderContacts();renderConversation(num);
 $('conv-title').textContent=num;
 $('btn-export-contact').style.display='';$('btn-del-contact').style.display='';}
function newConversation(){const num=$('sms-to').value.trim();
 if(!num)return alert('先在上方输入号码');
 currentContact=num;$('sms-to').value='';renderContacts();renderConversation(num);
 $('conv-title').textContent=num;
 $('btn-export-contact').style.display='';$('btn-del-contact').style.display='';}
// 字数统计：中文按70，英文按160
function smsLimit(text){return /[^\x00-\x7F]/.test(text)?70:160;}
function updateCharCount(){const t=$('sms-text').value;
 $('char-count').textContent=t.length+'/'+smsLimit(t);}
function renderConversation(num){
 const msgs=smsMessages.filter(m=>{
  const n=m.direction==='in'?m.sender:m.sender;
  return n===num;}).sort((a,b)=>(a.id||0)-(b.id||0));
 $('conv-title').textContent=num+' ('+msgs.length+' 条)';
 $('conv-messages').innerHTML=msgs.length?msgs.map(m=>{
  const t=m.sms_time||m.created_at||'';
  const lim=smsLimit(m.body||'');
  return `<div style="display:flex;flex-direction:column;position:relative">
   <div class="bubble ${m.direction==='in'?'in':'out'}">${esc(m.body)}
   <div class="time">${esc(t)}  ${m.body?.length||0}/${lim}
   <a href="javascript:deleteMsg(${m.id})" style="margin-left:6px;opacity:.6" title="删除">✕</a></div></div></div>`;}).join('')
  :'<div style="text-align:center;color:var(--muted);padding:40px">暂无消息，输入内容发送吧</div>';
 const el=$('conv-messages');el.scrollTop=el.scrollHeight;
}
async function deleteMsg(id){if(!confirm('删除这条短信？'))return;
 await api('/api/sms/'+id,{method:'DELETE'});loadSms();}
async function deleteContact(){if(!currentContact)return;
 if(!confirm('删除与 '+currentContact+' 的全部会话？'))return;
 const d=await api('/api/sms/contact/'+encodeURIComponent(currentContact),{method:'DELETE'});
 if(d&&d.ok){currentContact=null;
  $('conv-title').textContent='选择联系人';
  $('btn-export-contact').style.display='none';$('btn-del-contact').style.display='none';
  $('conv-messages').innerHTML='<div style="text-align:center;color:var(--muted);padding:40px">← 选择左侧联系人查看会话</div>';
  loadSms();}}
function exportBackup(){window.location='/api/sms/export';}
function exportContact(){if(!currentContact)return;
 window.location='/api/sms/export/'+encodeURIComponent(currentContact);}
async function importBackup(input){const f=input.files[0];if(!f)return;
 $('backup-status').textContent='导入中…';
 try{const text=await f.text();const data=JSON.parse(text);
  const d=await api('/api/sms/import',{method:'POST',body:JSON.stringify(data)});
  $('backup-status').textContent=d&&d.ok?`✅ 导入 ${d.imported}/${d.total} 条`:'❌ '+(d&&d.error||'失败');
  if(d&&d.ok)loadSms();
 }catch(e){$('backup-status').textContent='❌ 文件格式错误';}
 input.value='';}
async function sendSms(){const to=(currentContact||$('sms-to').value.trim()),text=$('sms-text').value;
 if(!to||!text)return alert('选择联系人并填写内容');
 $('sms-send-status').textContent='发送中…';
 const d=await api('/api/sms/send',{method:'POST',body:JSON.stringify({to,text})});
 $('sms-send-status').textContent=d&&d.ok?'✅ 已发送':'❌ '+(d&&d.error||'失败');
 if(d&&d.ok){$('sms-text').value='';$('sms-text').style.height='auto';currentContact=to;setTimeout(loadSms,1000);}}
async function loadDevice(fresh){const d=await api('/api/device'+(fresh?'?fresh=1':''));if(!d)return;
 const r=d.reg||{};const regTxt={0:'未注册',1:'已注册(本地)',2:'搜索中',3:'被拒绝',5:'已注册(漫游)'}[r.stat]??('stat='+r.stat);
 $('device-info').innerHTML=`<dl class="kv">
 <dt>IMEI</dt><dd>${esc(d.imei)}</dd><dt>IMSI</dt><dd>${esc(d.imsi)}</dd>
 <dt>ICCID</dt><dd>${esc(d.iccid)}</dd><dt>信号</dt><dd>RSSI ${d.signal?.rssi??'-'} ${d.signal&&d.signal.rssi<=31?'<span class="badge '+(d.signal.rssi>=15?'ok':'bad')+'">'+(d.signal.rssi>=15?'良好':'较弱')+'</span>':''}</dd>
 <dt>注册</dt><dd>${regTxt}</dd><dt>运营商</dt><dd>${esc(d.operator?.oper||'-')}</dd>
 <dt>串口</dt><dd>${esc(d.port)} <button class="btn ghost" style="font-size:12px;padding:2px 8px" onclick="editPort('${esc(d.port)}')">修改</button></dd></dl>
 <div id="port-edit" style="display:none;margin-top:8px"><div class="row">
 <input id="port-input" placeholder="/dev/ttyUSB3" style="flex:2">
 <button class="btn" onclick="savePort()">保存</button>
 <button class="btn ghost" onclick="$('port-edit').style.display='none'">取消</button></div>
 <p class="muted">应急修改串口，保存后需重启 scc-lite 服务生效</p></div>`;
 $('flight-status').innerHTML=d.flight_mode?'<span class="badge bad">飞行模式开</span>':'<span class="badge ok">正常</span>';}
function editPort(cur){$('port-input').value=cur||'/dev/ttyUSB3';$('port-edit').style.display='block';}
async function savePort(){const p=$('port-input').value.trim();if(!p)return alert('串口不能为空');
 const d=await api('/api/device/port',{method:'POST',body:JSON.stringify({port:p})});
 alert(d&&d.ok?('已保存: '+d.port+'，'+(d.note||'')):'失败: '+(d&&d.error));loadDevice();}
async function setFlight(on){const d=await api('/api/device/flight',{method:'POST',body:JSON.stringify({enable:on})});
 alert(d&&d.ok?'已执行，等待 modem 生效':'失败: '+(d&&d.error));loadDevice();}
async function loadData(fresh){const d=await api('/api/data'+(fresh?'?fresh=1':''));if(!d)return;
 const v6=(d.ipv6||[]).map(esc).join('<br>')||'-';
 const ipChg=d.ip_changed?`<div style="margin-top:8px;padding:8px;background:#fff8e1;border-radius:4px;font-size:13px">⚠️ IP 发生变化: ${esc(d.ip_change_msg||'')}<button class="btn ghost" style="margin-left:8px" onclick="clearIpChange()">知道了</button></div>`:'';
 $('data-info').innerHTML=`<dl class="kv">
 <dt>模式</dt><dd>${esc(d.mode_name||d.mode||'-')}</dd>
 <dt>状态</dt><dd>${d.connected?'<span class="badge ok">已连接</span>':'<span class="badge bad">未连接</span>'}</dd>
 <dt>IP 类型</dt><dd>${d.ip_type?('IPv'+d.ip_type):'-'}</dd>
 <dt>IPv6</dt><dd>${v6}</dd><dt>IPv4</dt><dd>${esc(d.ipv4||'-')}</dd>
 <dt>网卡</dt><dd>${esc(d.iface)}</dd></dl>${ipChg}`;
 $('btn-data-on').disabled=d.connected;$('btn-data-off').disabled=!d.connected;
 loadCarrier();loadUsbnetMode();loadImplNotes();}
async function dataOn(){$('data-info').innerHTML='连接中…(约10-20秒)';
 const d=await api('/api/data/on',{method:'POST'});
 if(d&&d.ok){await loadData();testConnectivity();}
 else{alert('失败: '+(d&&d.error));loadData();}}
async function clearIpChange(){await api('/api/data/ipchange/clear',{method:'POST'});loadData();}
// 三段式连通性: bearer -> IP -> 互联网
async function testConnectivity(){$('conn-result').innerHTML='测试中…(约10秒)';
 const d=await api('/api/data/connectivity');if(!d||d.error){$('conn-result').innerHTML='测试失败';return;}
 const b1=d.bearer?'<span class="badge ok">✅ 已建立</span>':'<span class="badge bad">❌ 未建立</span>';
 const b2=(d.has_ipv4||d.has_ipv6)?'<span class="badge ok">✅ 已获取</span>':'<span class="badge bad">❌ 未获取</span>';
 const v4txt=d.internet_v4===null?'-':(d.internet_v4?'<span class="badge ok">✅ 通</span>':'<span class="badge bad">❌ 不通</span>');
 const v6txt=d.internet_v6===null?'-':(d.internet_v6?'<span class="badge ok">✅ 通</span>':'<span class="badge bad">❌ 不通</span>');
 $('conn-result').innerHTML=`<dl class="kv">
 <dt>① Bearer</dt><dd>${b1} <span class="muted">${esc(d.bearer_detail||'')}</span></dd>
 <dt>② 获取 IP</dt><dd>${b2}</dd>
 <dt style="padding-left:16px">IPv4</dt><dd>${esc(d.ipv4||'-')}</dd>
 <dt style="padding-left:16px">IPv6</dt><dd>${(d.ipv6||[]).map(esc).join('<br>')||'-'}</dd>
 <dt>③ 访问互联网</dt><dd>IPv4: ${v4txt} &nbsp; IPv6: ${v6txt}</dd></dl>`;
 $('data-stages').innerHTML='';}
// 操作日志折叠
function toggleOpLog(){const b=$('oplog-box');b.style.display=b.style.display==='none'?'block':'none';if(b.style.display==='block')loadOpLog();}
async function loadOpLog(){const d=await api('/api/data/oplog');if(!d)return;
 $('oplog-output').textContent=(d.logs||[]).join('\n')||'(空)';}
// 修改账户
async function openAccountModal(){const d=await api('/api/account');if(d&&d.username)$('acc-username').value=d.username;
 $('acc-oldpw').value='';$('acc-newpw').value='';$('acc-newpw2').value='';$('acc-msg').textContent='';
 $('account-modal').style.display='flex';}
function closeAccountModal(){$('account-modal').style.display='none';}
async function submitAccountChange(){
 const npw=$('acc-newpw').value, npw2=$('acc-newpw2').value;
 if(npw!==npw2){$('acc-msg').textContent='两次输入的新密码不一致';return;}
 if(npw.length<4){$('acc-msg').textContent='新密码至少 4 位';return;}
 const d=await api('/api/account/change',{method:'POST',body:JSON.stringify({
  username:$('acc-username').value.trim(),
  old_password:$('acc-oldpw').value,new_password:npw})});
 if(d&&d.ok){alert(d.msg||'已保存，请重新登录');location.href='/logout';}
 else{$('acc-msg').textContent=d&&(d.error||'失败');}}
async function dataOff(){if(!confirm('关闭上网将断开 PDP（省电），确定？'))return;
 const d=await api('/api/data/off',{method:'POST'});
 alert(d&&d.ok?'已断开':'失败');loadData();}
async function pingTest(){const t=$('ping-target').value.trim()||'2400:3200::1';
 $('ping-result').innerHTML='测试中…';
 const d=await api('/api/data/ping',{method:'POST',body:JSON.stringify({target:t})});
 $('ping-result').innerHTML=d?`<dl class="kv"><dt>目标</dt><dd>${esc(t)}</dd>
 <dt>结果</dt><dd>${d.ok?'<span class="badge ok">通</span>':'<span class="badge bad">不通</span>'}</dd>
 <dt>平均时延</dt><dd>${d.avg_ms} ms</dd><dt>丢包</dt><dd>${d.loss_pct}%</dd></dl>`:'失败';}
async function loadCarrier(){const d=await api('/api/data/carrier');if(!d||!d.ok)return;
 $('carrier-info').innerHTML=`<dl class="kv">
 <dt>运营商</dt><dd>${esc(d.carrier||'-')}</dd><dt>IMSI</dt><dd>${esc(d.imsi||'-')}</dd>
 <dt>网络</dt><dd>${esc(d.operator||'-')}</dd><dt>识别APN</dt><dd>${esc(d.apn||'-')}</dd>
 <dt>MCC/MNC</dt><dd>${esc(d.mcc||'')}/${esc(d.mnc||'')}</dd>
 <dt>来源</dt><dd>${esc(d.source||'-')}</dd></dl>`;
 if(d.apn)$('apn-input').value=d.apn;}
async function setApn(){const a=$('apn-input').value.trim();if(!a){alert('APN 不能为空');return;}
 const d=await api('/api/data/apn',{method:'POST',body:JSON.stringify({apn:a})});
 alert(d&&d.ok?'APN 已下发到模块':'失败: '+(d&&d.error));}
async function loadUsbnetMode(){const d=await api('/api/data/mode');if(!d||!d.ok)return;
 $('usbnet-mode').textContent=d.name+' ('+d.mode+')';}
async function switchMode(){const d=await api('/api/data/mode');if(!d||!d.ok)return;
 const cur=d.mode;const target=cur===1?0:1;
 const tname=target===1?'ECM':'QMI';
 if(!confirm('切换到 '+tname+' 模式需要重启模块（语音/短信中断约1分钟），确定？'))return;
 if(!confirm('再次确认：真的要切换到 '+tname+' 吗？'))return;
 const r=await api('/api/data/mode',{method:'POST',body:JSON.stringify({mode:target})});
 alert(r&&r.ok?'已下发，请在 AT 终端执行 AT+CFUN=1,1 重启':'失败: '+(r&&r.error));}
const IMPL_NOTES=`【ECM 模式上网实现】(2026-10-08 N1 实测)
# 1. 模块设为 ECM 模式 (只需做一次)
AT+QCFG="usbnet",1
AT+CFUN=1,1   # 重启生效, usb0 网卡出现
# 2. 下发 APN (重启后需重下发)
AT+CGDCONT=1,"IPV4V6","cbnet"
# 3. 开启上网
AT+CGACT=1,1
ip link set usb0 up
# 等待 IPv6: ip -6 addr show usb0
# 4. 测试 (广电 IPv4 被拦截, 用 IPv6)
ping -6 -I usb0 2400:3200::1
# 5. 关闭上网 (省电)
ip link set usb0 down
AT+CGACT=0,1
# 运营商自动识别: AT+CIMI 取 IMSI -> 查内置 APN 库 (26000+ 条)`;
async function loadImplNotes(){$('impl-notes').textContent=IMPL_NOTES;}
function copyImpl(){navigator.clipboard.writeText(IMPL_NOTES).then(()=>alert('已复制'),()=>alert('复制失败'));}
async function ussdSend(){const c=$('ussd-code').value.trim();if(!c)return;
 $('ussd-result').textContent='发送中…';
 const d=await api('/api/ussd',{method:'POST',body:JSON.stringify({code:c})});
 $('ussd-result').textContent=(d&&d.ok?d.response:'❌ '+(d&&d.error||'失败'));}
async function ussdCancel(){await api('/api/ussd/cancel',{method:'POST'});$('ussd-result').textContent='已取消';}
async function atSend(){const c=$('at-cmd').value.trim();if(!c)return;
 $('at-result').textContent='执行中…';
 const d=await api('/api/at',{method:'POST',body:JSON.stringify({cmd:c})});
 $('at-result').textContent=d?(d.lines||[]).join('\n'):'失败';}
async function loadEc20(){const d=await api('/api/ec20');if(!d)return;
 let h='<h3>通用规格</h3><dl class="kv">';
 for(const [k,v] of Object.entries(d.common))h+=`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`;
 h+='</dl><h3>版本对照</h3>';
 h+='<table><tr><th>版本</th><th>地区/用途</th><th>LTE FDD</th><th>LTE TDD</th><th>WCDMA</th><th>TD-SCDMA</th><th>CDMA</th><th>GSM</th></tr>';
 for(const [v,i] of Object.entries(d.variants))h+=`<tr><td><b>${esc(v)}</b></td><td>${esc(i.region)}<br><span style="color:var(--muted)">${esc(i.use)}</span></td><td>${esc(i.lte_fdd)}</td><td>${esc(i.lte_tdd)}</td><td>${esc(i.wcdma)}</td><td>${esc(i.tdscdma)}</td><td>${esc(i.cdma)}</td><td>${esc(i.gsm)}</td></tr>`;
 h+='</table>';
 h+=`<p style="font-size:12px;color:var(--muted);margin-top:8px">${esc(d.firmware_note)}</p>`;
 h+=`<p style="font-size:13px;margin-top:6px"><b>典型应用：</b>${d.applications.map(esc).join('、')}</p>`;
 $('ec20-info').innerHTML=h;}
let atdocCat='';
async function loadAtdoc(cat){if(cat!==undefined)atdocCat=cat;
 const q=$('atdoc-q').value.trim();
 const d=await api('/api/atdoc?q='+encodeURIComponent(q)+'&cat='+encodeURIComponent(atdocCat));if(!d)return;
 $('atdoc-cats').innerHTML='<button class="btn ghost" onclick="loadAtdoc(\'\')">全部</button> '+
  Object.entries(d.categories).map(([k,v])=>`<button class="btn ${atdocCat===k?'':'ghost'}" onclick="loadAtdoc('${k}')">${esc(v)}</button>`).join(' ');
 $('atdoc-list').innerHTML=d.commands.length?'<table><tr><th>指令</th><th>说明</th><th>出处</th><th>示例</th></tr>'+
  d.commands.map(c=>`<tr><td><code>${esc(c.cmd)}</code></td><td>${esc(c.desc)}</td><td><span class="badge info">${esc(c.src)}</span></td><td><code>${esc(c.example)}</code></td></tr>`).join('')+'</table>':'无匹配';}
let logService='scc-lite',logTimer=null;
async function loadLogs(){
 if(logService==='qmi'){const d=await api('/api/data/oplog');if(!d)return;
  $('log-service-label').textContent='qmi';
  $('log-output').textContent=(d.logs||[]).join('\n')||'(无日志)';
  $('log-output').scrollTop=$('log-output').scrollHeight;return;}
 const d=await api('/api/logs?service='+encodeURIComponent(logService));if(!d)return;
 $('log-service-label').textContent=logService;
 $('log-output').textContent=d.logs||'(无日志)';
 $('log-output').scrollTop=$('log-output').scrollHeight;}
function toggleLogAuto(){if($('log-auto').checked){logTimer=setInterval(loadLogs,5000);}else{clearInterval(logTimer);logTimer=null;}}
document.querySelectorAll('#tabs button').forEach(b=>b.addEventListener('click',()=>{
 if(b.dataset.t==='logs'&&$('log-auto').checked&&!logTimer)logTimer=setInterval(loadLogs,5000);
 if(b.dataset.t!=='logs'&&logTimer){clearInterval(logTimer);logTimer=null;}
}));
let notifyCfg={};
async function loadNotify(){const d=await api('/api/notify');if(!d)return;notifyCfg=d;
 const f=(n,title,fields)=>`<h3>${title}
 <input type="checkbox" data-n="${n}" data-k="enabled" ${d[n]?.enabled?'checked':''}> 启用</h3>`+
 fields.map(([k,label,ph])=>`<div class="row"><label style="width:110px">${label}</label>
 <input data-n="${n}" data-k="${k}" value="${esc(d[n]?.[k]??'')}" placeholder="${ph||''}"></div>`).join('');
 $('notify-form').innerHTML=
 f('qq','QQ Bot',[['app_id','AppID',''],['app_secret','AppSecret',''],['openid','接收 OpenID','']])+
 `<div class="row"><button class="btn ghost" onclick="loadQQOpenids()">🔍 查看抓到的 OpenID</button></div><div id="qq-openids"></div>`+
 f('telegram','Telegram',[['bot_token','Bot Token',''],['chat_id','Chat ID','']])+
 f('webhook','Webhook',[['url','URL','https://…'],['secret','Secret(可选)','']])+
 f('bark','Bark',[['key','Key',''],['server','服务器','https://api.day.app']]);}
async function saveNotify(){document.querySelectorAll('#notify-form input').forEach(i=>{
 const n=i.dataset.n,k=i.dataset.k;notifyCfg[n]=notifyCfg[n]||{};
 notifyCfg[n][k]=i.type==='checkbox'?i.checked:i.value;});
 const d=await api('/api/notify',{method:'POST',body:JSON.stringify(notifyCfg)});
 $('notify-status').textContent=d&&d.ok?'✅ 已保存':'❌ 失败';}
async function loadQQOpenids(){const d=await api('/api/qq/openids');
 if(!d||!d.ok){$('qq-openids').innerHTML='❌ '+(d&&d.error||'获取失败');return;}
 const o=d.openids;let h='';
 const c2c=Object.entries(o.c2c||{}),grp=Object.entries(o.group||{});
 if(c2c.length){h+='<h3>私聊 OpenID（点复制）</h3>';
  h+=c2c.map(([id,v])=>`<div class="row"><code style="flex:1">${esc(id)}</code>
   <span style="font-size:12px;color:var(--muted)">${esc(v.name||'')} ${esc(v.last_seen||'')}</span>
   <button class="btn ghost" onclick="fillQQOpenid('${esc(id)}')">填入</button></div>`).join('');}
 if(grp.length){h+='<h3>群成员 OpenID</h3>';
  h+=grp.map(([id,v])=>`<div class="row"><code style="flex:1">${esc(id)}</code>
   <span style="font-size:12px;color:var(--muted)">${esc(v.name||'')}</span>
   <button class="btn ghost" onclick="fillQQOpenid('${esc(id)}')">填入</button></div>`).join('');}
 $('qq-openids').innerHTML=h||'<p style="color:var(--muted)">暂无。用 QQ 给机器人发条消息后点刷新。</p>';}
function fillQQOpenid(id){const inp=document.querySelector('#notify-form input[data-n="qq"][data-k="openid"]');
 if(inp){inp.value=id;$('notify-status').textContent='已填入，点保存生效';}}
loadDash();
</script></body></html>
"""


@app.route("/")
@requires_auth
def index():
    return render_template_string(PAGE, version=VERSION)


@app.route("/logout")
def logout():
    # Basic Auth 无标准登出: 用 XHR 带错误账密请求一次, 把浏览器缓存的旧账密顶掉
    # (XHR 的 401 不会弹框), 再跳回主页, 由主页的 401 自然弹出登录框.
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>已退出 - SCC-lite</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2>已退出登录</h2><p>正在跳转到登录页…</p>
<p><a href="/">如果没有自动跳转, 点这里</a></p>
<script>
function go(){location.href='/';}
try{
 var xhr=new XMLHttpRequest();
 xhr.open('GET','/api/account',true,'logout','logout');
 xhr.onload=go; xhr.onerror=go; xhr.ontimeout=go;
 xhr.timeout=3000; xhr.send();
 setTimeout(go,3500);
}catch(e){go();}
</script></body></html>"""
    return Response(html, 200, {"Content-Type": "text/html; charset=utf-8"})


# ----------------------------------------------------------------------
# SMS API
# ----------------------------------------------------------------------
@app.route("/api/sms")
@requires_auth
def api_sms():
    db = get_store()
    rows = db.execute(
        "SELECT * FROM sms ORDER BY id DESC LIMIT 100").fetchall()
    db.close()
    msgs = []
    for r in rows:
        d = dict(r)
        # Convert UTC created_at to local (Asia/Shanghai) for display
        # sms_time from modem is already local, leave it as-is
        if d.get("created_at"):
            try:
                from datetime import datetime, timezone, timedelta
                # Parse "2026-10-08 07:26:31" as UTC
                dt = datetime.strptime(d["created_at"][:19], "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(timezone(timedelta(hours=8)))
                d["created_at"] = local.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass
        msgs.append(d)
    return jsonify({"messages": msgs})


@app.route("/api/sms/send", methods=["POST"])
@requires_auth
def api_sms_send():
    data = request.get_json(force=True)
    to, text = data.get("to", "").strip(), data.get("text", "")
    if not to or not text:
        return jsonify({"ok": False, "error": "号码和内容必填"})
    try:
        m = get_modem()
        try:
            mr = m.sms_send(to, text)
        finally:
            m.close()
        # Save to DB
        db = get_store()
        db.execute(
            "INSERT INTO sms (direction,sender,body,forwarded)"
            " VALUES ('out',?,?,1)", (to, text))
        db.commit()
        db.close()
        return jsonify({"ok": True, "mr": mr})
    except ModemError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        log.exception("sms send")
        return jsonify({"ok": False, "error": str(e)})


# ----------------------------------------------------------------------
# Device API
# ----------------------------------------------------------------------
def _scan_device():
    """实时扫描设备信息 (AT 指令, 慢, 供缓存系统调用)."""
    info = {"port": config.get("modem", {}).get("port", "")}
    try:
        m = get_modem()
        try:
            for fn, key in (("get_imei", "imei"), ("get_imsi", "imsi"),
                            ("get_iccid", "iccid"),
                            ("get_signal", "signal"),
                            ("get_registration", "reg"),
                            ("get_operator", "operator"),
                            ("get_flight_mode", "flight_mode")):
                try:
                    info[key] = getattr(m, fn)()
                except ModemError as e:
                    info[key] = f"error: {e}"
        finally:
            m.close()
    except ModemError as e:
        info["error"] = str(e)
    return info


@app.route("/api/device")
@requires_auth
def api_device():
    # v0.5.6: ?fresh=1 强制实时扫描, 否则返回缓存秒回
    if request.args.get("fresh") == "1":
        data = _scan_device()
        _cache_set("device", data)
        return jsonify(data)
    data = _cache_get("device")
    if data is None:
        data = _scan_device()
        _cache_set("device", data)
    return jsonify(data)


@app.route("/api/device/flight", methods=["POST"])
@requires_auth
def api_flight():
    enable = request.get_json(force=True).get("enable", False)
    try:
        m = get_modem()
        try:
            m.set_flight_mode(enable)
        finally:
            m.close()
        return jsonify({"ok": True})
    except ModemError as e:
        return jsonify({"ok": False, "error": str(e)})


# ----------------------------------------------------------------------
# Data API
# ----------------------------------------------------------------------
def _scan_data():
    """实时扫描数据状态 (QMI, 供缓存系统调用)."""
    return get_data_ctl().get_status()


@app.route("/api/data")
@requires_auth
def api_data():
    # v0.5.6: ?fresh=1 强制实时扫描, 否则返回缓存秒回
    if request.args.get("fresh") == "1":
        data = _scan_data()
        _cache_set("data", data)
        return jsonify(data)
    data = _cache_get("data")
    if data is None:
        data = _scan_data()
        _cache_set("data", data)
    return jsonify(data)


@app.route("/api/data/on", methods=["POST"])
@requires_auth
def api_data_on():
    ctl = get_data_ctl()
    ok = ctl.start()
    d = ctl.get_status()
    err = ""
    if not ok:
        err = "连接失败"
        if getattr(ctl, "mode", "ecm") == "qmi":
            import shutil
            if not shutil.which("qmicli"):
                err = "未安装 qmicli (apt install libqmi-utils)"
    elif not d.get("connected"):
        err = "已执行但未连接，检查 APN/信号"
    elif not d.get("ip") and not d.get("ipv6"):
        err = "已连接但未获取 IP"
    return jsonify({"ok": ok and d.get("connected"), "error": err, **d})


@app.route("/api/data/off", methods=["POST"])
@requires_auth
def api_data_off():
    ok = get_data_ctl().stop()
    return jsonify({"ok": ok})


@app.route("/api/data/ping", methods=["POST"])
@requires_auth
def api_data_ping():
    # v0.5.5: QMI 自动选 IP 类型, ping 目标默认按当前类型
    # 用户可在页面输入框改目标
    ctl = get_data_ctl()
    st = ctl.get_status()
    default = "114.114.114.114" if st.get("ip_type") == 4 else "2400:3200::1"
    target = request.get_json(force=True).get("target", default)
    return jsonify(ctl.ping_test(target, count=3))


@app.route("/api/data/oplog")
@requires_auth
def api_data_oplog():
    """QMI 操作日志 (拨号全过程, 供分析)."""
    ctl = get_data_ctl()
    fn = getattr(ctl, "get_op_log", None)
    logs = fn(100) if fn else []
    return jsonify({"logs": logs})


@app.route("/api/data/ipchange/clear", methods=["POST"])
@requires_auth
def api_data_ipchange_clear():
    """清除 IP 变化提醒标记 (用户已读)."""
    ctl = get_data_ctl()
    fn = getattr(ctl, "clear_ip_change_flag", None)
    if fn:
        fn()
    return jsonify({"ok": True})


@app.route("/api/data/connectivity")
@requires_auth
def api_data_connectivity():
    """三段式连通性检查: bearer -> IP -> 互联网 (v4/v6 分开)."""
    ctl = get_data_ctl()
    fn = getattr(ctl, "check_internet", None)
    if fn:
        return jsonify(fn())
    return jsonify({"error": "not supported"})


@app.route("/api/device/port", methods=["POST"])
@requires_auth
def api_device_port():
    """
    修改 modem 串口 (v0.5.5, 应急用).
    写回 config.yaml 的 modem.port, 需重启 scc-lite 生效.
    """
    import re
    data = request.get_json(force=True) or {}
    port = (data.get("port") or "").strip()
    if not re.fullmatch(r"/dev/[A-Za-z0-9_.-]+", port):
        return jsonify({"ok": False, "error": "串口格式非法"})
    if "modem" not in config:
        config["modem"] = {}
    config["modem"]["port"] = port
    try:
        save_config()
    except Exception as e:
        return jsonify({"ok": False, "error": f"保存失败: {e}"})
    return jsonify({"ok": True, "port": port,
                    "note": "需重启 scc-lite 服务生效"})


@app.route("/api/account", methods=["GET"])
@requires_auth
def api_account_info():
    """当前登录账户名 (不返回密码)."""
    w = config.get("web", {})
    return jsonify({"username": w.get("username", "admin")})


@app.route("/api/system")
@requires_auth
def _scan_system():
    """
    本机系统信息 (v0.5.5/v0.5.6 仪表盘用):
    CPU/内存/磁盘/网卡/IP/DNS/网关/运行时间/负载.
    """
    import shutil
    info = {}
    # ip 命令绝对路径 (systemd 的 PATH 可能没有 /usr/sbin)
    IP_BIN = "/usr/sbin/ip"
    import os as _os
    if not _os.path.exists(IP_BIN):
        IP_BIN = "/sbin/ip" if _os.path.exists("/sbin/ip") else "ip"
    # CPU (兼容 ARM: 无 model name, 用 Hardware/Processor/Model)
    try:
        with open("/proc/cpuinfo") as f:
            txt = f.read()
        models = re.findall(r"model name\s*:\s*(.+)", txt)
        if models:
            info["cpu_model"] = models[0].strip()
            info["cpu_cores"] = len(models)
        else:
            # ARM 格式
            m = re.search(r"Hardware\s*:\s*(.+)", txt)
            if not m:
                m = re.search(r"^Model\s*:\s*(.+)", txt, re.M)
            info["cpu_model"] = m.group(1).strip() if m else "ARM"
            procs = re.findall(r"^processor\s*:", txt, re.M)
            info["cpu_cores"] = len(procs) or (os.cpu_count() or 1)
    except Exception:
        info["cpu_model"] = "未知"
        info["cpu_cores"] = os.cpu_count() or 1
    # CPU 占用 (两次采样)
    try:
        def _cpu_times():
            with open("/proc/stat") as f:
                p = f.readline().split()
            vals = list(map(int, p[1:8]))
            return sum(vals), vals[3]  # total, idle
        t1, i1 = _cpu_times()
        time.sleep(0.5)
        t2, i2 = _cpu_times()
        info["cpu_usage"] = round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)
    except Exception:
        info["cpu_usage"] = 0
    # 内存
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                m = re.match(r"(\w+):\s+(\d+)", line)
                if m:
                    mem[m.group(1)] = int(m.group(2))
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        info["mem_total_mb"] = total // 1024
        info["mem_used_mb"] = (total - avail) // 1024
        info["mem_usage"] = round(100 * (total - avail) / max(total, 1), 1)
    except Exception:
        info["mem_total_mb"] = info["mem_used_mb"] = 0
        info["mem_usage"] = 0
    # 磁盘 (根分区)
    try:
        du = shutil.disk_usage("/")
        info["disk_total_gb"] = round(du.total / 1e9, 1)
        info["disk_used_gb"] = round(du.used / 1e9, 1)
        info["disk_usage"] = round(100 * du.used / max(du.total, 1), 1)
    except Exception:
        info["disk_total_gb"] = info["disk_used_gb"] = 0
        info["disk_usage"] = 0
    # 运行时间 / 负载
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        d, rem = divmod(int(up), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        info["uptime"] = f"{d}天{h}时{m}分" if d else f"{h}时{m}分"
    except Exception:
        info["uptime"] = "-"
    try:
        with open("/proc/loadavg") as f:
            info["loadavg"] = f.read().split()[:3]
    except Exception:
        info["loadavg"] = []
    # 网关 + 默认网卡 (先取, 供默认显示用)
    info["gateway"] = ""
    info["default_iface"] = ""
    try:
        rc, out, _ = _run_shell([IP_BIN, "route", "show", "default"], timeout=10)
        if rc == 0:
            m = re.search(r"default via ([\d.:a-fA-F]+)\s+dev\s+(\S+)", out)
            if m:
                info["gateway"] = m.group(1)
                info["default_iface"] = m.group(2)
    except Exception:
        pass
    # 网卡详情 (含 MAC、流量, 供展开显示)
    info["interfaces"] = []
    try:
        rc, out, _ = _run_shell([IP_BIN, "-o", "addr", "show"], timeout=10)
        ifaces = {}
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/\d+", line)
                if m:
                    name = m.group(1)
                    ifaces.setdefault(name, {"name": name, "ipv4": [], "ipv6": []})
                    if name != "lo":
                        ifaces[name]["ipv4"].append(m.group(2))
                m = re.match(r"\d+:\s+(\S+)\s+inet6\s+([0-9a-fA-F:]+)/\d+\s+scope global", line)
                if m:
                    name = m.group(1)
                    ifaces.setdefault(name, {"name": name, "ipv4": [], "ipv6": []})
                    ifaces[name]["ipv6"].append(m.group(2))
        # MAC + 流量
        rc2, out2, _ = _run_shell([IP_BIN, "-s", "link", "show"], timeout=10)
        stats = {}
        if rc2 == 0:
            cur = None
            for line in out2.splitlines():
                m = re.match(r"\d+:\s+(\S+):", line)
                if m:
                    cur = m.group(1).rstrip(":")
                    stats[cur] = {}
                    continue
                m = re.search(r"link/\S+\s+([0-9a-f:]+)", line)
                if m and cur:
                    stats[cur]["mac"] = m.group(1)
                m = re.match(r"\s+RX:\s+bytes\s+packets", line)
                if m and cur:
                    stats[cur]["_rx_next"] = True
                    continue
                if cur and stats[cur].pop("_rx_next", False):
                    p = line.split()
                    if len(p) >= 2:
                        stats[cur]["rx_bytes"] = int(p[0])
                        stats[cur]["rx_packets"] = int(p[1])
                m = re.match(r"\s+TX:\s+bytes\s+packets", line)
                if m and cur:
                    stats[cur]["_tx_next"] = True
                    continue
                if cur and stats[cur].pop("_tx_next", False):
                    p = line.split()
                    if len(p) >= 2:
                        stats[cur]["tx_bytes"] = int(p[0])
                        stats[cur]["tx_packets"] = int(p[1])
        for name, idata in ifaces.items():
            s = stats.get(name, {})
            # 流量 human readable
            def _hr(b):
                b = b or 0
                for u in ["B", "KB", "MB", "GB"]:
                    if b < 1024:
                        return f"{b:.1f}{u}"
                    b /= 1024
                return f"{b:.1f}TB"
            info["interfaces"].append({
                "name": name,
                "ipv4": idata["ipv4"],
                "ipv6": idata["ipv6"],
                "mac": s.get("mac", "-"),
                "rx": _hr(s.get("rx_bytes")),
                "tx": _hr(s.get("tx_bytes")),
                "is_default": name == info["default_iface"],
            })
    except Exception:
        pass
    # DNS (兼容 systemd-resolved stub)
    info["dns"] = []
    for _p in ["/etc/resolv.conf", "/run/systemd/resolve/resolv.conf"]:
        try:
            with open(_p) as f:
                ns = re.findall(r"nameserver\s+(\S+)", f.read())
                # 过滤 stub (127.0.0.53)
                ns = [x for x in ns if not x.startswith("127.")]
                if ns:
                    info["dns"] = ns[:3]
                    break
        except Exception:
            continue
    return jsonify(info)



@app.route("/api/system")
@requires_auth
def api_system():
    """本机系统信息 (缓存版, ?fresh=1 强制实时)."""
    if request.args.get("fresh") == "1":
        data = _scan_system()
        _cache_set("system", data)
        return jsonify(data)
    data = _cache_get("system")
    if data is None:
        data = _scan_system()
        _cache_set("system", data)
    return jsonify(data)

def _run_shell(cmd, timeout=10):
    """供 api_system 用的 shell 执行."""
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


# ----------------------------------------------------------------------
# Samba 共享管理 (v0.5.6): 状态 / 创建共享 / 密码同步
# 共享定义写 /etc/samba/scc-lite-shares.conf (include 方式, 不污染 smb.conf)
# 账密与 Web 登录同一套, 创建共享时自动同步
# ----------------------------------------------------------------------
SAMBA_SHARES_CONF = "/etc/samba/scc-lite-shares.conf"
SAMBA_SMB_CONF = "/etc/samba/smb.conf"


def _samba_installed():
    import shutil
    return bool(shutil.which("smbd") or shutil.which("samba"))


def _samba_running():
    rc, out, _ = _run_shell(["systemctl", "is-active", "smbd"], timeout=10)
    if out.strip() == "active":
        return True
    rc, out, _ = _run_shell(["systemctl", "is-active", "samba"], timeout=10)
    return out.strip() == "active"


def _samba_ensure_include():
    """确保 smb.conf 包含我们的共享文件."""
    try:
        with open(SAMBA_SMB_CONF) as f:
            content = f.read()
    except Exception:
        return False
    inc = f"include = {SAMBA_SHARES_CONF}"
    if SAMBA_SHARES_CONF in content:
        return True
    try:
        with open(SAMBA_SMB_CONF, "a") as f:
            f.write(f"\n# SCC-lite USB shares (v0.5.6)\n{inc}\n")
        return True
    except Exception:
        return False


def _samba_read_shares():
    """解析我们的共享文件, 返回 {share_name: path}."""
    shares = {}
    try:
        with open(SAMBA_SHARES_CONF) as f:
            content = f.read()
    except Exception:
        return shares
    import re
    cur = None
    for line in content.splitlines():
        m = re.match(r"\[(.+)\]", line.strip())
        if m:
            cur = m.group(1)
            shares[cur] = ""
        elif cur and line.strip().lower().startswith("path"):
            pm = re.match(r"path\s*=\s*(.+)", line.strip(), re.I)
            if pm:
                shares[cur] = pm.group(1).strip()
    return shares


def _samba_write_shares(shares):
    """重写共享文件. shares: {name: (path, user)}"""
    lines = ["# SCC-lite USB shares (v0.5.6, 自动生成, 请勿手改)\n"]
    for name, (path, user) in shares.items():
        lines.append(f"[{name}]\n")
        lines.append(f"    path = {path}\n")
        lines.append("    browseable = yes\n")
        lines.append("    read only = no\n")
        lines.append(f"    valid users = {user}\n")
        lines.append("    create mask = 0644\n")
        lines.append("    directory mask = 0755\n")
        lines.append("\n")
    with open(SAMBA_SHARES_CONF, "w") as f:
        f.writelines(lines)


def _samba_reload():
    _run_shell(["systemctl", "reload", "smbd"], timeout=15)
    _run_shell(["systemctl", "reload", "samba"], timeout=15)


def _samba_remove_share_for_path(mp):
    """按挂载点删除共享 (卸载 U 盘时调用)."""
    shares = _samba_read_shares()
    # 需要 user 信息, 从现有文件解析太麻烦, 直接按 path 匹配删除整段
    import re
    try:
        with open(SAMBA_SHARES_CONF) as f:
            content = f.read()
    except Exception:
        return
    # 按 [name] 段切分, 删掉 path 匹配的段
    parts = re.split(r"(?m)^\[(.+)\]\s*$", content)
    # parts[0] 是头部注释, 之后每两项一组 (name, body)
    out = [parts[0]]
    for i in range(1, len(parts), 2):
        name = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.search(r"(?m)^\s*path\s*=\s*(.+)\s*$", body)
        if m and m.group(1).strip() == mp:
            continue  # 删掉这个段
        out.append(f"[{name}]\n")
        out.append(body)
    with open(SAMBA_SHARES_CONF, "w") as f:
        f.write("".join(out))
    _samba_reload()


def _samba_sync_password(username, password):
    """把 Web 账密同步到 Samba (smbpasswd). 返回 (ok, msg)."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return False, "用户名非法"
    if not _samba_installed():
        return False, "Samba 未安装"
    # -a 添加用户 (已存在则更新密码), -s 静默从 stdin 读
    import subprocess
    try:
        p = subprocess.run(
            ["smbpasswd", "-a", "-s", username],
            input=f"{password}\n{password}\n",
            capture_output=True, text=True, timeout=15)
        if p.returncode != 0:
            return False, p.stderr.strip()[:200] or "smbpasswd 失败"
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


@app.route("/api/system/samba/status")
@requires_auth
def api_samba_status():
    """Samba 状态: 装没装 / 跑没跑 / 共享列表."""
    shares = _samba_read_shares()
    return jsonify({
        "installed": _samba_installed(),
        "running": _samba_running(),
        "shares": [{"name": n, "path": p} for n, p in shares.items()],
    })


@app.route("/api/system/samba/share", methods=["POST"])
@requires_auth
def api_samba_share():
    """
    为已挂载的 USB 创建 Samba 共享.
    参数: {"path": "/mnt/usb-BACKUP"}
    共享名=卷标 (无卷标则 usbshare), 账密=Web 登录账密.
    """
    import re
    import os
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not re.fullmatch(r"/mnt/usb-[A-Za-z0-9_-]+", path):
        return jsonify({"ok": False, "error": "路径非法"})
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": "目录不存在"})
    if not _samba_installed():
        return jsonify({"ok": False, "error": "Samba 未安装, 请先 sudo bash install.sh"})
    # 共享名: 卷标, 无卷标用 usbshare (重名则加序号)
    base = path.replace("/mnt/usb-", "")
    name = re.sub(r"[^A-Za-z0-9_-]", "_", base) or "usbshare"
    shares = _samba_read_shares()
    if name in shares:
        i = 2
        while f"{name}{i}" in shares:
            i += 1
        name = f"{name}{i}"
    # Web 账密
    w = config.get("web", {})
    user = w.get("username", "admin")
    pw = w.get("password", "admin")
    # 同步 Samba 密码
    ok, msg = _samba_sync_password(user, pw)
    if not ok:
        return jsonify({"ok": False, "error": f"Samba 密码同步失败: {msg}"})
    # 写共享
    _samba_ensure_include()
    # 读出现有 (带 user), 重新构造
    full = {}
    for n, p in shares.items():
        full[n] = (p, user)  # user 统一用当前 Web 用户
    full[name] = (path, user)
    try:
        _samba_write_shares(full)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写共享配置失败: {e}"})
    _samba_reload()
    # 本机 IP (给指引用)
    rc, out, _ = _run_shell(["hostname", "-I"], timeout=10)
    ip = out.strip().split()[0] if out.strip() else "<N1-IP>"
    return jsonify({
        "ok": True,
        "share": name,
        "path": path,
        "user": user,
        "hint": f"请用 Web 登录的账密访问: \\\\{ip}\\{name}",
    })
# ----------------------------------------------------------------------
# USB 存储管理 (v0.5.6): 列设备 / 挂载 / 卸载
# 安全: 只操作 TRAN=usb 的设备, 设备路径严格校验, 不碰系统盘
# ----------------------------------------------------------------------
def _usb_list():
    """列出 USB 存储设备 (lsblk JSON)."""
    import json
    import re
    rc, out, _ = _run_shell(
        ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,LABEL,FSTYPE,TRAN"],
        timeout=10)
    devs = []
    if rc != 0:
        return devs
    try:
        data = json.loads(out)
    except Exception:
        return devs
    for blk in data.get("blockdevices", []):
        # 只收 USB 设备 (整盘或分区)
        is_usb = (blk.get("tran") == "usb")
        children = []
        for ch in blk.get("children", []):
            if ch.get("tran") == "usb" or is_usb:
                children.append(ch)
        targets = [blk] if is_usb and not children else children
        for t in targets:
            if t.get("type") not in ("part", "disk"):
                continue
            name = t.get("name", "")
            # 严格校验设备名, 防路径穿越
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                continue
            devs.append({
                "dev": f"/dev/{name}",
                "name": name,
                "size": t.get("size", ""),
                "type": t.get("type", ""),
                "label": t.get("label") or "",
                "fstype": t.get("fstype") or "",
                "mountpoint": t.get("mountpoint") or "",
            })
    return devs


def _usb_mountpoint(label, dev):
    """挂载点: /mnt/usb-<卷标>, 无卷标用设备名."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", label) if label else ""
    if not safe:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", dev.replace("/dev/", ""))
    return f"/mnt/usb-{safe}"


@app.route("/api/system/usb")
@requires_auth
def api_usb_list():
    """USB 存储设备列表."""
    return jsonify({"devices": _usb_list()})


@app.route("/api/system/usb/mount", methods=["POST"])
@requires_auth
def api_usb_mount():
    """挂载 USB 设备. 参数: {"dev": "/dev/sda1"}"""
    import os
    import re
    data = request.get_json(force=True, silent=True) or {}
    dev = (data.get("dev") or "").strip()
    # 严格校验: 只允许 /dev/sdX /dev/nvmeXnYpZ 等块设备名
    if not re.fullmatch(r"/dev/[a-zA-Z0-9_-]+", dev):
        return jsonify({"ok": False, "error": "设备路径非法"})
    # 确认是 USB 设备
    found = [d for d in _usb_list() if d["dev"] == dev]
    if not found:
        return jsonify({"ok": False, "error": "非 USB 存储设备, 拒绝挂载"})
    info = found[0]
    if info["mountpoint"]:
        return jsonify({"ok": True, "mountpoint": info["mountpoint"],
                        "msg": "已挂载"})
    mp = _usb_mountpoint(info["label"], dev)
    os.makedirs(mp, exist_ok=True)
    rc, _, err = _run_shell(["mount", dev, mp], timeout=30)
    if rc != 0:
        return jsonify({"ok": False, "error": f"挂载失败: {err.strip()[:200]}"})
    return jsonify({"ok": True, "mountpoint": mp})


@app.route("/api/system/usb/umount", methods=["POST"])
@requires_auth
def api_usb_umount():
    """卸载 USB 设备, 同时删除其 Samba 共享. 参数: {"dev": "/dev/sda1"}"""
    import re
    data = request.get_json(force=True, silent=True) or {}
    dev = (data.get("dev") or "").strip()
    if not re.fullmatch(r"/dev/[a-zA-Z0-9_-]+", dev):
        return jsonify({"ok": False, "error": "设备路径非法"})
    found = [d for d in _usb_list() if d["dev"] == dev]
    mp = found[0]["mountpoint"] if found else ""
    if not mp:
        return jsonify({"ok": False, "error": "设备未挂载"})
    # 先删 Samba 共享 (如果有)
    try:
        _samba_remove_share_for_path(mp)
    except Exception:
        pass
    rc, _, err = _run_shell(["umount", mp], timeout=30)
    if rc != 0:
        return jsonify({"ok": False, "error": f"卸载失败: {err.strip()[:200]}"})
    return jsonify({"ok": True})


@app.route("/api/account/change", methods=["POST"])
@requires_auth
def api_account_change():
    """
    修改 Web 登录账户/密码 (v0.5.5, v0.5.6 增强).
    需验证旧密码. 成功后写回 config.yaml, 同步 Samba 密码, 前端强制登出重登.
    """
    data = request.get_json(force=True) or {}
    old_pw = data.get("old_password", "")
    new_user = (data.get("username") or "").strip()
    new_pw = data.get("new_password", "")
    w = config.get("web", {})
    cur_user = w.get("username", "admin")
    cur_pw = w.get("password", "admin")
    # 验证旧密码
    if old_pw != cur_pw:
        return jsonify({"ok": False, "error": "旧密码不正确"})
    if not new_user:
        return jsonify({"ok": False, "error": "用户名不能为空"})
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "新密码至少 4 位"})
    # 写回配置
    if "web" not in config:
        config["web"] = {}
    config["web"]["username"] = new_user
    config["web"]["password"] = new_pw
    try:
        save_config()
    except Exception as e:
        return jsonify({"ok": False, "error": f"保存失败: {e}"})
    # v0.5.6: 同步 Samba 密码 (用户名变了则删旧建新)
    samba_msg = ""
    if _samba_installed():
        try:
            if new_user != cur_user:
                # 删旧 Samba 用户
                _run_shell(["smbpasswd", "-x", cur_user], timeout=15)
            ok, msg = _samba_sync_password(new_user, new_pw)
            if not ok:
                samba_msg = f" (Samba 密码同步失败: {msg})"
            else:
                # 用户名变了, 更新共享文件中的 valid users
                shares = _samba_read_shares()
                if shares:
                    full = {n: (p, new_user) for n, p in shares.items()}
                    try:
                        _samba_write_shares(full)
                        _samba_reload()
                    except Exception:
                        pass
        except Exception as e:
            samba_msg = f" (Samba 同步异常: {e})"
    # 清 API 缓存 (账密变了, 旧缓存无意义)
    _cache_invalidate()
    return jsonify({"ok": True, "samba_msg": samba_msg,
                    "msg": f"已保存{samba_msg}, 请重新登录"})


@app.route("/api/data/carrier")
@requires_auth
def api_data_carrier():
    """运营商信息 + APN (自动识别)."""
    try:
        m = _get_modem_for_data()
        try:
            info = get_carrier_info(m)
        finally:
            m.close()
        # 当前模块内已下发的 APN
        return jsonify({"ok": True, **info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/data/apn", methods=["POST"])
@requires_auth
def api_data_apn():
    """手动设置 APN 并下发到模块. body: {"apn": "cbnet"}"""
    apn = (request.get_json(force=True).get("apn") or "").strip()
    if not apn:
        return jsonify({"ok": False, "error": "APN 不能为空"})
    try:
        m = _get_modem_for_data()
        try:
            ok = provision_apn(m, apn)
        finally:
            m.close()
        if ok:
            # 同步写回 config (内存 + 文件由调用方持久化)
            config.setdefault("data", {})["apn"] = apn
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/data/mode")
@requires_auth
def api_data_mode():
    """查询当前 usbnet 模式."""
    try:
        m = _get_modem_for_data()
        try:
            mode, name = get_usbnet_mode(m)
        finally:
            m.close()
        return jsonify({"ok": True, "mode": mode, "name": name,
                        "modes": USBNET_MODES})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/data/mode", methods=["POST"])
@requires_auth
def api_data_mode_set():
    """
    切换 usbnet 模式. body: {"mode": 1}
    注意: 需重启模块生效, 前端必须二次确认.
    这里只下发指令, 不自动重启 (返回 need_reboot=True).
    """
    try:
        mode = int(request.get_json(force=True).get("mode"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "mode 必须是 0-3 的整数"})
    if mode not in USBNET_MODES:
        return jsonify({"ok": False, "error": "mode 越界"})
    try:
        m = _get_modem_for_data()
        try:
            ok = set_usbnet_mode(m, mode)
        finally:
            m.close()
        return jsonify({"ok": ok, "need_reboot": True,
                        "message": "已下发，需 AT+CFUN=1,1 重启生效"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ----------------------------------------------------------------------
# USSD / AT API
# ----------------------------------------------------------------------
@app.route("/api/ussd", methods=["POST"])
@requires_auth
def api_ussd():
    code = request.get_json(force=True).get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "USSD 码必填"})
    try:
        m = get_modem()
        try:
            resp = m.ussd_send(code)
        finally:
            m.close()
        return jsonify({"ok": True, "response": resp or "(无返回)"})
    except ModemError as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/ussd/cancel", methods=["POST"])
@requires_auth
def api_ussd_cancel():
    try:
        m = get_modem()
        try:
            m.ussd_cancel()
        finally:
            m.close()
        return jsonify({"ok": True})
    except ModemError as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/at", methods=["POST"])
@requires_auth
def api_at():
    cmd = request.get_json(force=True).get("cmd", "").strip()
    if not cmd.upper().startswith("AT"):
        return jsonify({"ok": False, "error": "必须以 AT 开头"})
    try:
        m = get_modem()
        try:
            lines, ok = m.raw(cmd)
        finally:
            m.close()
        return jsonify({"ok": ok, "lines": lines})
    except ModemError as e:
        return jsonify({"ok": False, "error": str(e)})


# ----------------------------------------------------------------------
# Notify settings API
# ----------------------------------------------------------------------
@app.route("/api/notify")
@requires_auth
def api_notify_get():
    return jsonify(config.get("notifications", {}))


@app.route("/api/notify", methods=["POST"])
@requires_auth
def api_notify_set():
    data = request.get_json(force=True)
    # Only allow known channels/keys (prevent config injection)
    allowed = {
        "qq": ("enabled", "app_id", "app_secret", "openid"),
        "telegram": ("enabled", "bot_token", "chat_id"),
        "webhook": ("enabled", "url", "headers", "secret"),
        "bark": ("enabled", "key", "server"),
    }
    notif = config.setdefault("notifications", {})
    for ch, keys in allowed.items():
        if ch in data and isinstance(data[ch], dict):
            cfg = notif.setdefault(ch, {})
            for k in keys:
                if k in data[ch]:
                    cfg[k] = data[ch][k]
    save_config()
    return jsonify({"ok": True})


@app.route("/api/notify/test", methods=["POST"])
@requires_auth
def api_notify_test():
    ch = request.get_json(force=True).get("channel", "")
    ok = notifier.test(ch)
    return jsonify({"ok": ok})


# ----------------------------------------------------------------------
# EC20 资料 / AT 速查 API
# ----------------------------------------------------------------------
@app.route("/api/ec20")
@requires_auth
def api_ec20():
    return jsonify({
        "common": EC20_COMMON,
        "variants": EC20_VARIANTS,
        "firmware_note": FIRMWARE_NOTE,
        "applications": APPLICATIONS,
    })


@app.route("/api/atdoc")
@requires_auth
def api_atdoc():
    q = request.args.get("q", "")
    cat = request.args.get("cat", "")
    if cat:
        cmds = [{"cmd": c, "desc": d, "src": s, "example": e}
                for c, d, s, e in at_by_cat(cat)]
    else:
        cmds = [{"cmd": c, "desc": d, "src": s, "example": e, "cat": ct}
                for c, d, s, e, ct in at_search(q)]
    return jsonify({"commands": cmds, "categories": CATEGORIES})


@app.route("/api/logs")
@requires_auth
def api_logs():
    """Return recent journalctl logs for scc-lite or scc-web."""
    service = request.args.get("service", "scc-lite")
    if service not in ("scc-lite", "scc-web"):
        service = "scc-lite"
    import subprocess
    try:
        p = subprocess.run(
            ["journalctl", "-u", f"{service}.service", "-n", "100",
             "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=10)
        return jsonify({"ok": True, "logs": p.stdout or "(无日志)"})
    except Exception as e:
        return jsonify({"ok": False, "logs": f"读取失败: {e}"})


@app.route("/api/sms/export")
@requires_auth
def api_sms_export():
    """Export all SMS as JSON (full backup)."""
    from flask import Response
    import json
    from datetime import datetime
    db = get_store()
    rows = db.execute("SELECT * FROM sms ORDER BY id").fetchall()
    db.close()
    data = {"version": "scc-lite-1.0", "exported_at": datetime.now().isoformat(),
            "messages": [dict(r) for r in rows]}
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=scc-lite-backup.json"})


@app.route("/api/sms/export/<number>")
@requires_auth
def api_sms_export_number(number):
    """Export single contact's SMS as JSON."""
    from flask import Response
    import json
    from datetime import datetime
    db = get_store()
    rows = db.execute(
        "SELECT * FROM sms WHERE sender=? ORDER BY id", (number,)).fetchall()
    db.close()
    data = {"version": "scc-lite-1.0", "exported_at": datetime.now().isoformat(),
            "number": number, "messages": [dict(r) for r in rows]}
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=scc-lite-{number}.json"})


@app.route("/api/sms/import", methods=["POST"])
@requires_auth
def api_sms_import():
    """Import SMS from JSON backup (full or single-number). Skips duplicates."""
    data = request.get_json(force=True)
    msgs = data.get("messages", [])
    if not isinstance(msgs, list):
        return jsonify({"ok": False, "error": "invalid format"})
    db = get_store()
    imported = 0
    for m in msgs:
        sender = m.get("sender", "")
        body = m.get("body", "")
        sms_time = m.get("sms_time", "")
        direction = m.get("direction", "in")
        # Skip duplicates
        cur = db.execute(
            "SELECT id FROM sms WHERE direction=? AND sender=? AND sms_time=? AND body=?",
            (direction, sender, sms_time, body))
        if cur.fetchone():
            continue
        db.execute(
            "INSERT INTO sms (direction,sender,body,sms_time,forwarded)"
            " VALUES (?,?,?,?,1)",
            (direction, sender, body, sms_time))
        imported += 1
    db.commit()
    db.close()
    return jsonify({"ok": True, "imported": imported, "total": len(msgs)})


@app.route("/api/sms/<int:msg_id>", methods=["DELETE"])
@requires_auth
def api_sms_delete(msg_id):
    """Delete a single SMS message."""
    db = get_store()
    db.execute("DELETE FROM sms WHERE id=?", (msg_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/sms/contact/<number>", methods=["DELETE"])
@requires_auth
def api_sms_delete_contact(number):
    """Delete entire conversation with a number."""
    db = get_store()
    cur = db.execute("DELETE FROM sms WHERE sender=?", (number,))
    count = cur.rowcount
    db.commit()
    db.close()
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/qq/openids")
@requires_auth
def api_qq_openids():
    """Return QQ openids captured via WebSocket receiver."""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from qq_receiver import get_captured_openids, set_openid_file
        # 和 daemon 用同一个 data_dir
        cfg = load_config()
        data_dir = cfg.get("data_dir", "/opt/scc-lite-for-EC20-4g-module/data")
        set_openid_file(os.path.join(data_dir, "qq_openids.json"))
        return jsonify({"ok": True, "openids": get_captured_openids()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "openids": {"c2c": {}, "group": {}}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/scc-lite-for-EC20-4g-module/config.yaml")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    global CONFIG_PATH
    CONFIG_PATH = args.config

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    load_config()
    w = config.get("web", {})
    port = w.get("port", 7577)
    log.info("SCC-lite web on :%d", port)
    # v0.5.6: 启动 API 缓存后台线程
    try:
        _cache_start()
    except Exception as e:
        log.warning("cache thread start failed: %s", e)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
