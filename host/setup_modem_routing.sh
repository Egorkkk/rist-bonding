#!/bin/bash

# Modem configuration
IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.199";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

# VRF names (match routing tables 101..104)
VRF1="vrf1"
VRF2="vrf2"
VRF3="vrf3"
VRF4="vrf4"

# Virtual IPs for load balancing
VIP1="192.168.100.1"
VIP2="192.168.100.2"
VIP3="192.168.100.3"
VIP4="192.168.100.4"

# Routing tables
TABLE1=101
TABLE2=102
TABLE3=103
TABLE4=104

# Target server configuration
SERVER_IP="${SERVER_IP:-83.222.26.3}"
SERVER_PORT="${SERVER_PORT:-8000}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_status()  { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_root() {
  if [[ $EUID -ne 0 ]]; then
    print_error "This script must be run as root"; exit 1
  fi
}

check_interface() {
  local interface=$1
  if ! ip link show "$interface" &>/dev/null; then
    print_warning "Interface $interface does not exist"
    return 1
  fi
  return 0
}

clean_config() {
  print_status "Cleaning existing configuration..."

  # NAT
  print_status "Cleaning iptables NAT rules..."
  iptables -t nat -F OUTPUT 2>/dev/null || true
  iptables -t nat -F PREROUTING 2>/dev/null || true
  iptables -t nat -F POSTROUTING 2>/dev/null || true

  # VIPs
  print_status "Removing virtual IP addresses..."
  ip addr del $VIP1/32 dev lo 2>/dev/null || true
  ip addr del $VIP2/32 dev lo 2>/dev/null || true
  ip addr del $VIP3/32 dev lo 2>/dev/null || true
  ip addr del $VIP4/32 dev lo 2>/dev/null || true

  # RPDB rules
  print_status "Cleaning routing rules..."
  ip rule del from $VIP1 table $TABLE1 2>/dev/null || true
  ip rule del from $VIP2 table $TABLE2 2>/dev/null || true
  ip rule del from $VIP3 table $TABLE3 2>/dev/null || true
  ip rule del from $VIP4 table $TABLE4 2>/dev/null || true

  # Tables
  print_status "Cleaning routing tables..."
  ip route flush table $TABLE1 2>/dev/null || true
  ip route flush table $TABLE2 2>/dev/null || true
  ip route flush table $TABLE3 2>/dev/null || true
  ip route flush table $TABLE4 2>/dev/null || true

  # Detach interfaces from VRF (if any) and remove VRFs
  print_status "Detaching interfaces from VRFs (if present)..."
  ip link set $IF1 nomaster 2>/dev/null || true
  ip link set $IF2 nomaster 2>/dev/null || true
  ip link set $IF3 nomaster 2>/dev/null || true
  ip link set $IF4 nomaster 2>/dev/null || true

  print_status "Deleting VRF devices (if present)..."
  ip link del $VRF1 2>/dev/null || true
  ip link del $VRF2 2>/dev/null || true
  ip link del $VRF3 2>/dev/null || true
  ip link del $VRF4 2>/dev/null || true

  print_success "Configuration cleaned"
}

setup_virtual_ips() {
  print_status "Setting up virtual IP addresses..."

  ip addr add $VIP1/32 dev lo || { print_error "Failed to add $VIP1"; exit 1; }
  ip addr add $VIP2/32 dev lo || { print_error "Failed to add $VIP2"; exit 1; }
  ip addr add $VIP3/32 dev lo || { print_error "Failed to add $VIP3"; exit 1; }
  ip addr add $VIP4/32 dev lo || { print_error "Failed to add $VIP4"; exit 1; }

  print_success "Virtual IPs configured"
}

