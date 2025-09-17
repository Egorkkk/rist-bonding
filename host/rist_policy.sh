#!/usr/bin/env bash
set -euo pipefail
# set -x

# ================== ПАРАМЕТРЫ ==================
SRV="83.222.26.3"
SRV_PORT="8000"                # <- у тебя теперь 8000

# Виртуальные цели для ristsender (локальные «псевдо-IP» и порты)
V1="10.255.0.1"; P1=9001
V2="10.255.0.2"; P2=9002
V3="10.255.0.3"; P3=9003
V4="10.255.0.4"; P4=9004

# Модемы
IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.198";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

# Таблицы
T1=100; T2=101; T3=102; T4=103
N1="modem1"; N2="modem2"; N3="modem3"; N4="modem4"

# Цепочка DNAT
CHAIN="RIST_DNAT"
# =================================================

require_root() { if [ "$(id -u)" -ne 0 ]; then echo "Run as root (sudo)." >&2; exit 1; fi; }

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

# ---- УТИЛИТЫ IPTABLES ----
# удалить ВСЕ прямые DNAT к 10.255.0.[1-4]:900X в nat/OUTPUT (независимо от --to-destination и порядка опций)
purge_direct_dnat_virtuals_output() {
  # 1) попытка по -S (rulespec) — с очень широким шаблоном
  for pass in 1 2 3 4 5; do
    local removed=0
    while read -r line; do
      # линейка вида: -A OUTPUT ... -d 10.255.0.X ... --dport 900X ... -j DNAT ...
      local m; m=$(grep -E -- "-A OUTPUT .*10\.255\.0\.(1|2|3|4)(/32)? .* --dport (9001|9002|9003|9004) .* -j DNAT" <<<"$line" || true)
      [ -z "$m" ] && continue
      local spec="${line#-A OUTPUT }"
      if iptables -t nat -D OUTPUT $spec 2>/dev/null; then removed=1; fi
    done < <(iptables -t nat -S OUTPUT)
    [ "$removed" = 0 ] && break
  done

  # 2) fallback по -L --line-numbers (если вдруг что-то осталось)
  for pass in 1 2 3 4 5 6 7 8; do
    mapfile -t LNS < <(
      iptables -t nat -L OUTPUT --line-numbers -n \
      | awk '/DNAT/ && /10\.255\.0\.(1|2|3|4)(\/32)?/ && /(dpt:9001|dpt:9002|dpt:9003|dpt:9004)/ {print $1}' \
      | sort -rn
    )
    [ "${#LNS[@]}" -eq 0 ] && break
    for ln in "${LNS[@]}"; do iptables -t nat -D OUTPUT "$ln" || true; done
  done
}

# старые owner/SNAT → снести по номерам строк
purge_owner_snat_to_srv_output() {
  for pass in 1 2 3; do
    mapfile -t LNS < <(
      iptables -t nat -L OUTPUT --line-numbers -n \
      | awk -v srv="$SRV" -v dpt="$SRV_PORT" '/SNAT/ && $0 ~ srv && $0 ~ ("dpt:" dpt) && /owner UID match/ {print $1}' \
      | sort -rn
    )
    [ "${#LNS[@]}" -eq 0 ] && break
    for ln in "${LNS[@]}"; do iptables -t nat -D OUTPUT "$ln" || true; done
  done
}

