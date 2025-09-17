#!/usr/bin/env bash
set -euo pipefail
# set -x

# ====== ПАРАМЕТРЫ ======
SRV="83.222.26.3"
SRV_PORT="8000"          # целевой порт на сервере

# DPORT'ы до DNAT (должны совпадать с тем, что в entrypoint/config)
P1=9001; P2=9002; P3=9003; P4=9004

# Интерфейсы/шлюзы/адреса модемов
IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.198";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

# Таблицы
T1=100; T2=101; T3=102; T4=103
N1="modem1"; N2="modem2"; N3="modem3"; N4="modem4"

# Наши цепочки
MCHAIN="RIST_MARK"   # mangle/OUTPUT
NCHAIN="RIST_DNAT"   # nat/OUTPUT
# =======================

root() { [ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }; }

rt_tables() {
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

sysctl_loose() {
  cat >/etc/sysctl.d/99-rist-bonding.conf <<EOF
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.${IF1}.rp_filter = 2
net.ipv4.conf.${IF2}.rp_filter = 2
net.ipv4.conf.${IF3}.rp_filter = 2
net.ipv4.conf.${IF4}.rp_filter = 2
EOF
  sysctl --system >/dev/null
}

# --- ЦЕПОЧКИ IPTABLES ---
ensure_mangle_chain() {
  iptables -t mangle -N "$MCHAIN" 2>/dev/null || true
  if ! iptables -t mangle -C OUTPUT -j "$MCHAIN" 2>/dev/null; then
    iptables -t mangle -I OUTPUT 1 -j "$MCHAIN"
  fi
  # убрать дубликаты прыжка
  while :; do
    mapfile -t L < <(iptables -t mangle -L OUTPUT --line-numbers | awk -v c="$MCHAIN" '$0 ~ ("jump " c) {print $1}' | sort -rn)
    [ "${#L[@]}" -le 1 ] && break
    for i in "${L[@]:0:${#L[@]}-1}"; do iptables -t mangle -D OUTPUT "$i" || true; done
  done
}

ensure_nat_chain() {
  iptables -t nat -N "$NCHAIN" 2>/dev/null || true
  if ! iptables -t nat -C OUTPUT -j "$NCHAIN" 2>/dev/null; then
    iptables -t nat -I OUTPUT 1 -j "$NCHAIN"
  fi
  while :; do
    mapfile -t L < <(iptables -t nat -L OUTPUT --line-numbers | awk -v c="$NCHAIN" '$0 ~ ("jump " c) {print $1}' | sort -rn)
    [ "${#L[@]}" -le 1 ] && break
    for i in "${L[@]:0:${#L[@]}-1}"; do iptables -t nat -D OUTPUT "$i" || true; done
  done
}

rebuild_mangle_mark() {
  ensure_mangle_chain
  iptables -t mangle -F "$MCHAIN"
  # метим по dport (до DNAT), назначение уже реальный SRV
  iptables -t mangle -A "$MCHAIN" -p udp -d "$SRV" --dport "$P1" -j MARK --set-mark 0x10 -m comment --comment "rist p1→modem1"
  iptables -t mangle -A "$MCHAIN" -p udp -d "$SRV" --dport "$P2" -j MARK --set-mark 0x11 -m comment --comment "rist p2→modem2"
  iptables -t mangle -A "$MCHAIN" -p udp -d "$SRV" --dport "$P3" -j MARK --set-mark 0x12 -m comment --comment "rist p3→modem3"
  iptables -t mangle -A "$MCHAIN" -p udp -d "$SRV" --dport "$P4" -j MARK --set-mark 0x13 -m comment --comment "rist p4→modem4"
  # сохраним метку в conntrack (на будущее, если потребуется)
  # iptables -t mangle -A "$MCHAIN" -m mark ! --mark 0x0 -j CONNMARK --save-mark
}

rebuild_nat_dnat() {
  ensure_nat_chain
  iptables -t nat -F "$NCHAIN"
  # Меняем порт назначения на сервере (IP такой же), из 900X -> 8000
  iptables -t nat -A "$NCHAIN" -p udp -d "$SRV" --dport "$P1" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist p1→8000"
  iptables -t nat -A "$NCHAIN" -p udp -d "$SRV" --dport "$P2" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist p2→8000"
  iptables -t nat -A "$NCHAIN" -p udp -d "$SRV" --dport "$P3" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist p3→8000"
  iptables -t nat -A "$NCHAIN" -p udp -d "$SRV" --dport "$P4" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist p4→8000"
}

# --- ПОЛИТИКА/МАРШРУТЫ ---
iface_ip() { ip -o -4 addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1; }

add_link_route() {
  local tbl="$1" net="$2" dev="$3" src; src="$(iface_ip "$dev" || true)"
  if [[ -n "${src:-}" ]]; then
    ip route replace "$net" dev "$dev" table "$tbl" proto kernel scope link src "$src"
  else
    ip route replace "$net" dev "$dev" table "$tbl" proto kernel scope link
  fi
}

add_def_src() { ip route replace default via "$2" dev "$3" src "$4" table "$1"; }

# связываем метки с таблицами
add_fwmark_rules() {
  # сначала снесём старые (если есть)
  for m in 0x10 0x11 0x12 0x13; do
    while ip rule show | grep -q "fwmark $m "; do ip rule del fwmark "$m" 2>/dev/null || true; done
  done
  ip rule add fwmark 0x10 table "$N1" pref 9001
  ip rule add fwmark 0x11 table "$N2" pref 9002
  ip rule add fwmark 0x12 table "$N3" pref 9003
  ip rule add fwmark 0x13 table "$N4" pref 9004
}

main() {
  root
  echo "[*] rt_tables…"; rt_tables
  echo "[*] sysctl (rp_filter=2)…"; sysctl_loose

  echo "[*] flush route tables…"
  ip route flush table "$N1" || ip route flush table "$T1" || true
  ip route flush table "$N2" || ip route flush table "$T2" || true
  ip route flush table "$N3" || ip route flush table "$T3" || true
  ip route flush table "$N4" || ip route flush table "$T4" || true

  echo "[*] build per-table routes…"
  add_link_route "$N1" "$NET1" "$IF1"; add_def_src "$N1" "$GW1" "$IF1" "$IP1"
  add_link_route "$N2" "$NET2" "$IF2"; add_def_src "$N2" "$GW2" "$IF2" "$IP2"
  add_link_route "$N3" "$NET3" "$IF3"; add_def_src "$N3" "$GW3" "$IF3" "$IP3"
  add_link_route "$N4" "$NET4" "$IF4"; add_def_src "$N4" "$GW4" "$IF4" "$IP4"

  echo "[*] ip rule (fwmark -> tables)…"
  add_fwmark_rules

  echo "[*] iptables chains…"
  rebuild_mangle_mark
  rebuild_nat_dnat

  echo "[*] done."
  echo "Checks:"
  echo "  iptables -t mangle -vnL $MCHAIN"
  echo "  iptables -t nat -vnL $NCHAIN"
  echo "  ip rule show | egrep 'fwmark 0x1[0-3]'"
}

main "$@"
