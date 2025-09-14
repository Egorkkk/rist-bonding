#!/usr/bin/env bash
# NM dispatcher: $1 = interface, $2 = action
# Срабатывает, когда поднимается любой modem* и есть IPv4 — запускаем политику.

IFACE="$1"
ACTION="$2"

# Логгировать в journal для отладки
log(){ logger -t rist-policy-dispatcher -- "$@"; }

# Интересуют только наши модемы
case "$IFACE" in
  modem1|modem2|modem3|modem4) ;;
  *) exit 0 ;;
esac

# Нас интересуют моменты, когда адреса/линк обновились
case "$ACTION" in
  up|dhcp4-change|connectivity-change|carrier|vpn-up)
    :
    ;;
  *)
    exit 0
    ;;
esac

# Ждём максимум 10 секунд появления IPv4 на интерфейсе
for _ in {1..10}; do
  if ip -4 addr show dev "$IFACE" | grep -q "inet "; then
    break
  fi
  sleep 1
done

# Если IP так и не появился — выходим тихо
ip -4 addr show dev "$IFACE" | grep -q "inet " || exit 0

# Чтобы не запускать несколько копий одновременно — берём lock
exec 9>/run/rist-policy.lock
if ! flock -n 9; then
  log "lock busy; another policy run in progress"
  exit 0
fi

log "Trigger policy due to $IFACE $ACTION"
# Запускаем политику
/usr/local/bin/rist_policy.sh && log "policy applied" || log "policy FAILED ($?)"
EOF

sudo chmod +x /etc/NetworkManager/dispatcher.d/90-rist-policy.sh