setup_routing() {
  print_status "Setting up routing rules, VRFs and tables..."

  # Interfaces presence
  local interfaces=($IF1 $IF2 $IF3 $IF4)
  for interface in "${interfaces[@]}"; do
    if ! check_interface "$interface"; then
      print_error "Interface $interface not found. Please check modem configuration."
      exit 1
    fi
  done

  # Create VRFs (id = table)
  ip link show $VRF1 &>/dev/null || ip link add $VRF1 type vrf table $TABLE1
  ip link show $VRF2 &>/dev/null || ip link add $VRF2 type vrf table $TABLE2
  ip link show $VRF3 &>/dev/null || ip link add $VRF3 type vrf table $TABLE3
  ip link show $VRF4 &>/dev/null || ip link add $VRF4 type vrf table $TABLE4

  ip link set $VRF1 up
  ip link set $VRF2 up
  ip link set $VRF3 up
  ip link set $VRF4 up

  # Enslave modem interfaces to VRFs
  ip link set $IF1 master $VRF1
  ip link set $IF2 master $VRF2
  ip link set $IF3 master $VRF3
  ip link set $IF4 master $VRF4

  # RPDB rules (VIP → table)
  ip rule add from $VIP1 table $TABLE1 || { print_error "Failed to add rule for $VIP1"; exit 1; }
  ip rule add from $VIP2 table $TABLE2 || { print_error "Failed to add rule for $VIP2"; exit 1; }
  ip rule add from $VIP3 table $TABLE3 || { print_error "Failed to add rule for $VIP3"; exit 1; }
  ip rule add from $VIP4 table $TABLE4 || { print_error "Failed to add rule for $VIP4"; exit 1; }

  # Connected routes into each table (so that default via GW is valid)
  SRC1=$(ip -4 -o addr show dev $IF1 | awk '/inet /{print $4}' | cut -d/ -f1)
  SRC2=$(ip -4 -o addr show dev $IF2 | awk '/inet /{print $4}' | cut -d/ -f1)
  SRC3=$(ip -4 -o addr show dev $IF3 | awk '/inet /{print $4}' | cut -d/ -f1)
  SRC4=$(ip -4 -o addr show dev $IF4 | awk '/inet /{print $4}' | cut -d/ -f1)

  ip route replace table $TABLE1 $NET1 dev $IF1 proto kernel scope link src $SRC1 2>/dev/null || true
  ip route replace table $TABLE2 $NET2 dev $IF2 proto kernel scope link src $SRC2 2>/dev/null || true
  ip route replace table $TABLE3 $NET3 dev $IF3 proto kernel scope link src $SRC3 2>/dev/null || true
  ip route replace table $TABLE4 $NET4 dev $IF4 proto kernel scope link src $SRC4 2>/dev/null || true

  # Default routes
  ip route replace table $TABLE1 default via $GW1 dev $IF1 || { print_error "Failed to add default route for table $TABLE1"; exit 1; }
  ip route replace table $TABLE2 default via $GW2 dev $IF2 || { print_error "Failed to add default route for table $TABLE2"; exit 1; }
  ip route replace table $TABLE3 default via $GW3 dev $IF3 || { print_error "Failed to add default route for table $TABLE3"; exit 1; }
  ip route replace table $TABLE4 default via $GW4 dev $IF4 || { print_error "Failed to add default route for table $TABLE4"; exit 1; }

  print_success "Routing + VRF configured"
}

setup_iptables() {
  print_status "Setting up iptables NAT rules..."

  # DNAT (VIP → SERVER:PORT)
  iptables -t nat -A OUTPUT -s $VIP1 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP1"; exit 1; }
  iptables -t nat -A OUTPUT -s $VIP2 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP2"; exit 1; }
  iptables -t nat -A OUTPUT -s $VIP3 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP3"; exit 1; }
  iptables -t nat -A OUTPUT -s $VIP4 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP4"; exit 1; }

  # SNAT per modem (исходник = VIPX → IPX на нужном интерфейсе)
  iptables -t nat -A POSTROUTING -s $VIP1 -o $IF1 -j SNAT --to-source $IP1 || { print_error "Failed to add SNAT rule for $VIP1"; exit 1; }
  iptables -t nat -A POSTROUTING -s $VIP2 -o $IF2 -j SNAT --to-source $IP2 || { print_error "Failed to add SNAT rule for $VIP2"; exit 1; }
  iptables -t nat -A POSTROUTING -s $VIP3 -o $IF3 -j SNAT --to-source $IP3 || { print_error "Failed to add SNAT rule for $VIP3"; exit 1; }
  iptables -t nat -A POSTROUTING -s $VIP4 -o $IF4 -j SNAT --to-source $IP4 || { print_error "Failed to add SNAT rule for $VIP4"; exit 1; }

  # Reverse DNAT for replies (optional, оставляем как было)
  iptables -t nat -A PREROUTING -i $IF1 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP1 || { print_error "Failed to add PREROUTING rule for $IF1"; exit 1; }
  iptables -t nat -A PREROUTING -i $IF2 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP2 || { print_error "Failed to add PREROUTING rule for $IF2"; exit 1; }
  iptables -t nat -A PREROUTING -i $IF3 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP3 || { print_error "Failed to add PREROUTING rule for $IF3"; exit 1; }
  iptables -t nat -A PREROUTING -i $IF4 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP4 || { print_error "Failed to add PREROUTING rule for $IF4"; exit 1; }

  print_success "iptables rules configured"
}

