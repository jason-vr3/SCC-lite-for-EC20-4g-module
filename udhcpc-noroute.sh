#!/bin/sh
# udhcpc-noroute.sh - SCC-lite 专用 DHCP 脚本 (v0.5.8)
#
# 铁律: 只给 wwan0 配置 IP/MTU, 绝不添加任何路由 (尤其默认路由),
#       绝不修改系统 DNS (/etc/resolv.conf)。
# DNS 只写入 /run/udhcpc-wwan0.dns 供 scc-lite 读取展示。
#
# udhcpc 传入的环境变量 (busybox):
#   $1 = bound | renew | deconfig | nak
#   $interface  网卡名 (wwan0)
#   $ip         获取到的 IPv4
#   $subnet     子网掩码 (点分十进制, 如 255.255.255.252)
#   $mask       CIDR 位数 (部分版本直接给位数)
#   $mtu        MTU (可能为空)
#   $dns        DNS 列表 (空格分隔)
#   $router     网关 (故意忽略, 不加路由)

DNS_FILE="/run/udhcpc-wwan0.dns"

mask_to_cidr() {
    # 点分掩码 -> CIDR 位数, 如 255.255.255.252 -> 30
    # 非法掩码 (非 4 段 / 含非掩码值 / 1 与 0 不连续) 返回非 0
    local m="$1" cidr=0 octet n=0 phase=0 add
    for octet in $(echo "$m" | tr '.' ' '); do
        n=$((n + 1))
        [ "$n" -gt 4 ] && return 1
        case "$octet" in
            255) add=8 ;; 254) add=7 ;; 252) add=6 ;; 248) add=5 ;;
            240) add=4 ;; 224) add=3 ;; 192) add=2 ;; 128) add=1 ;;
            0)   add=0 ;;
            *)   return 1 ;;
        esac
        # 连续性: 255 只能在开头; 部分位(1~7)只能出现一次且其后全为 0
        if [ "$add" -eq 8 ]; then
            [ "$phase" -eq 0 ] || return 1
        elif [ "$add" -gt 0 ]; then
            [ "$phase" -eq 0 ] || return 1
            phase=1
        else
            [ "$phase" -eq 0 ] && phase=2
        fi
        cidr=$((cidr + add))
    done
    [ "$n" -eq 4 ] || return 1
    echo "$cidr"
}

case "$1" in
    bound|renew)
        [ -n "$ip" ] || exit 1
        # 确定前缀长度: 优先用 $mask (位数), 否则把 $subnet 点分掩码换算
        CIDR=""
        if [ -n "$mask" ]; then
            case "$mask" in
                ''|*[!0-9]*) ;;
                *) [ "$mask" -ge 0 ] 2>/dev/null && [ "$mask" -le 32 ] 2>/dev/null && CIDR="$mask" ;;
            esac
        fi
        if [ -z "$CIDR" ] && [ -n "$subnet" ]; then
            CIDR=$(mask_to_cidr "$subnet") || CIDR="32"
        fi
        [ -n "$CIDR" ] || CIDR="32"

        ip -4 addr flush dev "$interface"
        ip -4 addr add "$ip/$CIDR" dev "$interface"
        [ -n "$mtu" ] && ip link set dev "$interface" mtu "$mtu"
        ip link set "$interface" up

        # DNS 落盘备查, 不动 /etc/resolv.conf
        if [ -n "$dns" ]; then
            # shellcheck disable=SC2086
            printf '%s\n' $dns > "$DNS_FILE"
        else
            rm -f "$DNS_FILE"
        fi
        ;;
    deconfig|nak)
        ip -4 addr flush dev "$interface"
        rm -f "$DNS_FILE"
        ;;
esac
exit 0