# создать/починить свою цепочку и единственный jump
ensure_nat_chain() {
  iptables -t nat -N "$CHAIN" 2>/dev/null || true
  if ! iptables -t nat -C OUTPUT -j "$CHAIN" 2>/dev/null; then
    iptables -t nat -I OUTPUT 1 -j "$CHAIN"
  fi
  # удалить возможные дубликаты jump'ов
  while :; do
    mapfile -t JUMPS < <(iptables -t nat -L OUTPUT --line-numbers \
      | awk -v c="$CHAIN" '$0 ~ ("jump " c) {print $1}' | sort -rn)
    [ "${#JUMPS[@]}" -le 1 ] && break
    for ((i=0;i<${#JUMPS[@]}-1;i++)); do iptables -t nat -D OUTPUT "${JUMPS[i]}" || true; done
  done
}

# полностью пересобрать содержимое нашей цепочки
rebuild_rist_dnat_chain() {
  ensure_nat_chain
  iptables -t nat -F "$CHAIN"

  iptables -t nat -A "$CHAIN" -p udp -d "$V1" --dport "$P1" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist v1"
  iptables -t nat -A "$CHAIN" -p udp -d "$V2" --dport "$P2" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist v2"
  iptables -t nat -A "$CHAIN" -p udp -d "$V3" --dport "$P3" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist v3"
  iptables -t nat -A "$CHAIN" -p udp -d "$V4" --dport "$P4" -j DNAT --to-destination "$SRV:$SRV_PORT" -m comment --comment "rist v4"
}

# ---- POLICY ROUTING ----
get_iface_ip() { ip -o -4 addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1; }

add_link_route() {
  local tbl="$1" net="$2" dev="$3" src
  src="$(get_iface_ip "$dev" || true)"
  if [[ -n "${src:-}" ]]; then
    ip route replace "$net" dev "$dev" table "$tbl" proto kernel scope link src "$src"
  else
    ip route replace "$net" dev "$dev" table "$tbl" proto kernel scope link
  fi
}

add_default_route_src() { ip route replace default via "$2" dev "$3" src "$4" table "$1"; }

add_rule_to()   { ip rule del to "$1" table "$2" 2>/dev/null || true; ip rule add to "$1" table "$2" pref "$3"; }
add_rule_from() { ip rule del from "$1" table "$2" 2>/dev/null || true; ip rule add from "$1" table "$2" pref "$3"; }

# ---- MAIN ----
main() {
  require_root

  echo "[*] rt_tables mapping…"; write_rt_tables_file
  echo "[*] Sysctl (rp_filter=2)…"; write_sysctl

  echo "[*] Cleanup: ip rules & route tables…"
  # ip rules — удалим только наши сочетания (и добавим заново ниже)
  for spec in \
    "to $V1 table $N1" "to $V2 table $N2" "to $V3 table $N3" "to $V4 table $N4" \
    "from $IP1 table $N1" "from $IP2 table $N2" "from $IP3 table $N3" "from $IP4 table $N4"
  do ip rule del $spec 2>/dev/null || true; done

  ip route flush table "$N1" || ip route flush table "$T1" || true
  ip route flush table "$N2" || ip route flush table "$T2" || true
  ip route flush table "$N3" || ip route flush table "$T3" || true
  ip route flush table "$N4" || ip route flush table "$T4" || true

  echo "[*] Cleanup: nat/OUTPUT legacy DNAT & owner/SNAT…"
  purge_direct_dnat_virtuals_output
  purge_owner_snat_to_srv_output

  echo "[*] Policy rules (to → from)…"
  add_rule_to   "$V1" "$N1" 1001
  add_rule_to   "$V2" "$N2" 1002
  add_rule_to   "$V3" "$N3" 1003
  add_rule_to   "$V4" "$N4" 1004
  add_rule_from "$IP1" "$N1" 1101
  add_rule_from "$IP2" "$N2" 1102
  add_rule_from "$IP3" "$N3" 1103
  add_rule_from "$IP4" "$N4" 1104

  echo "[*] Routes per table…"
  add_link_route "$N1" "$NET1" "$IF1"
  add_link_route "$N2" "$NET2" "$IF2"
  add_link_route "$N3" "$NET3" "$IF3"
  add_link_route "$N4" "$NET4" "$IF4"
  add_default_route_src "$N1" "$GW1" "$IF1" "$IP1"
  add_default_route_src "$N2" "$GW2" "$IF2" "$IP2"
  add_default_route_src "$N3" "$GW3" "$IF3" "$IP3"
  add_default_route_src "$N4" "$GW4" "$IF4" "$IP4"

  echo "[*] NAT: OUTPUT → $CHAIN (flush & rebuild)…"
  rebuild_rist_dnat_chain

  echo "[*] Готово.\n"
  echo "Проверка:"
  echo "  iptables -t nat -S OUTPUT | egrep 'jump $CHAIN|10\\.255\\.0\\.'"
  echo "  iptables -t nat -S $CHAIN"
  echo "  ip rule show | egrep 'to 10\\.255\\.0\\.|from 192\\.168\\.'"
}
main "$@"
