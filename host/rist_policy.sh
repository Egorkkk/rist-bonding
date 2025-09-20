#!/usr/bin/env bash
set -euo pipefail

# ===== CONFIG =====
SRV="83.222.26.3"
SRV_PORT="8000"

# VIPs listened by socat and targeted by ristsender
VIPS=(10.255.0.1 10.255.0.2 10.255.0.3 10.255.0.4)

# System users for per-path routing (one per modem)
USERS=(rist1 rist2 rist3 rist4)

# Modems
IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.199";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

# Route tables
N1="modem1"; T1=100
N2="modem2"; T2=101
N3="modem3"; T3=102
N4="modem4"; T4=103

# Our mangle chain for owner->mark
MCHAIN="RIST_OWNER"
# ==================

msg(){ echo "[rist-policy] $*"; }

need_root(){ [ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }; }

ensure_bins(){
  command -v ip >/dev/null       || { echo "iproute2 required"; exit 1; }
  command -v iptables >/dev/null || { echo "iptables required"; exit 1; }
}

ensure_users(){
  for u in "${USERS[@]}"; do
    id -u "$u" >/dev/null 2>&1 || useradd --system --no-create-home "$u"
  done
}

ensure_vips(){
  for vip in "${VIPS[@]}"; do
    ip addr show dev lo | grep -q " $vip/" || ip addr add "$vip/32" dev lo
  done
  ip route replace 10.255.0.0/24 dev lo proto static
}

write_rt_tables(){
  mkdir -p /etc/iproute2
  if [ -d /etc/iproute2/rt_tables.d ]; then
    cat > /etc/iproute2/rt_tables.d/99-rist-bonding.conf <<EOF
$T1 $N1
$T2 $N2
$T3 $N3
$T4 $N4
EOF
  else
    local f=/etc/iproute2/rt_tables
    touch "$f"
    sed -i -E "/^($T1|$T2|$T3|$T4)[[:space:]]+/d" "$f"
    printf "%s %s\n" "$T1" "$N1" >>"$f"
    printf "%s %s\n" "$T2" "$N2" >>"$f"
    printf "%s %s\n" "$T3" "$N3" >>"$f"
    printf "%s %s\n" "$T4" "$N4" >>"$f"
  fi
}

set_sysctl(){
  # keep output quiet
  cat >/etc/sysctl.d/99-rist-bonding.conf <<EOF
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.${IF1}.rp_filter = 2
net.ipv4.conf.${IF2}.rp_filter = 2
net.ipv4.conf.${IF3}.rp_filter = 2
net.ipv4.conf.${IF4}.rp_filter = 2
EOF
  sysctl --system >/dev/null 2>&1 || true
}

cleanup_old(){
  msg "cleanup: ip rules"
  # delete our fwmark rules
  for m in 0x10 0x11 0x12 0x13; do
    while ip rule show | grep -q "fwmark $m "; do ip rule del fwmark "$m" >/dev/null 2>&1 || true; done
  done
  # delete any stale 'to 10.255.0.x' / 'from 192.168.x.x'
  for p in $(ip rule show | awk '/to 10\.255\.0\.|from 192\.168\./{print $1}'); do
    ip rule del pref "$p" >/dev/null 2>&1 || true
  done

  msg "cleanup: route tables"
  ip route flush table "$N1" >/dev/null 2>&1 || ip route flush table "$T1" >/dev/null 2>&1 || true
  ip route flush table "$N2" >/dev/null 2>&1 || ip route flush table "$T2" >/dev/null 2>&1 || true
  ip route flush table "$N3" >/dev/null 2>&1 || ip route flush table "$T3" >/dev/null 2>&1 || true
  ip route flush table "$N4" >/dev/null 2>&1 || ip route flush table "$T4" >/dev/null 2>&1 || true

  msg "cleanup: iptables chains"
  # remove our jumps and chains if present (both mangle and nat just in case)
  for table in mangle nat; do
    for ch in RIST_MARK RIST_DNAT RIST_OWNER; do
      iptables -t "$table" -C OUTPUT -j "$ch" >/dev/null 2>&1 && iptables -t "$table" -D OUTPUT -j "$ch" || true
      iptables -t "$table" -F "$ch" >/dev/null 2>&1 || true
      iptables -t "$table" -X "$ch" >/dev/null 2>&1 || true
    done
  done
}

