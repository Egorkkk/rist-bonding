#!/usr/bin/env bash
set -euo pipefail

SRV="83.222.26.3"
SRV_PORT="8000"
VIPS=(10.255.0.1 10.255.0.2 10.255.0.3 10.255.0.4)
USERS=(rist1 rist2 rist3 rist4)
PIDDIR="/run/rist-socat"
LOGDIR="/var/log/rist-socat"
SPORTS=(40001 40002 40003 40004)

ensure() {
  command -v socat >/dev/null || { echo "Install socat (dnf install -y socat)"; exit 1; }
  mkdir -p "$PIDDIR" "$LOGDIR"
  # убедимся, что массивы синхронной длины
  if [ "${#VIPS[@]}" -ne "${#USERS[@]}" ]; then
    echo "VIPS and USERS length mismatch"; exit 1
  fi
  # проверим, что пользователи существуют
  for u in "${USERS[@]}"; do
    id -u "$u" >/dev/null 2>&1 || { echo "User $u not found (run rist_policy.sh first)"; exit 1; }
  done
}

start_one() {
  local idx="${1:-}"
  if [[ -z "$idx" ]]; then echo "start_one: missing index"; return 1; fi
  local u="${USERS[$idx]}"
  local vip="${VIPS[$idx]}"
  local pidf="$PIDDIR/socat_$((idx+1)).pid"
  local logf="$LOGDIR/socat_$((idx+1)).log"

  if [[ -f "$pidf" ]]; then
    local oldpid; oldpid="$(cat "$pidf" 2>/dev/null || true)"
    if [[ -n "${oldpid:-}" ]] && kill -0 "$oldpid" 2>/dev/null; then
      echo "socat[$((idx+1))] already running (pid $oldpid)"
      return 0
    else
      rm -f "$pidf"
    fi
  fi

  echo "Starting socat[$((idx+1))] as $u: ${vip}:${SRV_PORT} -> ${SRV}:${SRV_PORT}"
  # UDP relay: listen on VIP:8000 and forward to SRV:8000; replies are relayed back
  # -u (unidirectional copy) ок для релея; fork — для множества источников; reuseaddr — безопасный реbind
  sudo -u "$u" nohup socat -T 600 \
    "UDP4-RECVFROM:8000,bind=${vip},reuseaddr" \
    "UDP4-SENDTO:${SRV}:8000,sourceport=${SPORTS[$idx]},reuseaddr" \
    >>"$logf" 2>&1 &

  echo $! >"$pidf"
}

stop_one() {
  local idx="${1:-}"
  if [[ -z "$idx" ]]; then echo "stop_one: missing index"; return 1; fi
  local pidf="$PIDDIR/socat_$((idx+1)).pid"
  if [[ -f "$pidf" ]]; then
    local pid; pid="$(cat "$pidf" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping socat[$((idx+1))] pid $pid"
      kill "$pid" || true
    fi
    rm -f "$pidf"
  else
    echo "socat[$((idx+1))] not running"
  fi
}

status() {
  for ((i=0; i<${#VIPS[@]}; i++)); do
    local pidf="$PIDDIR/socat_$((i+1)).pid"
    if [[ -f "$pidf" ]]; then
      local pid; pid="$(cat "$pidf" 2>/dev/null || true)"
      if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "socat[$((i+1))] RUNNING (pid $pid) on ${VIPS[$i]}:${SRV_PORT}"
        continue
      fi
    fi
    echo "socat[$((i+1))] STOPPED"
  done
}

case "${1:-status}" in
  start)
    ensure
    for ((i=0; i<${#VIPS[@]}; i++)); do start_one "$i"; done
    ;;
  stop)
    for ((i=0; i<${#VIPS[@]}; i++)); do stop_one "$i"; done
    ;;
  restart)
    "$0" stop || true
    "$0" start
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"; exit 1
    ;;
esac