show_config() {
  print_status "Current configuration:"
  echo
  echo "Virtual IPs:"
  echo "  $VIP1 -> $IF1 ($IP1) [$VRF1 / table $TABLE1]"
  echo "  $VIP2 -> $IF2 ($IP2) [$VRF2 / table $TABLE2]"
  echo "  $VIP3 -> $IF3 ($IP3) [$VRF3 / table $TABLE3]"
  echo "  $VIP4 -> $IF4 ($IP4) [$VRF4 / table $TABLE4]"
  echo
  echo "Send streams to VIPs:"
  echo "  $VIP1:$SERVER_PORT"
  echo "  $VIP2:$SERVER_PORT"
  echo "  $VIP3:$SERVER_PORT"
  echo "  $VIP4:$SERVER_PORT"
  echo
  echo "Target server: $SERVER_IP:$SERVER_PORT"
}

verify_config() {
  print_status "Verifying configuration..."

  local errors=0

  for vip in $VIP1 $VIP2 $VIP3 $VIP4; do
    if ! ip addr show lo | grep -q "$vip"; then
      print_error "Virtual IP $vip not found"; ((errors++))
    fi
  done

  for vip in $VIP1 $VIP2 $VIP3 $VIP4; do
    if ! ip rule show | grep -q "from $vip"; then
      print_error "Routing rule for $vip not found"; ((errors++))
    fi
  done

  if [[ $errors -eq 0 ]]; then
    print_success "Configuration verified successfully"; return 0
  else
    print_error "Configuration verification failed with $errors errors"; return 1
  fi
}

save_config() {
  local config_file="/etc/modem-routing.conf"
  print_status "Saving configuration to $config_file..."

  cat > "$config_file" << EOF
# Modem routing configuration
# Generated on $(date)

IF1="$IF1"; GW1="$GW1"; IP1="$IP1"; NET1="$NET1"
IF2="$IF2"; GW2="$GW2"; IP2="$IP2"; NET2="$NET2"
IF3="$IF3"; GW3="$GW3"; IP3="$IP3"; NET3="$NET3"
IF4="$IF4"; GW4="$GW4"; IP4="$IP4"; NET4="$NET4"

VRF1="$VRF1"; VRF2="$VRF2"; VRF3="$VRF3"; VRF4="$VRF4"

VIP1="$VIP1"
VIP2="$VIP2"
VIP3="$VIP3"
VIP4="$VIP4"

SERVER_IP="$SERVER_IP"
SERVER_PORT="$SERVER_PORT"

TABLE1=$TABLE1
TABLE2=$TABLE2
TABLE3=$TABLE3
TABLE4=$TABLE4
EOF

  print_success "Configuration saved to $config_file"
}

main() {
  print_status "Starting modem routing setup..."

  if [[ "$SERVER_IP" == "83.222.26.3" ]]; then
    print_warning "SERVER_IP is set to default value."
    read -p "Enter server IP address: " SERVER_IP
    export SERVER_IP
  fi

  if [[ "$SERVER_PORT" == "8000" ]]; then
    print_warning "SERVER_PORT is set to default value."
    read -p "Enter server port (default: 8000): " input_port
    if [[ -n "$input_port" ]]; then
      SERVER_PORT="$input_port"; export SERVER_PORT
    fi
  fi

  check_root
  clean_config
  setup_virtual_ips
  setup_routing
  setup_iptables

  if verify_config; then
    save_config
    show_config
    print_success "Modem routing setup completed successfully!"
  else
    print_error "Setup completed with errors. Please check the configuration."
    exit 1
  fi
}

show_help() {
  echo "Usage: $0 [OPTIONS]"
  echo
  echo "Options:"
  echo "  -h, --help      Show this help message"
  echo "  -c, --clean     Clean configuration only (no setup)"
  echo "  -v, --verify    Verify existing configuration"
  echo "  -s, --show      Show current configuration"
  echo
  echo "Environment variables:"
  echo "  SERVER_IP       Target server IP address"
  echo "  SERVER_PORT     Target server port (default: 8000)"
  echo
  echo "Example:"
  echo "  SERVER_IP=10.0.0.100 SERVER_PORT=8080 $0"
}

case "${1:-}" in
  -h|--help)  show_help; exit 0 ;;
  -c|--clean) check_root; clean_config; print_success "Configuration cleaned"; exit 0 ;;
  -v|--verify) verify_config; exit $? ;;
  -s|--show)  show_config; exit 0 ;;
  "")         main ;;
  *)          print_error "Unknown option: $1"; show_help; exit 1 ;;
esac
