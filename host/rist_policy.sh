#!/usr/bin/env bash
set -euo pipefail
# set -x   # включить при отладке

# ====== НАСТРОЙКИ ======
LP1=40010; LP2=40011; LP3=40012; LP4=40013
DST=83.222.26.3
DPORT=8000

IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.198";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

# Номера таблиц (для /etc/iproute2/rt_tables)
T1=100; T2=101; T3=102; T4=103

# Метки для ristsender по UID
M1=0x10; M2=0x11; M3=0x12; M4=0x13
U1=972;  U2=971;  U3=970;  U4=969

# Имена таблиц
N1="modem1"; N2="modem2"; N3="modem3"; N4="modem4"
# =======================

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root." >&2; exit 1
  fi
}

write_rt_tables_file() {
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
    printf "%s %s\n" "$T1" "$N1" >> "$f"
    printf "%s %s\n" "$T2" "$N2" >> "$f"
    printf "%s %s\n" "$T3" "$N3" >> "$f"
    printf "%s %s\n" "$T4" "$N4" >> "$f"
  fi
}

# LOOSE rp_filter (=2) на интерфейсах + «all»
write_sysctl() {
  cat >/etc/sysctl.d/99-rist-bonding.conf <<EOF
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.${IF1}.rp_filter = 2
net.ipv4.conf.${IF2}.rp_filter = 2
net.ipv4.conf.${IF3}.rp_filter = 2
net.ipv4.conf.${IF4}.rp_filter = 2
EOF
  sysctl --system >/dev/null
}

add_snat_rule() {
  local uid="$1" iface="$2" ip="$3" lport="$4"
  # SNAT для именно этого назначения/порта и интерфейса
  iptables -t nat -C POSTROUTING -p udp -d "$DST" --dport "$DPORT" -m owner --uid-owner "$uid" -o "$iface" \
    -j SNAT --to-source "$ip:$lport" 2>/dev/null || \
  iptables -t nat -A POSTROUTING -p udp -d "$DST" --dport "$DPORT" -m owner --uid-owner "$uid" -o "$iface" \
    -j SNAT --to-source "$ip:$lport"
}

del_snat_rule() {
  local uid="$1" iface="$2" ip="$3" lport="$4"
  while iptables -t nat -C POSTROUTING -p udp -d "$DST" --dport "$DPORT" -m owner --uid-owner "$uid" -o "$iface" \
        -j SNAT --to-source "$ip:$lport" 2>/dev/null; do
    iptables -t nat -D POSTROUTING -p udp -d "$DST" --dport "$DPORT" -m owner --uid-owner "$uid" -o "$iface" \
      -j SNAT --to-source "$ip:$lport" || true
  done
}


cleanup_policy() {
  echo "[*] Cleanup: ip rule (fwmark)…"
  while read -r MARK TABN TABNAME; do
    while ip rule show | grep -Eq "fwmark[[:space:]]+$MARK.*lookup[[:space:]]+($TABN|$TABNAME)"; do
      ip rule del fwmark "$MARK" table "$TABNAME" 2>/dev/null || ip rule del fwmark "$MARK" table "$TABN" || true
    done
  done <<EOF
$M1 $T1 $N1
$M2 $T2 $N2
$M3 $T3 $N3
$M4 $T4 $N4
EOF

  echo "[*] Cleanup: ip rule (from source)…"
  while read -r SRC TABN TABNAME; do
    while ip rule show | grep -Eq "from[[:space:]]+$SRC[[:space:]].*lookup[[:space:]]+($TABN|$TABNAME)"; do
      ip rule del from "$SRC" table "$TABNAME" 2>/dev/null || ip rule del from "$SRC" table "$TABN" || true
    done
  done <<EOF
$IP1 $T1 $N1
$IP2 $T2 $N2
$IP3 $T3 $N3
$IP4 $T4 $N4
EOF

  echo "[*] Cleanup: route tables…"
  ip route flush table "$N1" || ip route flush table "$T1" || true
  ip route flush table "$N2" || ip route flush table "$T2" || true
  ip route flush table "$N3" || ip route flush table "$T3" || true
  ip route flush table "$N4" || ip route flush table "$T4" || true

  echo "[*] Cleanup: iptables OUTPUT rules…"
  iptables_del_all_matching "-m owner --uid-owner $U1 -j MARK --set-mark $M1"
  iptables_del_all_matching "-m owner --uid-owner $U2 -j MARK --set-mark $M2"
  iptables_del_all_matching "-m owner --uid-owner $U3 -j MARK --set-mark $M3"
  iptables_del_all_matching "-m owner --uid-owner $U4 -j MARK --set-mark $M4"
  iptables_del_all_matching "-m owner --uid-owner $U1 -j CONNMARK --save-mark"
  iptables_del_all_matching "-m owner --uid-owner $U2 -j CONNMARK --save-mark"
  iptables_del_all_matching "-m owner --uid-owner $U3 -j CONNMARK --save-mark"
  iptables_del_all_matching "-m owner --uid-owner $U4 -j CONNMARK --save-mark"
  
  echo "[*] Cleanup: NAT SNAT rules…"
  del_snat_rule "$U1" "$IF1" "$IP1" "$LP1"
  del_snat_rule "$U2" "$IF2" "$IP2" "$LP2"
  del_snat_rule "$U3" "$IF3" "$IP3" "$LP3"
  del_snat_rule "$U4" "$IF4" "$IP4" "$LP4"
}

