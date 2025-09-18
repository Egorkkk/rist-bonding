#!/usr/bin/env bash
set -euo pipefail

MIST_IP=83.222.26.3

# main
MAIN_DEV=wlp4s0
MAIN_IP=192.168.0.171
MAIN_GW=192.168.0.2
MAIN_METRIC=100

# paths
P1_IF=modem1; P1_IP=192.168.8.198;  P1_GW=192.168.8.1;  P1_T=101; P1_M=0x101; P1_PRIO=1010; P1_UID=rist1
P2_IF=modem2; P2_IP=192.168.14.100; P2_GW=192.168.14.1; P2_T=102; P2_M=0x102; P2_PRIO=1020; P2_UID=rist2
P3_IF=modem3; P3_IP=192.168.38.100; P3_GW=192.168.38.1; P3_T=103; P3_M=0x103; P3_PRIO=1030; P3_UID=rist3
P4_IF=modem4; P4_IP=192.168.11.100; P4_GW=192.168.11.1; P4_T=104; P4_M=0x104; P4_PRIO=1040; P4_UID=rist4

apply_one() {
  local IF=$1 IP=$2 GW=$3 T=$4 M=$5 PRIO=$6 UIDNAME=$7

  # Таблица маршрутизации (default с src)
  ip route replace default via "$GW" dev "$IF" src "$IP" table "$T"

  # Чистим старые ip rule по селекторам (без приоритета)
  while ip rule show | grep -qE "fwmark $M .*lookup $T"; do
    ip rule del fwmark "$M" lookup "$T" || true
  done
  # Ставим правило с нужным приоритетом
  ip rule add fwmark "$M" lookup "$T" priority "$PRIO" 2>/dev/null || true

  # RP-filter off (на интерфейсе пути)
  sysctl -w "net.ipv4.conf.${IF}.rp_filter=0" >/dev/null || true
}

# ---- main default (оставляем Wifi) ----
ip route replace default via "$MAIN_GW" dev "$MAIN_DEV" src "$MAIN_IP" metric "$MAIN_METRIC"

# ---- policy routing для путей ----
apply_one "$P1_IF" "$P1_IP" "$P1_GW" "$P1_T" "$P1_M" "$P1_PRIO" "$P1_UID"
apply_one "$P2_IF" "$P2_IP" "$P2_GW" "$P2_T" "$P2_M" "$P2_PRIO" "$P2_UID"
apply_one "$P3_IF" "$P3_IP" "$P3_GW" "$P3_T" "$P3_M" "$P3_PRIO" "$P3_UID"
apply_one "$P4_IF" "$P4_IP" "$P4_GW" "$P4_T" "$P4_M" "$P4_PRIO" "$P4_UID"

# ---- цепь маркировки по UID ----
iptables -t mangle -N RIST_MARK_OUT 2>/dev/null || true
iptables -t mangle -C OUTPUT -j RIST_MARK_OUT 2>/dev/null || iptables -t mangle -A OUTPUT -j RIST_MARK_OUT

add_owner_mark() {
  local UIDNAME=$1 MARK=$2
  # Удалим дубликаты, если есть
  while iptables -t mangle -C RIST_MARK_OUT -m owner --uid-owner "$UIDNAME" -j MARK --set-mark "$MARK" 2>/dev/null; do
    iptables -t mangle -D RIST_MARK_OUT -m owner --uid-owner "$UIDNAME" -j MARK --set-mark "$MARK" || true
  done
  iptables -t mangle -A RIST_MARK_OUT -m owner --uid-owner "$UIDNAME" -j MARK --set-mark "$MARK"
}
add_owner_mark "$P1_UID" "$P1_M"
add_owner_mark "$P2_UID" "$P2_M"
add_owner_mark "$P3_UID" "$P3_M"
add_owner_mark "$P4_UID" "$P4_M"

ip route flush cache

echo "== DONE =="
echo "ip rule show | egrep '0x10(1|2|3|4)'"
echo "ip route show table 101; ip route show table 102; ip route show table 103; ip route show table 104"
echo "iptables -t mangle -L RIST_MARK_OUT -v -n"
