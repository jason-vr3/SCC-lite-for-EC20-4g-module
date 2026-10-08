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
import sqlite3
import yaml

VERSION = "0.5.0"
from flask import Flask, request, jsonify, render_template_string, Response

from modem import Modem, ModemError
from data_control import DataControl
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
    d = config.get("data", {})
    return DataControl(
        qmi_dev=d.get("qmi_dev", "/dev/cdc-wdm0"),
        iface=d.get("iface", "wwan0"),
        apn=d.get("apn", ""),
    )


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
        return f(*args, **kwargs)
    return decorated


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
<div class="logo">📡 SCC-lite<small>SMS Control Centre v0.5.0</small></div>
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
<div class="user"><span>👤 Admin</span><a href="/logout">退出</a></div>
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
<div class="card"><h2>最新短信</h2><div id="dash-sms">加载中…</div></div>
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
<div class="card"><h2>设备状态 <button class="btn ghost" onclick="loadDevice()">刷新</button></h2>
<div id="device-info">加载中…</div></div>
<div class="card"><h2>飞行模式</h2>
<div class="row"><span id="flight-status">未知</span>
<button class="btn" onclick="setFlight(true)">开启飞行模式</button>
<button class="btn ghost" onclick="setFlight(false)">关闭飞行模式</button></div></div>
</section>
<!-- Data -->
<section id="t-data" class="hidden">
<h1 class="page-title">蜂窝网络</h1><p class="page-sub">QMI 数据连接管理</p>
<div class="card"><h2>数据连接 <button class="btn ghost" onclick="loadData()">刷新</button></h2>
<div id="data-info">加载中…</div>
<div class="row" style="margin-top:8px">
<button class="btn" id="btn-data-on" onclick="dataOn()">开启上网</button>
<button class="btn danger" id="btn-data-off" onclick="dataOff()">关闭上网</button></div></div>
<div class="card"><h2>Ping 测试</h2>
<div class="row"><input id="ping-target" value="114.114.114.114" style="flex:2">
<button class="btn" onclick="pingTest()">Ping 3 次</button></div>
<div id="ping-result"></div></div>
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
 if(data)$('st-data').textContent=data.connected?(data.ip||'已连接'):'未连接';
 if(sms)$('dash-sms').innerHTML=sms.messages.slice(0,5).map(m=>
  `<div class="msg ${m.direction}"><div class="meta">${m.direction==='in'?'📩':'📤'} ${esc(m.sender)} · ${esc(m.sms_time||m.created_at)}</div><div>${esc(m.body?.slice(0,80))}</div></div>`).join('')||'暂无短信';}
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
async function loadDevice(){const d=await api('/api/device');if(!d)return;
 const r=d.reg||{};const regTxt={0:'未注册',1:'已注册(本地)',2:'搜索中',3:'被拒绝',5:'已注册(漫游)'}[r.stat]??('stat='+r.stat);
 $('device-info').innerHTML=`<dl class="kv">
 <dt>IMEI</dt><dd>${esc(d.imei)}</dd><dt>IMSI</dt><dd>${esc(d.imsi)}</dd>
 <dt>ICCID</dt><dd>${esc(d.iccid)}</dd><dt>信号</dt><dd>RSSI ${d.signal?.rssi??'-'} ${d.signal&&d.signal.rssi<=31?'<span class="badge '+(d.signal.rssi>=15?'ok':'bad')+'">'+(d.signal.rssi>=15?'良好':'较弱')+'</span>':''}</dd>
 <dt>注册</dt><dd>${regTxt}</dd><dt>运营商</dt><dd>${esc(d.operator?.oper||'-')}</dd>
 <dt>串口</dt><dd>${esc(d.port)}</dd></dl>`;
 $('flight-status').innerHTML=d.flight_mode?'<span class="badge bad">飞行模式开</span>':'<span class="badge ok">正常</span>';}
async function setFlight(on){const d=await api('/api/device/flight',{method:'POST',body:JSON.stringify({enable:on})});
 alert(d&&d.ok?'已执行，等待 modem 生效':'失败: '+(d&&d.error));loadDevice();}
async function loadData(){const d=await api('/api/data');if(!d)return;
 $('data-info').innerHTML=`<dl class="kv">
 <dt>状态</dt><dd>${d.connected?'<span class="badge ok">已连接</span>':'<span class="badge bad">未连接</span>'}</dd>
 <dt>IP</dt><dd>${esc(d.ip||'-')}</dd><dt>网卡</dt><dd>${esc(d.iface)}</dd></dl>`;
 $('btn-data-on').disabled=d.connected;$('btn-data-off').disabled=!d.connected;}
async function dataOn(){$('data-info').innerHTML='拨号中…(约10秒)';
 const d=await api('/api/data/on',{method:'POST'});alert(d&&d.ok?'已连接':'失败: '+(d&&d.error));loadData();}
async function dataOff(){const d=await api('/api/data/off',{method:'POST'});
 alert(d&&d.ok?'已断开':'失败');loadData();}
async function pingTest(){const t=$('ping-target').value.trim()||'114.114.114.114';
 $('ping-result').innerHTML='测试中…';
 const d=await api('/api/data/ping',{method:'POST',body:JSON.stringify({target:t})});
 $('ping-result').innerHTML=d?`<dl class="kv"><dt>目标</dt><dd>${esc(t)}</dd>
 <dt>结果</dt><dd>${d.ok?'<span class="badge ok">通</span>':'<span class="badge bad">不通</span>'}</dd>
 <dt>平均时延</dt><dd>${d.avg_ms} ms</dd><dt>丢包</dt><dd>${d.loss_pct}%</dd></dl>`:'失败';}
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
async function loadLogs(){const d=await api('/api/logs?service='+encodeURIComponent(logService));if(!d)return;
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
    return render_template_string(PAGE)


@app.route("/logout")
def logout():
    return Response("已退出，请关闭浏览器标签页", 401,
                    {"WWW-Authenticate": 'Basic realm="SCC-lite"'})


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
@app.route("/api/device")
@requires_auth
def api_device():
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
    return jsonify(info)


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
@app.route("/api/data")
@requires_auth
def api_data():
    return jsonify(get_data_ctl().get_status())


@app.route("/api/data/on", methods=["POST"])
@requires_auth
def api_data_on():
    ok = get_data_ctl().start()
    d = get_data_ctl().get_status()
    return jsonify({"ok": ok, "error": "" if ok else "拨号失败，查日志",
                    **d})


@app.route("/api/data/off", methods=["POST"])
@requires_auth
def api_data_off():
    ok = get_data_ctl().stop()
    return jsonify({"ok": ok})


@app.route("/api/data/ping", methods=["POST"])
@requires_auth
def api_data_ping():
    target = request.get_json(force=True).get("target", "114.114.114.114")
    return jsonify(get_data_ctl().ping_test(target, count=3))


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
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