iptables_del_all_matching() {
  local rule="$1"
  while iptables -t mangle -C OUTPUT $rule 2>/dev/null; do
    iptables -t mangle -D OUTPUT $rule || true
  done
}

get_iface_ip() {
  local dev="$1"
  ip -o -4 addr show dev "$dev" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1
}

# connected-route в таблицу (с src, если интерфейс уже поднят)
add_link_route() {
  local table_name="$1" subnet="$2" dev="$3"
  local actual_src; actual_src="$(get_iface_ip "$dev" || true)"
  if [[ -n "${actual_src:-}" ]]; then
    ip route replace "$subnet" dev "$dev" table "$table_name" proto kernel scope link src "$actual_src"
  else
    ip route replace "$subnet" dev "$dev" table "$table_name" proto kernel scope link
  fi
}

# ВАЖНО: default с явным src, чтобы ответ шёл с IP интерфейса
add_default_route_src() {
  local table_name="$1" gw="$2" dev="$3" srcip="$4"
  ip route replace default via "$gw" dev "$dev" src "$srcip" table "$table_name"
}

# Добавь рядом с add_ip_rule_from/fwmark:
add_ip_rule_uidrange() {
  local uid="$1" table_name="$2" pref="$3"
  if ip rule help 2>&1 | grep -q '\buidrange\b'; then
    ip rule replace pref "$pref" uidrange "$uid-$uid" table "$table_name"
  else
    echo "WARNING: 'ip rule uidrange' not supported on this system" >&2
  fi
}

add_ip_rule_fwmark() {
  local mark="$1" table_name="$2" pref="$3"
  if ip rule help 2>&1 | grep -q '\breplace\b'; then
    ip rule replace pref "$pref" fwmark "$mark" table "$table_name"
  else
    ip rule show | grep -Eq "fwmark[[:space:]]+$mark.*lookup[[:space:]]+$table_name" \
      || ip rule add pref "$pref" fwmark "$mark" table "$table_name"
  fi
}

add_ip_rule_from() {
  local src="$1" table_name="$2" pref="$3"
  if ip rule help 2>&1 | grep -q '\breplace\b'; then
    ip rule replace pref "$pref" from "$src" table "$table_name"
  else
    ip rule show | grep -Eq "from[[:space:]]+$src[[:space:]].*lookup[[:space:]]+$table_name" \
      || ip rule add pref "$pref" from "$src" table "$table_name"
  fi
}

main() {
  require_root

  echo "[*] rt_tables mapping…"; write_rt_tables_file
  echo "[*] Sysctl (rp_filter=2)…"; write_sysctl

  echo "[*] Cleanup старых правил/маршрутов…"; cleanup_policy

  echo "[*] Policy rules…"
  
  
  echo "[*] Policy rules (uidrange → from → fwmark)…"
  # uidrange — самый высокий приоритет (чтобы src выбрался правильно на connect())
  add_ip_rule_uidrange "$U1" "$N1" 1050
  add_ip_rule_uidrange "$U2" "$N2" 1051
  add_ip_rule_uidrange "$U3" "$N3" 1052
  add_ip_rule_uidrange "$U4" "$N4" 1053

  # from — выше приоритет (меньше pref), fwmark — ниже
  add_ip_rule_from "$IP1" "$N1" 1101
  add_ip_rule_from "$IP2" "$N2" 1102
  add_ip_rule_from "$IP3" "$N3" 1103
  add_ip_rule_from "$IP4" "$N4" 1104

  add_ip_rule_fwmark "$M1" "$N1" 1200
  add_ip_rule_fwmark "$M2" "$N2" 1201
  add_ip_rule_fwmark "$M3" "$N3" 1202
  add_ip_rule_fwmark "$M4" "$N4" 1203

  echo "[*] Routes per table…"
  add_link_route "$N1" "$NET1" "$IF1"
  add_link_route "$N2" "$NET2" "$IF2"
  add_link_route "$N3" "$NET3" "$IF3"
  add_link_route "$N4" "$NET4" "$IF4"

  add_default_route_src "$N1" "$GW1" "$IF1" "$IP1"
  add_default_route_src "$N2" "$GW2" "$IF2" "$IP2"
  add_default_route_src "$N3" "$GW3" "$IF3" "$IP3"
  add_default_route_src "$N4" "$GW4" "$IF4" "$IP4"

  echo "[*] iptables маркировка по UID (+save CONNMARK)…"
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U1" -j MARK --set-mark "$M1"
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U1" -j CONNMARK --save-mark
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U2" -j MARK --set-mark "$M2"
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U2" -j CONNMARK --save-mark
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U3" -j MARK --set-mark "$M3"
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U3" -j CONNMARK --save-mark
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U4" -j MARK --set-mark "$M4"
  iptables -t mangle -A OUTPUT -m owner --uid-owner "$U4" -j CONNMARK --save-mark
  
  echo "[*] NAT: SNAT src IP:PORT per RIST sender…"
  add_snat_rule "$U1" "$IF1" "$IP1" "$LP1"
  add_snat_rule "$U2" "$IF2" "$IP2" "$LP2"
  add_snat_rule "$U3" "$IF3" "$IP3" "$LP3"
  add_snat_rule "$U4" "$IF4" "$IP4" "$LP4"

  echo "[*] Done."
}

main "$@"
