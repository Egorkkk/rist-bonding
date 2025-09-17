#!/usr/bin/env bash
set -euo pipefail

echo "=== interfaces (brief) ==="
ip -br addr show modem1 modem2 modem3 modem4 || true

echo
echo "=== ip rule ==="
ip rule show

echo
echo "=== rt_tables ==="
(test -d /etc/iproute2/rt_tables.d \
  && cat /etc/iproute2/rt_tables.d/* \
  || cat /etc/iproute2/rt_tables) \
  | sed -n 's/^#.*//p'

echo
echo "=== tables ==="
for T in main local default 100 101 102 103 modem1 modem2 modem3 modem4; do
  echo "-- table $T"
  ip route show table "$T" || true
  echo
done

echo "=== sysctl rp_filter ==="
sysctl net.ipv4.conf.all.rp_filter \
  $(for i in modem1 modem2 modem3 modem4; do echo -n " net.ipv4.conf.$i.rp_filter"; done)

echo
echo "=== mangle/OUTPUT rules ==="
iptables -t mangle -S OUTPUT

echo
echo "=== mangle/OUTPUT counters ==="
iptables -t mangle -vnL OUTPUT --line-numbers

echo
echo "=== UIDs ristsender ==="
getent passwd rist0 rist1 rist2 rist3 rist4 || true
