#!/usr/bin/env bash
set -euo pipefail

# ====== НАСТРОЙКИ (ИЗМЕНИ ПОД СЕБЯ) ======
IF1="modem1"; GW1="192.168.8.1"
IF2="modem2"; GW2="192.168.14.1"
IF3="modem3"; GW3="192.168.38.1"
IF4="modem4"; GW4="192.168.11.1"

T1=100; T2=101; T3=102; T4=103

M1=0x10; M2=0x11; M3=0x12; M4=0x13

U1=972   # rist1
U2=971   # rist2
U3=970   # rist3
U4=969   # rist4

N1="modem1"
N2="modem2"
N3="modem3"
N4="modem4"
# ==========================================

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (or via sudo)." >&2
    exit 1
  fi
}

ensure_rt_tables_entry() {
  local tid="$1" name="$2"

  # Вариант A: drop-in каталог
  if [ -d /etc/iproute2/rt_tables.d ]; then
    mkdir -p /etc/iproute2/rt_tables.d
    local f=/etc/iproute2/rt_tables.d/99-rist-bonding.conf
    touch "$f"
    # удалим возможные дубликаты и добавим актуальную строку
    sed -i -E "s/^${tid}[[:space:]]\+.*$//" "$f"
    echo "${tid} ${name}" >> "$f"
    return
  fi

  # Вариант B: единый файл
  mkdir -p /etc/iproute2
  local f=/etc/iproute2/rt_tables
  touch "$f"
  grep -qE "^${tid}[[:space:]]+${name}$" "$f" || echo "${tid} ${name}" >> "$f"
}

add_ip_rule_fwmark() {
  local mark="$1" table="$2"
  # ip rule show печатает fwmark в hex (0xNN)
  if ! ip rule show | grep -q "fwmark ${mark} .* lookup ${table}"; then
    ip rule add fwmark "${mark}" table "${table}"
  fi
}

add_route() {
  local table="$1" gw="$2" dev="$3"
  ip route replace default via "$gw" dev "$dev" table "$table" || true
}

iptables_add_unique() {
  local rule="$1"
  if ! iptables -t mangle -C OUTPUT $rule 2>/dev/null; then
    iptables -t mangle -A OUTPUT $rule
  fi
}

write_sysctl() {
  cat >/etc/sysctl.d/99-rist-bonding.conf <<EOF
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.${IF1}.rp_filter = 0
net.ipv4.conf.${IF2}.rp_filter = 0
net.ipv4.conf.${IF3}.rp_filter = 0
net.ipv4.conf.${IF4}.rp_filter = 0
EOF
  sysctl --system >/dev/null
}

main() {
  require_root

  echo "[*] Регистрация таблиц маршрутизации…"
  ensure_rt_tables_entry "$T1" "$N1"
  ensure_rt_tables_entry "$T2" "$N2"
  ensure_rt_tables_entry "$T3" "$N3"
  ensure_rt_tables_entry "$T4" "$N4"

  echo "[*] Sysctl (rp_filter=0)…"
  write_sysctl

  echo "[*] Policy routing правила…"
  add_ip_rule_fwmark "$M1" "$T1"
  add_ip_rule_fwmark "$M2" "$T2"
  add_ip_rule_fwmark "$M3" "$T3"
  add_ip_rule_fwmark "$M4" "$T4"

  echo "[*] Маршруты по таблицам…"
  add_route "$T1" "$GW1" "$IF1"
  add_route "$T2" "$GW2" "$IF2"
  add_route "$T3" "$GW3" "$IF3"
  add_route "$T4" "$GW4" "$IF4"

  echo "[*] iptables маркировка по UID…"
  iptables_add_unique "-m owner --uid-owner $U1 -j MARK --set-mark $M1"
  iptables_add_unique "-m owner --uid-owner $U2 -j MARK --set-mark $M2"
  iptables_add_unique "-m owner --uid-owner $U3 -j MARK --set-mark $M3"
  iptables_add_unique "-m owner --uid-owner $U4 -j MARK --set-mark $M4"

  echo "[*] Готово."
}

main "$@"
