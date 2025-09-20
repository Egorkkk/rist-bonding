#!/usr/bin/env bash
set -euo pipefail
MODEMS=(
  "modem1 192.168.8.199 192.168.8.1"
  "modem2 192.168.14.100 192.168.14.1"
  "modem3 192.168.38.100 192.168.38.1"
  "modem4 192.168.11.100 192.168.11.1"
)
DST="1.1.1.1"   # можно список и рандомайзить: 1.1.1.1 / 8.8.8.8

TMP="/run/rist-modems.tmp.json"
OUT="/run/rist-modems.json"
mkdir -p /run
echo "[" > "$TMP"
first=1
for entry in "${MODEMS[@]}"; do
  read -r IFACE IP GW <<<"$entry"

  OPER="down"
  [[ -f /sys/class/net/$IFACE/operstate ]] && OPER=$(cat /sys/class/net/$IFACE/operstate || echo "down")

  HAS_IP="no"
  ip -4 addr show dev "$IFACE" | grep -q "inet " && HAS_IP="yes"

  GW_OK="no"
  if [[ "$HAS_IP" == "yes" ]]; then
    ping -I "$IFACE" -W 1 -c 1 "$GW" >/dev/null 2>&1 && GW_OK="yes"
  fi

  WAN_OK="no"
  if [[ "$HAS_IP" == "yes" ]]; then
    # ВАЖНО: source = IP модема → пойдёт по его таблице (ip rule from ...)
    ping -I "$IP" -W 1 -c 1 "$DST" >/dev/null 2>&1 && WAN_OK="yes"
  fi

  STATUS="down"
  if [[ "$OPER" == "up" && "$HAS_IP" == "yes" && "$GW_OK" == "yes" ]]; then
    STATUS="up_local"
    [[ "$WAN_OK" == "yes" ]] && STATUS="up_internet"
  fi

  [[ $first -eq 0 ]] && echo "," >> "$TMP"
  first=0
  jq -cn --arg iface "$IFACE" --arg ip "$IP" --arg gw "$GW" \
        --arg oper "$OPER" --arg has_ip "$HAS_IP" \
        --arg gw_ok "$GW_OK" --arg wan_ok "$WAN_OK" --arg status "$STATUS" \
        '{iface:$iface, ip:$ip, gw:$gw, oper:$oper, has_ip:$has_ip, gw_ok:$gw_ok, wan_ok:$wan_ok, status:$status}' \
        >> "$TMP"
done
echo "]" >> "$TMP"
mv "$TMP" "$OUT"
