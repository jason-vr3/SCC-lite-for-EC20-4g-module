# SCC-lite 项目全程踩坑记录（2026-10-01 ~ 2026-10-13）

> 从玩客云 OneCloud 起步、因性能不足迁到 N1、自研 SCC-lite 替代 VoHiveX 的完整过程。
> 按时间顺序记录每个坑：**现象 → 根因 → 教训 → 验证**。
> 指令只贴实机验证过的关键命令，不贴推测性命令。

## 目录

- [一、项目背景与架构变迁](#一项目背景与架构变迁)
- [二、踩坑时间线](#二踩坑时间线)
  - [2026-10-03](#2026-10-03)
    - [1. armbian-install 报 no u-boot package found](#p1)
    - [2. Debian 12 Bookworm 官方仓库没有 Asterisk](#p2)
  - [2026-10-04](#2026-10-04)
    - [3. chan_sip 抢占 5060/UDP，PJSIP 注册误报 Wrong password](#p3)
    - [4. pjsip.conf 漏写 transport 段](#p4)
    - [5. chan_dongle 编译 --with-astversion 取值失败](#p5)
    - [6. chan_dongle 编译缺依赖、安装路径不存在](#p6)
    - [7. 自定义放音文件目录放错](#p7)
    - [8. 源码版 Asterisk 的 SysV 启动脚本导致 active (exited)](#p8)
    - [9. trixie 上 Asterisk 20.21.0 反复 SIGBUS](#p9)
    - [10. N1 OpenWrt WAN 防火墙接口名错位](#p10)
    - [11. N1 Tailscale 通但 SSH 不进](#p11)
  - [2026-10-05](#2026-10-05)
    - [12. OneCloud 内核/模块错位，tailscaled 起不来](#p12)
    - [13. .burn.img 无法 loop 挂载](#p13)
    - [14. uImage 头标签与运行内核不一致](#p14)
    - [15. 大疆改制 QDC507 模块通话接通但无声](#p15)
    - [16. Samba 共享参数误放入 [global]](#p16)
    - [17. SIP MESSAGE 配置后不生效](#p17)
  - [2026-10-06](#2026-10-06)
    - [18. data-web.py 部署后不执行](#p18)
    - [19. AT+CGACT=1,1 不能代表 Linux 已联网](#p19)
    - [20. QMI CID allocation failed](#p20)
    - [21. qmi-network start 报 241 interface-in-use-config-match](#p21)
    - [22. VoHive Web 发短信对方收不到](#p22)
    - [23. VoHive v1.5.5 AT backend 报 worker_nil](#p23)
    - [24. 数据开启后恢复 VoHive 导致断网](#p24)
    - [25. EC20 的 AT 命令大小写敏感](#p25)
    - [26. AOSP APN 数据库 raw 地址 404](#p26)
    - [27. APN 排序选中 cmwap/ims](#p27)
  - [2026-10-07](#2026-10-07)
    - [28. chan_dongle 与 chan_quectel 抢 modem](#p28)
    - [29. chan_quectel 通话中 AT 超时被误杀](#p29)
    - [30. OneCloud 通话大量 lost frame → 迁 N1](#p30)
    - [31. N1 通话后掉网（GSM not registered）](#p31)
  - [2026-10-08](#2026-10-08)
    - [32. 误判"EC20 不支持 UAC"](#p32)
    - [33. QMI 79 PolicyMismatch（残留 bearer）](#p33)
    - [34. 广电卡 IPv4 不通](#p34)
    - [35. MBN 手动切换打乱串口映射](#p35)
    - [36. VoHiveX 删除，自研 SCC-lite](#p36)
    - [37. EC20 LTE 发射致 OneCloud USB 全口掉线](#p37)
  - [2026-10-09](#2026-10-09)
    - [38. scc-web.py 缺 import re 被宽泛 except 吞掉](#p38)
    - [39. IPv6 显示 bug](#p39)
    - [40. v0.5.6 升级说明漏系统软件包](#p40)
    - [41. SCC-lite 定纯 QMI，放弃 ECM](#p41)
  - [2026-10-10](#2026-10-10)
    - [42. fail2ban backend=systemd 架空 logpath](#p42)
    - [43. fail2ban 封禁走 nftables](#p43)
    - [44. bash /dev/udp 探测包被拆行](#p44)
    - [45. 电信卡短信彻底不通](#p45)
    - [46. wwan0 默认路由铁律](#p46)
    - [47. 旧 bearer 卡住致 79 PolicyMismatch](#p47)
    - [48. wds-reset 后 wwan0 变 DOWN 致 DHCP 失败](#p48)
    - [49. N1↔VPS SIP trunk 打通](#p49)
  - [2026-10-11](#2026-10-11)
    - [50. v0.5.8 发布：DHCP 拨号、无 APN 优先](#p50)
    - [51. v0.5.9 首次打包翻车：install.sh 无 +x 且漏 sms_pdu.py](#p51)
    - [52. PDU 模式丢长短信：+CMGL 正则过严](#p52)
    - [53. 只读验收流程（用户定的闸门）](#p53)
    - [54. _parse_udh 截断崩溃路径（本地复现、未在 N1 触发）](#p54)
    - [55. "10086 长短信收不到"真因：chan_quectel 截胡（本节详见第三章专题）](#p55)
    - [56. v0.5.9 发布](#p56)
  - [2026-10-12](#2026-10-12)
  - [2026-10-13](#2026-10-13)
    - [63. 前端 18 处调用未定义 JS 函数，数据页交互全灭](#p63)
    - [64. 短信"丢失"：老短信 module_id 为空被过滤隐藏](#p64)
    - [65. 仪表盘运营商无显示：dev.get("data") 误取](#p65)
    - [66. 号码管理"加载中"：get_store() 返回原生连接非 Store](#p66)
    - [67. 弹窗写在 hidden section 内，仪表盘点击无反应](#p67)
    - [68. 打包漏文件：手写子集漏 7 个，全新安装必挂](#p68)
    - [69. 架构决策：短信按 IMSI（SIM）关联，不按 IMEI（硬件）](#p69)
    - [70. 信号两级缓存：AT+CSQ 与重型扫描解耦](#p70)
    - [57. at+qdownload 在 EC20-CE 上返回 ERROR](#p57)
    - [58. QFirehose 被 glibc fortify 误杀](#p58)
    - [59. Sahara 报错：模块停在 EDL 里跑着旧 programmer](#p59)
    - [60. R06A15 上 uac_fs=1 让 UAC 完全不可用](#p60)
    - [61. R06A15 出厂 usbcfg 默认关 UAC](#p61)
    - [62. 刷机前备份 AT 配置](#p62)
- [三、专题：PDU 长短信重组与 chan_quectel 截胡](#三专题pdu-长短信重组与-chan_quectel-截胡)
- [四、铁律与纪律汇总](#四铁律与纪律汇总)

---

## 一、项目背景与架构变迁

| 时间 | 事件 |
|------|------|
| 10-01~10-05 | 玩客云 OneCloud（Amlogic S805，32 位，1G RAM）跑 Ubuntu jammy，Asterisk 管电话 |
| 10-06 | 大疆改制模块语音调测失败（硬件原因），改买正品 Quectel EC20 |
| 10-07 | OneCloud 通话大量 `lost frame`，S805 性能不足，**语音栈迁到 N1**（S905D） |
| 10-08 | VoHiveX 官方跑路有 bug，**从 N1 彻底删除，自研 SCC-lite** 接管短信+蜂窝数据 |
| 10-08 | 语音方案定案：chan_quectel + UAC（USB 音频），音质验收通过 |
| 10-09~10-11 | SCC-lite 迭代 v0.5.5 → v0.5.9（DHCP 拨号、PDU 长短信） |

**最终分工**：Asterisk 只管语音（EC20 经 chan_quectel 走 UAC），SCC-lite 管短信和蜂窝数据（纯 QMI）。
**串口红线**（10-08 定，之后一直遵守）：Asterisk 占 `ttyUSB1`/`ttyUSB2`，短信走 `ttyUSB3`，互不抢。

**硬件档案**（Quectel EC20，非改制）：USB `2c7c:0125`，固件 `EC20CEHDLGR06A05M1G`，
IMEI `866041049020974`，2026-10-10 起用移动卡（IMSI `46000` 开头）。

---

## 二、踩坑时间线

### 2026-10-03

#### <a id="p1"></a>1. `armbian-install` 报 `no u-boot package found`
- 现象：U 盘启动后往 eMMC 装系统失败退出。
- 根因：hzyitc 非官方镜像不带 `armbian-install` 要的 u-boot deb 包。
- 教训：非官方镜像装 eMMC 用 `.burn` 版线刷；官方镜像才走 `armbian-install`。
- 验证：无（流程性结论）。

#### <a id="p2"></a>2. Debian 12 Bookworm 官方仓库没有 Asterisk
- 现象：`apt install asterisk` 报无安装候选。
- 根因：Asterisk 在 Debian 12 发布前因维护者跟进不及时被移出官方稳定仓库。
- 教训：部署前先查 packages.debian.org，不凭经验断言仓库里有什么。

### 2026-10-04

#### <a id="p3"></a>3. chan_sip 抢占 5060/UDP，PJSIP 注册误报 Wrong password
- 现象：Linphone 注册 6001，日志报 `chan_sip.c ... Wrong password`，但密码是对的。
- 根因：Ubuntu 打包版默认加载老 `chan_sip`，注册请求被它截获，PJSIP 根本没收到。
- 教训：`noload => chan_sip.so` 必须写在 `modules.conf` 的 `[modules]` 段内；
  追加到文件末尾会落进 `[global]` 段，等于没写。
- 验证：`asterisk -rx "pjsip show endpoints"` 注册成功。

#### <a id="p4"></a>4. pjsip.conf 漏写 transport 段
- 现象：禁掉 chan_sip 后注册直接 timeout，`pjsip show transports` 返回 No objects found。
- 根因：一键配置脚本漏了 `[transport-udp]` 段。
- 教训：PJSIP 必须有 transport 对象才会监听 UDP，覆盖写配置时不能漏。

#### <a id="p5"></a>5. chan_dongle 编译 `--with-astversion` 取值失败
- 现象：`$(asterisk -V | awk '{print $2}')` 报 `Invalid --with-astversion`。
- 根因：Ubuntu 的 `asterisk -V` 输出带 `~dfsg` 打包后缀，awk 取出的不是纯版本号。
- 教训：版本参数写死纯数字，如 `--with-astversion=18.10.0`。
- 验证：编译通过。

#### <a id="p6"></a>6. chan_dongle 编译缺依赖、安装路径不存在
- 现象：configure 报 `sqlite3 library missing`；`make install` 报无法创建 `/usr/lib/asterisk/modules`。
- 根因：缺 `libsqlite3-dev`；Ubuntu 真实模块目录是 multiarch 路径
  `/usr/lib/arm-linux-gnueabihf/asterisk/modules/`。
- 教训：编译前装齐依赖；用 `dpkg -L asterisk-modules` 查真实模块目录，手动拷 `.so` 过去。

#### <a id="p7"></a>7. 自定义放音文件目录放错
- 现象：拨 200 不放音。
- 根因：打包版 datadir 是 `/usr/share/asterisk/sounds/`，不是源码版的 `/var/lib/asterisk/sounds/`。
- 教训：源码安装和发行版打包的路径不一样，按实际装的包查证。

#### <a id="p8"></a>8. 源码版 Asterisk 的 SysV 启动脚本导致 `active (exited)`
- 现象：`systemctl status asterisk` 显示 active (exited)，但无进程、CLI 连不上。
- 根因：`make config` 装的是旧 SysV 脚本，systemd 只记录了脚本退出。
- 教训：换原生 systemd unit：`Type=simple` + `asterisk -f` + `Restart=always`。

#### <a id="p9"></a>9. trixie 上 Asterisk 20.21.0 反复 SIGBUS
- 现象：拨 200 通话秒断，journal 多次 `code=killed, status=7/BUS`。
- 根因：未完全定位（禁用 chan_dongle 后仍崩）。
- 教训：放弃 trixie + 源码编译路线，改刷 Jammy 用仓库版 Asterisk 18.10.0。

#### <a id="p10"></a>10. N1 OpenWrt WAN 防火墙接口名错位
- 现象：`cannot resolve device of network 'wan'`，WAN 口 SSH 不通。
- 根因：逻辑接口叫 `wan_eth0`，但防火墙 wan zone 引用的是不存在的 `wan`/`wan6`；
  WAN SSH 配成了 Redirect 而不是 input 规则。
- 教训：接口名和 zone 引用必须一致；WAN SSH 用 input 规则。

#### <a id="p11"></a>11. N1 Tailscale 通但 SSH 不进
- 现象：隧道通，另一节点经 Tailscale SSH 不到 N1。
- 根因：Dropbear 只绑了 LAN 地址，tailscale0 没归属任何防火墙 zone。
- 教训：tailscale0 建 unmanaged 接口并加入 lan zone。
- 验证：对端经 Tailscale SSH 登录成功。

### 2026-10-05

#### <a id="p12"></a>12. OneCloud 内核/模块错位，tailscaled 起不来
- 现象：`tailscale up` 报 `503 Service Unavailable: no backend`。
- 根因：某次 `apt upgrade` 把内核升到 6.12.28 并删掉 `/lib/modules/6.6.21-current-meson`，
  但 `/boot/uImage` 仍是 6.6.21——运行中的 6.6.21 内核没有任何可用模块（TUN 缺失）。
  非官方镜像缺少升级内核时重打 uImage 的钩子。
- 教训：重刷后第一时间 `apt-mark hold linux-image-current-meson linux-dtb-current-meson`，
  再做任何 upgrade。（N1 的 ophub 内核不归 apt 管，此条不适用，precaution 改为别跑 `armbian-update`。）
- 验证：`tailscale0` UP 并拿到地址。

#### <a id="p13"></a>13. `.burn.img` 无法 loop 挂载
- 现象：`losetup -fP` 提示长度不适配扇区，7-Zip 也打不开。
- 根因：burn 版是 Amlogic 线刷专用格式，不是 raw 镜像。
- 教训：burn 镜像只能线刷，不能当 raw 镜像挂载取文件。

#### <a id="p14"></a>14. uImage 头标签与运行内核不一致
- 现象：`mkimage -l` 显示 6.12.28，`uname -r` 却是 6.6.21。
- 根因：payload 是自解压 zImage 风格内核，头标签不可信。
- 教训：不能只看 uImage 头判断内核版本。

#### <a id="p15"></a>15. 大疆改制 QDC507 模块通话接通但无声
- 现象：外呼可建立、双方无声，日志 `AT^DDSETEX (setup voice) failed`。
- 根因：QDC507 定制固件删了 `AT+QPCMV`/`AT+QAUDMOD`，chan_dongle 回退到该模块
  不接受的华为私有命令。
- 教训：改制模块不能按公版 EC25 假设。最终判定语音失败根因为硬件，改买正品 EC20。

#### <a id="p16"></a>16. Samba 共享参数误放入 `[global]`
- 现象：共享不生效。
- 根因：共享定义必须用独立段（如 `[udisk]`），`path` 等写 `[global]` 里无效。
- 教训：注意 Samba 段结构。

#### <a id="p17"></a>17. SIP MESSAGE 配置后不生效
- 现象：消息仍进 `[internal]` 撞 `Dial()`。
- 根因：pjsip.conf 改了 `message_context` 后只做了 `dialplan reload`。
- 教训：改 pjsip.conf 后必须 `pjsip reload`，`dialplan reload` 不够。

### 2026-10-06

#### <a id="p18"></a>18. data-web.py 部署后不执行
- 现象：服务起不来。
- 根因：脚本没有可执行权限。
- 教训：部署说明必须写 `chmod +x`。（这个坑在 10-11 v0.5.9 又踩一次，见后文。）

#### <a id="p19"></a>19. `AT+CGACT=1,1` 不能代表 Linux 已联网
- 现象：模块侧 PDP 显示 active，但 wwan0 无 IP，页面绿灯误判。
- 根因：模块侧 PDP 状态和主机侧 IP 获取是两回事。
- 教训：联网判定以 wwan0 实际拿到 IP 为准。

#### <a id="p20"></a>20. QMI `CID allocation failed`
- 现象：`qmicli --nas-get-serving-system` 报无法分配 CID。
- 根因：VoHive v1.5.5 的 QMI backend 长期独占 `/dev/cdc-wdm0`。
- 教训：QMI 操作前先停 VoHive 释放控制口。

#### <a id="p21"></a>21. `qmi-network start` 报 241 `interface-in-use-config-match`
- 现象：封装命令拨号失败。
- 根因：残留 bearer 未清理。
- 教训：直接用 `qmicli --wds-start-network`，不用 `qmi-network` 封装。

#### <a id="p22"></a>22. VoHive Web 发短信对方收不到
- 现象：发送显示成功，对方无收到。
- 根因：国内号码没带国家码。
- 教训：收件人写 `+86` 开头。

#### <a id="p23"></a>23. VoHive v1.5.5 AT backend 报 `worker_nil`
- 现象：切 AT backend 后 AT 超时、网页认不到设备。
- 根因：该版本 AT backend 自身 bug（同一串口经 Python 直测 AT 正常）。
- 教训：v1.5.5 只用 QMI backend。

#### <a id="p24"></a>24. 数据开启后恢复 VoHive 导致断网
- 现象：wwan0 有 IP 但 ping 不通。
- 根因：`on` 流程结束时恢复 VoHive，切断了 QMI 数据会话。
- 教训：数据开启期间保持 VoHive 停止（短信暂停），关闭数据后再恢复。

#### <a id="p25"></a>25. EC20 的 AT 命令大小写敏感
- 现象：大写 `AT+CFUN=1,1` 返回 ERROR，小写 `at+cfun=1,1` 才 OK。
- 根因：该模块 AT 大小写敏感。
- 教训：AT 命令统一用小写。

#### <a id="p26"></a>26. AOSP APN 数据库 raw 地址 404
- 现象：内置的 GitHub raw 链接下载失败。
- 根因：地址失效。
- 教训：下载链接必须实测可下载；改用 open-carrier-data（实测 4.1MB、24903 条）。

#### <a id="p27"></a>27. APN 排序选中 cmwap/ims
- 现象：自动识别给出错误 APN。
- 根因：排序只看 `type=default`，没排除 WAP/MMS/IMS。
- 教训：APN 名含 wap/mms/ims 的加惩罚分。

### 2026-10-07

#### <a id="p28"></a>28. chan_dongle 与 chan_quectel 抢 modem
- 现象：AT timeout 交替出现在两个驱动日志。
- 根因：两个驱动同时加载，抢串口。
- 教训：`noload => chan_dongle.so` 禁用旧驱动；串口独占，一次只跑一个驱动。
- 验证：卸载后 AT 超时消失。

#### <a id="p29"></a>29. chan_quectel 通话中 AT 超时被误杀
- 现象：`do_monitor_phone: timedout while waiting 'OK'` 后 `goto e_cleanup` 拆线。
- 根因：QPCMV 模式下通话时 AT 口无响应但 PCM 音频正常，超时不该拆线。
- 教训：补丁改为 LOG_WARNING + `at_queue_flush(pvt)`；动手前先在源码核实函数名
  真实存在（臆测的 `at_queue_clear` 不存在）。

#### <a id="p30"></a>30. OneCloud 通话大量 `lost frame` → 迁 N1
- 现象：2.5 分钟通话反复 `25380 lost frame(s)`。
- 根因：S805 CPU 性能不足。
- 教训：**语音栈迁到 N1（S905D）**。这是整个项目从 OneCloud 转向 N1 的直接原因。
- 验证：N1 上 UAC 通话音质完美（见 10-08）。

#### <a id="p31"></a>31. N1 通话后掉网（`GSM not registered`）
- 现象：2 分钟通话正常挂断后 `+CREG: 2,3` / `+CEREG: 0,3`，`AT+CFUN=1,1` 后仍搜网无信号。
- 根因：基带 wedged，软重启清不掉锁存状态。
- 教训：**模块 AT 无响应 / 基带异常时，断电硬重启**，`AT+CFUN=1,1` 不够。
  通话后先直连 AT 辨真假再动驱动；MBN 保持自动。

### 2026-10-08

#### <a id="p32"></a>32. 误判"EC20 不支持 UAC"
- 现象：`arecord -l` 无声卡，一度认定硬件无 UAC。
- 根因：UAC 功能没启用，不是硬件不支持。
  `AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,0,1` + `AT+CFUN=1,1` 后出现 `card 1: Android`。
- 教训：先查功能开关再下硬件结论。
- 验证：用户多次手机呼入录音，UAC 音质完美、无断续，方案验收通过。

#### <a id="p33"></a>33. QMI 79 PolicyMismatch（残留 bearer）
- 现象：`qmicli --wds-start-network` 报 `QMI protocol error (79): 'PolicyMismatch'`。
- 根因：残留激活 bearer（`AT+CGACT?` 显示 `+CGACT: 1,1`）。
- 教训：`qmicli -d /dev/cdc-wdm0 --wds-reset` 清除；
  `AT+CGACT=0,1` / `AT+QIDEACT=1` 对 QMI 建的 bearer 无效。
- 验证：reset 后拨号成功。

#### <a id="p34"></a>34. 广电卡 IPv4 不通
- 现象：IPv4 100% 丢包，IPv6 正常。
- 根因：广电核心网侧拦截 IPv4（10-11 DHCP 实测推翻此结论，见后文）。
- 教训：先区分运营商侧问题，不在主机侧死磕。（注：此条 10-11 被实测推翻，保留作记录。）

#### <a id="p35"></a>35. MBN 手动切换打乱串口映射
- 现象：`AT+QMBNCFG="Select",...CMCC` 返回 OK，重启后仍是 `ROW_Generic_3GPP`；
  ttyUSB3 消失、冒出 ttyUSB4。
- 根因：该 EC20 上 MBN Select 可能不生效，且打乱 USB 端口映射。
- 教训：MBN 保持自动（AutoSel=1）；断电硬重启恢复端口映射。

#### <a id="p36"></a>36. VoHiveX 删除，自研 SCC-lite
- 现象：VoHive 官方跑路，v1.5.5 AT backend 有 bug。
- 根因：闭源软件无人维护。
- 教训：10-08 从 N1 彻底删除 VoHiveX，短信+数据改走自研 SCC-lite（纯 QMI）。
  定下串口红线：Asterisk 用 ttyUSB1/2，短信用 ttyUSB3。

#### <a id="p37"></a>37. EC20 LTE 发射致 OneCloud USB 全口掉线
- 现象：通话/数据时 USB 口集体掉线。
- 根因：LTE 发射峰值电流 1.5~2A，超出 OneCloud USB 口供电。
- 教训：OneCloud 侧用带独立供电的 USB Hub。

### 2026-10-09

#### <a id="p38"></a>38. scc-web.py 缺 `import re` 被宽泛 except 吞掉
- 现象：`/api/system` 返回 CPU 未知、内存 0/0、接口为空。
- 根因：`api_system()` 用了 `re` 但顶层没 import，NameError 被宽泛 except 吞掉静默返回空值。
- 教训：`py_compile` 查不出缺失的运行时 import；宽泛 except 会掩盖真实错误。

#### <a id="p39"></a>39. IPv6 显示 bug
- 现象：wwan0 有 IPv6，蜂窝页面看不到。
- 根因：`ipv6 = self.get_ipv6() if connected else []`，bearer 一断就隐藏 IP。
- 教训：永远显示网卡真实 IP，bearer 状态单独区分。

#### <a id="p40"></a>40. v0.5.6 升级说明漏系统软件包
- 现象：用户 N1 缺 samba 等包，按说明升级后功能缺失。
- 根因：升级说明只写了代码替换。
- 教训：升级说明必须包含新增系统软件包的 apt 安装步骤。

#### <a id="p41"></a>41. SCC-lite 定纯 QMI，放弃 ECM
- 现象：ECM 和语音抢串口冲突。
- 根因：ECM 模式占用 AT 口影响语音。
- 教训：`AT+QCFG="usbnet",0` 定纯 QMI；wwan0 须 raw-ip
  （模块侧 `--wda-set-data-format=raw-ip` 与驱动侧 `/sys/class/net/wwan0/qmi/raw_ip` 一致）。

### 2026-10-10

#### <a id="p42"></a>42. fail2ban `backend=systemd` 架空 logpath
- 现象：jail status 显示 `Journal matches:`，不读指定的日志文件。
- 根因：Debian/Ubuntu 打包的 fail2ban 全局默认 `backend = systemd`，静默忽略 logpath。
- 教训：jail 里显式写 `backend = auto`；status 显示 `File list:` 才是生效。
- 验证：`fail2ban-client status asterisk` 显示 File list；实弹 6 次 REGISTER 探测封禁生效。

#### <a id="p43"></a>43. fail2ban 封禁走 nftables
- 现象：`iptables -L` 看不到封禁规则，误判未生效。
- 根因：Ubuntu 24.04 封禁实际走 nftables。
- 教训：用 `nft list ruleset` 查封禁。

#### <a id="p44"></a>44. bash `/dev/udp` 探测包被拆行
- 现象：探测 REGISTER 发出去，Asterisk 无日志。
- 根因：bash 按行拆包，SIP datagram 被拆散。
- 教训：探测必须一次 `sendto()` 发完整 datagram（Python）。

#### <a id="p45"></a>45. 电信卡短信彻底不通
- 现象：`AT+CMGS`/`AT+CMGL`/`AT+CSCA` 全 ERROR（连 `>` 提示符都不给），60 秒无 `+CMTI`。
- 根因：卡/网络侧问题（同模块换移动卡后短信收发正常；该电信卡在手机上正常）。
- 教训：VoLTE 语音可用不代表短信可用；`AT+QCFG="ims"` 显示 `1,0` 不可信。
  手动 AT 调试前必须 `systemctl stop scc-lite` 释放 ttyUSB3，否则串口争用返回错乱。

#### <a id="p46"></a>46. wwan0 默认路由铁律
- 现象：用户多次明确禁止。
- 根因：默认路由走 wwan0 会抢占 SSH 管理通道，导致断连。
- 教训：**铁律——蜂窝调测绝不加 `default via wwan0`**，只用 `ping -I wwan0` 或明细路由。

#### <a id="p47"></a>47. 旧 bearer 卡住致 79 PolicyMismatch
- 现象：`wds-reset` 显示成功但旧 bearer 未清，新建报 79。
- 根因：旧 packet data handle 仍存活。
- 教训：v0.5.8 加 `_reuse_active_bearer()`：PolicyMismatch 时查包服务状态，connected 则复用。

#### <a id="p48"></a>48. `wds-reset` 后 wwan0 变 DOWN 致 DHCP 失败
- 现象：`udhcpc: read error: Network is down`。
- 根因：`wds-reset` 后接口变 DOWN，漏了 `ip link set wwan0 up`。
- 教训：udhcpc 前先 up 接口；此前"udhcpc -b fork 不稳定"的说法被推翻。

#### <a id="p49"></a>49. N1↔VPS SIP trunk 打通
- 现象：无（成功记录）。
- 过程：手机→EC20→N1→trunk→VPS→8001，bridge 80 秒干净挂断。
- 结论：VPS SIP 用非标 65061/UDP（防扫描）；trunk 用 ulaw（EC20 单通道，Opus 省带宽不值得）；
  分机可用 opus+ulaw。
- 验证：真机呼入 80 秒通话。

### 2026-10-11

#### <a id="p50"></a>50. v0.5.8 发布：DHCP 拨号、无 APN 优先
- 现象：无（成功记录）。
- 过程：用户拍板"方案 B"——`udhcpc` + 自定义 `udhcpc-noroute.sh`（只配 IP/MTU/DNS，不加路由）。
  拨号策略：先无 APN 拨号，后台线程并行 IMSI 识别备用；DHCP 25 秒无 IP 才用识别出的 APN 重拨一次。
- 教训：`_step_start_network` 传 `None` 必须真正不带 APN 参数（曾把 `None` 当"回退到 self.apn"用，
  导致日志里"无 APN"实际带了 `apn=cmnet`）；`scc-web.py` 自持 DataControl 实例，
  换文件后必须同时重启 scc-lite 和 scc-web（曾只重启 scc-lite，白换文件）。
- 验证：N1 实测无 APN 拿到 `10.52.45.7/28`，`ping -I wwan0 223.5.5.5` 3/3，
  关闭后无残留；GitHub commit `2bb84eb`，tag+Release 已建；v0.5.7 tag 已删。

#### <a id="p51"></a>51. v0.5.9 首次打包翻车：`install.sh` 无 `+x` 且漏 `sms_pdu.py`
- 现象：N1 上 `./install.sh` 报 `Permission denied`；手动执行后 scc-lite 启动即崩溃
  `ModuleNotFoundError: No module named 'sms_pdu'`，service 重启循环。
- 根因：tar 包内 `install.sh` 没保留执行权限；`install.sh` 拷贝列表漏了新文件 `sms_pdu.py`。
- 教训：打包前必须 `tar tvzf` 检查权限和文件清单；`install.sh` 的拷贝列表每加新文件都要同步。
  用户纪律升级：**先说清楚改什么、实机验证通过后才打包**。
- 验证：复核清单 21 文件、`install.sh` 为 `-rwxrwx---` 后才重打。

#### <a id="p52"></a>52. PDU 模式丢长短信：`+CMGL` 正则过严
- 现象：文本模式能收到长短信（分多条显示），切 PDU 模式后新到的长短信直接"消失"，
  模块里查不到、库里也没有。
- 根因：`sms_list_pdu()` 的正则 `\+CMGL:\s*(\d+),(\d+),,\s*(\d+)` 强制要求 alpha 字段为空，
  带 alpha 的头行匹配不上，被 `i += 1` 连 PDU 行一起静默跳过。
- 教训：解析循环**不许静默丢行**——没匹配上的行必须打日志；解析失败的条目保留
  （`decoded=None`），由调用方决定不删除、不入库。
- 验证：重写后 `pdu_test.py` 三轮文本/PDU 计数 1/1、5/5、10/10 完全一致，解析失败 0；
  ref=182（3/3，142 字）、ref=32（5/5，284 字）完整重组。

#### <a id="p53"></a>53. 只读验收流程（用户定的闸门）
- 现象：无（流程记录）。
- 过程：先搭只读 PDU 测试环境，指令实测通过后再更新软件。
  `pdu_test.py` 只读：不写库、不删模块短信，结束切回文本模式。
  N1 流程：停 scc-lite 释锁 → 跑脚本记基线 → 发普通短信 → 重跑确认 →
  发长短信（中文>70字）→ 重跑确认分片 ref/total/seq 一致且合并正确 → 通过才部署。
- 教训：**先搭测试环境、再动生产代码**；这个流程后来成了 v0.5.9 的发布闸门。

#### <a id="p54"></a>54. `_parse_udh` 截断崩溃路径（本地复现、未在 N1 触发）
- 现象：本地复现 `_parse_udh(bytes([0x00,0x03,0x27]))` 在 `ie_data[2]` 抛 `IndexError`；
  `decode_pdu` 只包了 `bytes.fromhex` 的 try，后续解析无兜底。
- 根因：UDH IE 数据被截断时长度检查缺失。
- 教训：`decode_pdu` 拆为安全外壳 + `_decode_deliver_pdu` 内函数，任何畸形 PDU 只返回 None 永不抛异常；
  爆炸半径要想清楚——`prime()` 只捕 `ModemError`，抛异常会导致 systemd 崩溃循环
  （`is-active` 照样显示 active，最具迷惑性）。
- 验证：本地 8 项全过（含 300 次随机截断 fuzz）；N1 单独替换 `sms_pdu.py` 复现验证通过后才打包。

#### <a id="p55"></a>55. "10086 长短信收不到"真因：chan_quectel 截胡（本节详见第三章专题）
- 现象：用户让 10086 发长短信，模块/入库均未收到；一度怀疑 `_parse_udh` 崩溃。
- 根因：不是代码问题——chan_quectel 把短信事件直接接走（`+CMT` 直达，不进模块存储），
  还开了自动删除。SCC-lite 的 `AT+CMGL` 轮询永远看不见。
- 教训：**排障先分清"没到模块"还是"到了没处理"**：`AT+CMGL` 直连模块看存储，
  存储里没有 = 上游问题，不要先怀疑自己代码。这次分诊顺序反了，白查半天解析器。
- 验证：`quectel.conf` 设 `disablesms=yes` 后，联合验证 1 小时无截胡（详见第三章）。

#### <a id="p56"></a>56. v0.5.9 发布
- 现象：无（成功记录）。
- 内容：PDU 模式收短信、长短信按 UDH 重组入库、`_parse_udh` 加固、chan_quectel 短信关闭。
- 验证：端到端——清模块→只起 scc-lite→手机发长短信→DB 新增一行 113 字完整入库，
  分片自动从模块删除；asterisk+scc-lite 全开下复测正常。
- 发布：GitHub commit `cd1a5dda`，tag v0.5.9，Release 已建；
  tarball 附件需用户手动上传（uploads.github.com 不在 token scope 内）。

### 2026-10-12

#### <a id="p57"></a>57. `at+qdownload` 在 EC20-CE 上返回 ERROR
- 现象：想进 9008 下载模式，发 `at+qdownload` 返回 ERROR，`lsusb` 无 `05c6:9008`。
- 根因：`at+qdownload` 是 EC200U/EG91xU 等新系列的命令，EC20-CE 的 R06A05 根本不支持；
  官方文档里 EC20 进 9008 的方法只有 USB_BOOT 引脚短接。
- 教训：EC20 在 Linux 下刷机靠 QFirehose 经 DM 口自己切 EDL 模式，不用 AT 命令进下载模式。
- 验证：QFirehose 日志显示 DIAG 切 EDL 成功，重枚举出 `05c6:9008`。

#### <a id="p58"></a>58. QFirehose 被 glibc fortify 误杀（`buffer overflow detected`）
- 现象：QFirehose 跑到 programmer 握手返回 "Supported Functions" 后，
  报 `*** buffer overflow detected ***` 被杀掉。
- 根因：glibc 的 fortify 机制拦截了 QFirehose 自身的栈溢出——溢出是 benign 的，
  crash 点在握手之后、第一个写 flash 命令之前，模块 NAND 没被碰过。
- 教训：fortify 报警不等于致命；`gcc -O2 -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0 -fno-stack-protector`
  关掉重编。先认"工具坏了不是模块坏了"，再动手。
- 验证：重编后一次跑完，22 秒刷写完成。

#### <a id="p59"></a>59. Sahara 报错：模块停在 EDL 里跑着旧 programmer
- 现象：第二次跑 QFirehose 报 Sahara 超时/垃圾数据。
- 根因：run1 crash 后模块还停在 EDL 里，跑着 run1 上传的 programmer（在 RAM 里），
  它只懂 firehose 协议，工具却跟它说 Sahara 语，协议错配。
- 教训：断电重插 USB（programmer 跑在 RAM 里，断电就没了），再跑就干净了。
- 验证：断电重插后一次成功。

#### <a id="p60"></a>60. R06A15 上 `uac_fs=1` 让 UAC 完全不可用
- 现象：刷机后按旧习惯设 `at+qaudcfg="uac_fs",1`，UAC 声卡直接不可用。
- 根因：R06A15 上该参数行为变化（changelog 写 R06A05 起默认值改为 0）。
- 教训：**硬结论——R06A15 上 `uac_fs` 保持 0，以后不碰这个参数。**
- 验证：改回 0 后 `arecord -l` 见 `card 1: Android`，UAC 正常。

#### <a id="p61"></a>61. R06A15 出厂 usbcfg 默认关 UAC
- 现象：刷机后 `arecord -l` 看不到 Android 声卡。
- 根因：R06A15 出厂 `usbcfg` 最后一位是 0（UAC 关），旧固件上是手动开到 1 的。
- 教训：刷机后必须把 `AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,0,1` 写回去，
  再 `at+cfun=1,1` 重启让 USB 重枚举。
- 验证：重启后声卡回来；N1 重启后 SCC 短信+上网验证正常。

#### <a id="p62"></a>62. 刷机前备份 AT 配置
- 现象：无（流程记录）。
- 过程：停 scc-lite 释放 ttyUSB3，导出 `at+cgmr` / `ati` / `at+cgmm` / `at+cimi` /
  `usbcfg` / `ims` / `usbnet` / `urcport` / `uac_fs` / `qmbncfg list` 存档
  `~/fw/backup-20261012.txt`（796 字节）。
- 教训：刷机前备份是铁律；备份时发现当时插的是广电卡（IMSI 46015 开头），
  跟记录里的移动卡对不上——动手前先确认现状跟记录是否一致。
- 验证：刷后逐项回写，重配清单与备份一致。

### 2026-10-13

#### <a id="p63"></a>63. 前端 18 处调用未定义 JS 函数，数据页交互全灭
- 现象：v0.6.0 首版 N1 实测，数据页拨号步骤无输出、关闭无反应、连通性测试卡死、
  AT 终端不能发送——所有交互全灭。
- 根因：v0.6.0 重写时 18 处调用了 3 个从未定义的 JS 函数
  （`dataMod`×12、`modQ`×6、`atMod`×3）。
  `node --check` 只查语法，查不出运行时 `ReferenceError`。
- 教训：前端打包前必须加"未定义函数调用"扫描——提函数**定义集合**与**调用集合**
  做差集（含普通函数与箭头函数）。`node --check` 通过 ≠ 能运行。
- 验证：补三个函数定义后本地扫描零遗漏，N1 重装后数据页交互恢复。

#### <a id="p64"></a>64. 短信"丢失"：老短信 module_id 为空被过滤隐藏
- 现象：v0.6.0 上线后用户报"短信中心短信丢失了"。
- 根因：v0.5.9 的老短信 `module_id` 为空（新字段默认值），v0.6.0 短信中心按模块
  过滤（`WHERE module_id=?`），老短信查不出来——**非真删，数据全在库里**。
- 教训：加过滤维度时，老数据的空值必须有迁移/回填/兜底三选一，不能直接过滤。
  诊断先行：
  ```bash
  sqlite3 /opt/scc-lite-for-EC20-4g-module/data/scc-lite.db \
    "SELECT COALESCE(NULLIF(module_id,''),'(空)'), COUNT(*) FROM sms GROUP BY 1;"
  ```
- 验证：`Store.backfill_module_id()` 单模块自动回填（幂等），N1 上老短信重现。

#### <a id="p65"></a>65. 仪表盘运营商无显示：dev.get("data") 误取
- 现象：仪表盘 4G 模块卡运营商空白，但"设备管理"页运营商正确。
- 根因：`_cache_get()` 直接返回 data 字典，而 `/api/dashboard` 写了
  `dev.get("data", {})`——永远拿到空字典。设备管理页正常是因为它直接
  `jsonify(data)` 没套这一层。
- 教训：缓存读写两端的数据结构必须一致；"A 页正常 B 页不正常"时，
  先对比两页取数路径的差异，而不是怀疑数据源。
- 验证：改为直接用 `dev` 后，N1 仪表盘运营商正常显示。

#### <a id="p66"></a>66. 号码管理"加载中"：get_store() 返回原生连接非 Store
- 现象：号码管理弹窗一直"加载中…"，打不开。
- 根因：`scc-web.py` 的 `get_store()` 返回原生 `sqlite3.Connection`，
  不是 `scc-lite.py` 的 `Store` 对象；新接口调了 `db.get_sim_numbers()` 等
  Store 方法，直接 500，前端静默卡死。
- 教训：同一项目里"同名函数返回不同类型"是大坑。新接口必须跟所在文件的
  现有模式一致（scc-web.py 里一律用 `db.execute` 原生 SQL），或显式 import
  Store。前端 `api()` 遇到非 200 应有错误提示，不能静默转圈。
- 验证：改原生 SQL 后 N1 弹窗正常加载。

#### <a id="p67"></a>67. 弹窗写在 hidden section 内，仪表盘点击无反应
- 现象：仪表盘点"未设号码，点击填写"没反应；切到短信中心后弹窗突然冒出来。
- 根因：`#sim-modal` 的 HTML 写在 `#t-sms`（`class="hidden"`）section 里面——
  父级隐藏时子级 `display:flex` 也不可见。
- 教训：全局弹窗一律放顶层（跟 account-modal 放一起），不许嵌在任何 tab
  section 内。`position:fixed` 不等于脱离父级 `display:none` 的影响。
- 验证：移顶层后 N1 上仪表盘/短信中心均可直接调出。

#### <a id="p68"></a>68. 打包漏文件：手写子集漏 7 个，全新安装必挂
- 现象：重打的包只有 90K（之前全量 362K），用户一眼发现不对。
- 根因：打包时手写了文件子集，漏了 `notifications.py`、`ec20_data.py`、
  `at_cheatsheet.py`、`qq_receiver.py`、`apns-conf.xml`、`config.yaml.example`、
  `scc-lite.service`、`scc-web.service`。覆盖升级不受影响（旧文件还在），
  但全新安装跑 `install.sh` 的 `cp` 必报错。
- 教训：打包文件清单必须以 `install.sh` 的拷贝列表为准，逐个核对；
  包大小突变（362K→90K）是第一信号，打包后先看大小再上传。
- 验证：重打全量 24 文件 385K，逐个核对 `install.sh` 所需齐全，
  `tar tvzf` 确认 `install.sh`/`udhcpc-noroute.sh` 保留 `+x`。

#### <a id="p69"></a>69. 架构决策：短信按 IMSI（SIM）关联，不按 IMEI（硬件）
- 背景：多模块场景下 SIM 卡会对调（A 卡插 B 模块），按 IMEI 关联短信会错乱。
- 决策：IMSI 跟 SIM 卡走——短信存 `imsi` 字段，按 IMSI 查/分组；
  用户维护 `sim_numbers`（IMSI→号码，多 IMSI 可同号支持补卡）；
  换卡零操作，映射表不用动。
- 实现要点：worker 每轮跟大轮询读 `AT+CIMI`（不额外增加读取机制），
  3 轮滑动窗口容错（≤1 失败用缓存，≥2 失败置空），SIM 热插拔自动跟上；
  取消 `AT+CNUM`（国内卡取不到，实测多卡均为 empty/ERROR）。
- 验证：N1 单卡收发、号码管理填号、按号码过滤均正常。

#### <a id="p70"></a>70. 信号两级缓存：AT+CSQ 与重型扫描解耦
- 现象：数据页信号灰色"无信号"等很久才出；仪表盘信号迟到，感觉突兀。
- 根因：信号（`AT+CSQ` 单命令 <1s）被绑在 7 命令重型设备扫描（5-10s）里，
  60 秒才刷一次；前端只加载一次不轮询，缓存未命中就灰色到底。
- 关键认知（用户点出）：**信号是无线电测量，与拨号状态无关，一直有**——
  不该让用户觉得"拨号后才有信号"。
- 修法：`signal:{imei}` 轻量缓存 20s 刷 + `/api/signal` 接口；
  数据页 10s / 仪表盘 15s 轮询；`sigBars` 区分 null=加载中（"…"）与
  99=modem 明确无信号。
- 验证：N1 上打开页面信号秒出，10-15s 跳动更新，拨号前后一致。

---

## 三、专题：PDU 长短信重组与 chan_quectel 截胡

> 本节由 `docs/asterisk-chan_quectel-sms-interception.md` 合并而来，原文件已删除，内容全数并入此处。

### 症状

Asterisk + SCC-lite 同跑时，短信偶发"人间蒸发"：`AT+CMGL` 查不到（没进模块存储），
SCC-lite 库里也没有。用户让 10086 发长短信，模块和入库双双没有。

### 实锤

Asterisk 日志出现：

```text
Channel 'Local/sms@dongle-incoming-...' sent to invalid extension (...,dongle-incoming,sms,1)
```

`module show like quectel` 确认 `chan_quectel.so` Running（`chan_dongle` 显示 0 个），
是 **chan_quectel** 拦截了短信事件。

### 根因

```text
Direct Message : On    ← +CMT 直接送 chan_quectel，不进模块存储
Auto delete SMS : Yes  ← 读完即删
```

"截胡 + 销毁"二连：短信以 `+CMT` 直达 chan_quectel，根本不进模块存储，
SCC-lite 的 `AT+CMGL` 轮询永远看不见。

### 修复

`/etc/asterisk/quectel.conf`：`disablesms=no` → `yes`
（N1 用的 IchthysMaranatha 版保留此选项；新版上游文档说已移除，别被误导）。
重启 asterisk，`quectel show device settings quectel0` 确认 `Disable SMS: Yes`。
语音不受影响，还顺带避掉"通话中来短信搞崩 chan_quectel"的已知 bug。

### 走过的弯路

1. 误判"dialplan 没接住，短信一定还在模块里"——错，`+CMT` 直达的不进存储。
2. 先怀疑自己代码，查 `_parse_udh` 崩溃路径半天；诊断（零重启、无 poll error）排除后
   才回头查截胡。**分诊顺序反了：应先直连 AT 看模块存储，存储里没有 = 上游问题。**
3. "让 10086 发短信"测试法含糊：用自己手机发，回复只到自己手机，测了个寂寞。
   测试短信必须发到**模块号码**。
4. "没收到"有两种：分片被 daemon 收走但不齐（查 journal `concat part` + DB 新行），
   vs 压根没到模块——先分清再动手。

### 验证

全开状态（asterisk + scc-lite）收长短信：SCC-lite 正常合并入库；
`journalctl -u asterisk` 近 1 小时 + `/var/log/asterisk/messages` 均无
`sms@` / `invalid extension` warning。chan_quectel 未再碰短信，专题闭环。

### PDU 重组实现要点（v0.5.9）

- `sms_pdu.py`：3GPP 23.040 SMS-DELIVER 解析，UDH 取 ref/total/seq，支持 GSM7/UCS2，
  8-bit 和 16-bit ref；MTI 检查只收 DELIVER；全函数异常兜底返回 None。
- `modem.py::sms_list_pdu()`：`AT+CMGF=0` 后 `AT+CMGL=4` 读取，停在 PDU 模式不再切回
  （发送本就显式 `at+cmgf=1`，`AT+CMGD` 与模式无关，每轮省 1 条 AT）。
- `scc-lite.py`：按 `(sender, ref)` 缓存分片，收齐 `1..total` 按序合并存一条；
  24 小时未收齐按 `[分片不完整 x/y]` 存；启动 `prime()` 只做去重标记，不重组旧分片。
- 已知局限：分片缓冲在内存，daemon 重启会丢未收齐的分片（后续可改 SQLite 持久化）。

---

## 四、铁律与纪律汇总

1. **蜂窝数据铁律**：绝不给 `wwan0` 加默认路由；连通性测试只用 `ping -I wwan0` 或明细路由。
2. **软件更新纪律**：先说清楚改什么、经确认再动手；新代码必须先用实机终端命令验证通过，
   展示结果后才打包；版本号按序递增，只改相关文件。
3. **打包检查**：Python `py_compile`、JS `node --check`；tar 内脚本保留 `+x` 且拷贝列表无遗漏；
   `tar tvzf` 复核权限和文件清单。
4. **指令规范**：完整可复制粘贴，不许占位符；多行用 `{ ... }` 包裹；标题用 `echo` 不用 `#`；
   花括号内禁 `exit`（会杀掉 SSH）；`awk -F"'"` 取值，不用 `grep -oP`；下载后 `ls -l` 确认。
5. **串口红线**：Asterisk 占 ttyUSB1/2，短信用 ttyUSB3；手动操作 ttyUSB3 前
   `systemctl stop scc-lite` 释放串口锁。
6. **AT 调测**：命令统一小写；模块无响应时断电硬重启（`AT+CFUN=1,1` 不够）；
   MBN 保持自动，不要手动切。
7. **排障分诊**：先直连 AT 看模块存储——存储里没有 = 上游问题；先分清"没到模块"
   还是"到了没处理"，再决定查哪边。
8. **发布流程**：GitHub commit+tag+Release；tarball 附件用户手动上传
  （uploads.github.com 不在 token scope 内）；旧版本 tag/Release 及时清理。