routes_per_table(){
  # link + default with src in each modem table
  ip route replace "$NET1" dev "$IF1" table "$N1" proto kernel scope link src "$IP1"
  ip route replace default via "$GW1" dev "$IF1" src "$IP1" table "$N1"

  ip route replace "$NET2" dev "$IF2" table "$N2" proto kernel scope link src "$IP2"
  ip route replace default via "$GW2" dev "$IF2" src "$IP2" table "$N2"

  ip route replace "$NET3" dev "$IF3" table "$N3" proto kernel scope link src "$IP3"
  ip route replace default via "$GW3" dev "$IF3" src "$IP3" table "$N3"

  ip route replace "$NET4" dev "$IF4" table "$N4" proto kernel scope link src "$IP4"
  ip route replace default via "$GW4" dev "$IF4" src "$IP4" table "$N4"
}

apply_uid_rules() {
  # получаем UID’ы из системы
  U1=$(id -u rist1); U2=$(id -u rist2); U3=$(id -u rist3); U4=$(id -u rist4)
  # чистим старые
  for p in $(ip rule show | awk '/uidrange/{print $1}'); do ip rule del pref "$p" 2>/dev/null || true; done
  # ставим новые (приоритет ниже цифра — выше приоритет)
  ip rule add uidrange ${U1}-${U1} lookup modem1 pref 5001
  ip rule add uidrange ${U2}-${U2} lookup modem2 pref 5002
  ip rule add uidrange ${U3}-${U3} lookup modem3 pref 5003
  ip rule add uidrange ${U4}-${U4} lookup modem4 pref 5004
}

apply_owner_mark(){
  # create chain and single jump from OUTPUT
  iptables -t mangle -N "$MCHAIN" >/dev/null 2>&1 || true
  iptables -t mangle -C OUTPUT -j "$MCHAIN" >/dev/null 2>&1 || iptables -t mangle -I OUTPUT 1 -j "$MCHAIN"

  # ensure no duplicates of the jump (leave the first one)
  mapfile -t _J < <(iptables -t mangle -L OUTPUT --line-numbers | awk -v c="$MCHAIN" '$0 ~ ("jump " c) {print $1}' | sort -rn)
  if [ "${#_J[@]}" -gt 1 ]; then
    for ((i=0;i<${#_J[@]}-1;i++)); do iptables -t mangle -D OUTPUT "${_J[$i]}" >/dev/null 2>&1 || true; done
  fi

  iptables -t mangle -F "$MCHAIN"
  iptables -t mangle -A "$MCHAIN" -m owner --uid-owner "${USERS[0]}" -j MARK --set-mark 0x10 -m comment --comment "owner ${USERS[0]} -> modem1"
  iptables -t mangle -A "$MCHAIN" -m owner --uid-owner "${USERS[1]}" -j MARK --set-mark 0x11 -m comment --comment "owner ${USERS[1]} -> modem2"
  iptables -t mangle -A "$MCHAIN" -m owner --uid-owner "${USERS[2]}" -j MARK --set-mark 0x12 -m comment --comment "owner ${USERS[2]} -> modem3"
  iptables -t mangle -A "$MCHAIN" -m owner --uid-owner "${USERS[3]}" -j MARK --set-mark 0x13 -m comment --comment "owner ${USERS[3]} -> modem4"

  # fwmark -> tables
  ip rule add fwmark 0x10 table "$N1" pref 9001
  ip rule add fwmark 0x11 table "$N2" pref 9002
  ip rule add fwmark 0x12 table "$N3" pref 9003
  ip rule add fwmark 0x13 table "$N4" pref 9004
}

flush_conntrack_udp(){
  command -v conntrack >/dev/null 2>&1 || return 0
  for vip in "${VIPS[@]}"; do
    conntrack -D -p udp --orig-dst "$vip" --dport "$SRV_PORT" >/dev/null 2>&1 || true
  done
  conntrack -D -p udp --orig-dst "$SRV" --dport "$SRV_PORT" >/dev/null 2>&1 || true
}

main(){
  need_root
  ensure_bins
  msg "users"
  ensure_users
  msg "vips on lo"
  ensure_vips
  msg "rt_tables"
  write_rt_tables
  msg "sysctl rp_filter=2"
  set_sysctl
  msg "cleanup old"
  cleanup_old
  msg "routes per table"
  routes_per_table
  apply_uid_rules
  msg "owner->mark and fwmark->tables"
  apply_owner_mark
  msg "flush conntrack udp"
  flush_conntrack_udp
  msg "done"

  echo
  echo "Checks:"
  echo "  ip addr show lo | egrep '10\\.255\\.0\\.'"
  echo "  iptables -t mangle -vnL $MCHAIN"
  echo "  ip rule show | egrep 'fwmark 0x1[0-3]'"
  echo "  ip route show table $N1 ; ip route show table $N2 ; ip route show table $N3 ; ip route show table $N4"
}

main "$@"
