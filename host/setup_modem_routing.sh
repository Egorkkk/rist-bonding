#!/bin/bash

# Modem configuration
IF1="modem1"; GW1="192.168.8.1";   IP1="192.168.8.198";  NET1="192.168.8.0/24"
IF2="modem2"; GW2="192.168.14.1";  IP2="192.168.14.100"; NET2="192.168.14.0/24"
IF3="modem3"; GW3="192.168.38.1";  IP3="192.168.38.100"; NET3="192.168.38.0/24"
IF4="modem4"; GW4="192.168.11.1";  IP4="192.168.11.100"; NET4="192.168.11.0/24"

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

# Target server configuration (adjust as needed)
SERVER_IP="${SERVER_IP:-1.2.3.4}"  # Set this to your target server IP
SERVER_PORT="${SERVER_PORT:-5000}"  # Set this to your target server port

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "This script must be run as root"
        exit 1
    fi
}

# Function to check if interface exists
check_interface() {
    local interface=$1
    if ! ip link show "$interface" &> /dev/null; then
        print_warning "Interface $interface does not exist"
        return 1
    fi
    return 0
}

# Function to clean existing configuration
clean_config() {
    print_status "Cleaning existing configuration..."
    
    # Clean iptables NAT rules
    print_status "Cleaning iptables NAT rules..."
    iptables -t nat -F OUTPUT 2>/dev/null || true
    iptables -t nat -F PREROUTING 2>/dev/null || true
    iptables -t nat -F POSTROUTING 2>/dev/null || true
    
    # Remove virtual IPs
    print_status "Removing virtual IP addresses..."
    ip addr del $VIP1/32 dev lo 2>/dev/null || true
    ip addr del $VIP2/32 dev lo 2>/dev/null || true
    ip addr del $VIP3/32 dev lo 2>/dev/null || true
    ip addr del $VIP4/32 dev lo 2>/dev/null || true
    
    # Clean routing rules
    print_status "Cleaning routing rules..."
    ip rule del from $VIP1 table $TABLE1 2>/dev/null || true
    ip rule del from $VIP2 table $TABLE2 2>/dev/null || true
    ip rule del from $VIP3 table $TABLE3 2>/dev/null || true
    ip rule del from $VIP4 table $TABLE4 2>/dev/null || true
    
    # Clean routing tables
    print_status "Cleaning routing tables..."
    ip route flush table $TABLE1 2>/dev/null || true
    ip route flush table $TABLE2 2>/dev/null || true
    ip route flush table $TABLE3 2>/dev/null || true
    ip route flush table $TABLE4 2>/dev/null || true
    
    print_success "Configuration cleaned"
}

# Function to setup virtual IPs
setup_virtual_ips() {
    print_status "Setting up virtual IP addresses..."
    
    ip addr add $VIP1/32 dev lo || { print_error "Failed to add $VIP1"; exit 1; }
    ip addr add $VIP2/32 dev lo || { print_error "Failed to add $VIP2"; exit 1; }
    ip addr add $VIP3/32 dev lo || { print_error "Failed to add $VIP3"; exit 1; }
    ip addr add $VIP4/32 dev lo || { print_error "Failed to add $VIP4"; exit 1; }
    
    print_success "Virtual IPs configured"
}

# Function to setup routing rules and tables
setup_routing() {
    print_status "Setting up routing rules and tables..."
    
    # Check interfaces
    local interfaces=($IF1 $IF2 $IF3 $IF4)
    for interface in "${interfaces[@]}"; do
        if ! check_interface "$interface"; then
            print_error "Interface $interface not found. Please check modem configuration."
            exit 1
        fi
    done
    
    # Add routing rules
    ip rule add from $VIP1 table $TABLE1 || { print_error "Failed to add rule for $VIP1"; exit 1; }
    ip rule add from $VIP2 table $TABLE2 || { print_error "Failed to add rule for $VIP2"; exit 1; }
    ip rule add from $VIP3 table $TABLE3 || { print_error "Failed to add rule for $VIP3"; exit 1; }
    ip rule add from $VIP4 table $TABLE4 || { print_error "Failed to add rule for $VIP4"; exit 1; }
    
    # Add routes to routing tables
    # Local network routes
    ip route add $NET1 dev $IF1 table $TABLE1 || print_warning "Failed to add local route for $NET1"
    ip route add $NET2 dev $IF2 table $TABLE2 || print_warning "Failed to add local route for $NET2"
    ip route add $NET3 dev $IF3 table $TABLE3 || print_warning "Failed to add local route for $NET3"
    ip route add $NET4 dev $IF4 table $TABLE4 || print_warning "Failed to add local route for $NET4"
    
    # Default routes
    ip route add default via $GW1 dev $IF1 table $TABLE1 || { print_error "Failed to add default route for table $TABLE1"; exit 1; }
    ip route add default via $GW2 dev $IF2 table $TABLE2 || { print_error "Failed to add default route for table $TABLE2"; exit 1; }
    ip route add default via $GW3 dev $IF3 table $TABLE3 || { print_error "Failed to add default route for table $TABLE3"; exit 1; }
    ip route add default via $GW4 dev $IF4 table $TABLE4 || { print_error "Failed to add default route for table $TABLE4"; exit 1; }
    
    print_success "Routing configured"
}

# Function to setup iptables NAT rules
setup_iptables() {
    print_status "Setting up iptables NAT rules..."
    
    # DNAT rules for outgoing traffic (ristsender -> server)
    iptables -t nat -A OUTPUT -s $VIP1 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP1"; exit 1; }
    iptables -t nat -A OUTPUT -s $VIP2 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP2"; exit 1; }
    iptables -t nat -A OUTPUT -s $VIP3 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP3"; exit 1; }
    iptables -t nat -A OUTPUT -s $VIP4 -p udp --dport $SERVER_PORT -j DNAT --to-destination $SERVER_IP:$SERVER_PORT || { print_error "Failed to add DNAT rule for $VIP4"; exit 1; }
    
    # SNAT rules for outgoing traffic (change source IP to modem IP)
    iptables -t nat -A POSTROUTING -s $VIP1 -o $IF1 -j SNAT --to-source $IP1 || { print_error "Failed to add SNAT rule for $VIP1"; exit 1; }
    iptables -t nat -A POSTROUTING -s $VIP2 -o $IF2 -j SNAT --to-source $IP2 || { print_error "Failed to add SNAT rule for $VIP2"; exit 1; }
    iptables -t nat -A POSTROUTING -s $VIP3 -o $IF3 -j SNAT --to-source $IP3 || { print_error "Failed to add SNAT rule for $VIP3"; exit 1; }
    iptables -t nat -A POSTROUTING -s $VIP4 -o $IF4 -j SNAT --to-source $IP4 || { print_error "Failed to add SNAT rule for $VIP4"; exit 1; }
    
    # PREROUTING rules for incoming traffic (server -> ristsender)
    iptables -t nat -A PREROUTING -i $IF1 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP1 || { print_error "Failed to add PREROUTING rule for $IF1"; exit 1; }
    iptables -t nat -A PREROUTING -i $IF2 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP2 || { print_error "Failed to add PREROUTING rule for $IF2"; exit 1; }
    iptables -t nat -A PREROUTING -i $IF3 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP3 || { print_error "Failed to add PREROUTING rule for $IF3"; exit 1; }
    iptables -t nat -A PREROUTING -i $IF4 -p udp -s $SERVER_IP --sport $SERVER_PORT -j DNAT --to-destination $VIP4 || { print_error "Failed to add PREROUTING rule for $IF4"; exit 1; }
    
    print_success "iptables rules configured"
}

# Function to display configuration
show_config() {
    print_status "Current configuration:"
    echo
    echo "Virtual IPs:"
    echo "  $VIP1 -> $IF1 ($IP1)"
    echo "  $VIP2 -> $IF2 ($IP2)"
    echo "  $VIP3 -> $IF3 ($IP3)"
    echo "  $VIP4 -> $IF4 ($IP4)"
    echo
    echo "Configure ristsender to send streams to:"
    echo "  Stream 1: $VIP1:$SERVER_PORT"
    echo "  Stream 2: $VIP2:$SERVER_PORT"
    echo "  Stream 3: $VIP3:$SERVER_PORT"
    echo "  Stream 4: $VIP4:$SERVER_PORT"
    echo
    echo "Target server: $SERVER_IP:$SERVER_PORT"
}

# Function to verify configuration
verify_config() {
    print_status "Verifying configuration..."
    
    local errors=0
    
    # Check virtual IPs
    for vip in $VIP1 $VIP2 $VIP3 $VIP4; do
        if ! ip addr show lo | grep -q "$vip"; then
            print_error "Virtual IP $vip not found"
            ((errors++))
        fi
    done
    
    # Check routing rules
    for vip in $VIP1 $VIP2 $VIP3 $VIP4; do
        if ! ip rule show | grep -q "from $vip"; then
            print_error "Routing rule for $vip not found"
            ((errors++))
        fi
    done
    
    if [[ $errors -eq 0 ]]; then
        print_success "Configuration verified successfully"
        return 0
    else
        print_error "Configuration verification failed with $errors errors"
        return 1
    fi
}

# Function to save configuration to file
save_config() {
    local config_file="/etc/modem-routing.conf"
    print_status "Saving configuration to $config_file..."
    
    cat > "$config_file" << EOF
# Modem routing configuration
# Generated on $(date)

# Modem interfaces and IPs
IF1="$IF1"; GW1="$GW1"; IP1="$IP1"; NET1="$NET1"
IF2="$IF2"; GW2="$GW2"; IP2="$IP2"; NET2="$NET2"
IF3="$IF3"; GW3="$GW3"; IP3="$IP3"; NET3="$NET3"
IF4="$IF4"; GW4="$GW4"; IP4="$IP4"; NET4="$NET4"

# Virtual IPs
VIP1="$VIP1"
VIP2="$VIP2"
VIP3="$VIP3"
VIP4="$VIP4"

# Target server
SERVER_IP="$SERVER_IP"
SERVER_PORT="$SERVER_PORT"

# Routing tables
TABLE1=$TABLE1
TABLE2=$TABLE2
TABLE3=$TABLE3
TABLE4=$TABLE4
EOF
    
    print_success "Configuration saved to $config_file"
}

# Main function
main() {
    print_status "Starting modem routing setup..."
    
    # Check if SERVER_IP is set
    if [[ "$SERVER_IP" == "83.222.26.3" ]]; then
        print_warning "SERVER_IP is set to default value. Please set SERVER_IP environment variable."
        read -p "Enter server IP address: " SERVER_IP
        export SERVER_IP
    fi
    
    if [[ "$SERVER_PORT" == "8000" ]]; then
        print_warning "SERVER_PORT is set to default value."
        read -p "Enter server port (default: 8000): " input_port
        if [[ -n "$input_port" ]]; then
            SERVER_PORT="$input_port"
            export SERVER_PORT
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

# Help function
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
    echo "  SERVER_PORT     Target server port (default: 5000)"
    echo
    echo "Example:"
    echo "  SERVER_IP=10.0.0.100 SERVER_PORT=8080 $0"
}

# Parse command line arguments
case "${1:-}" in
    -h|--help)
        show_help
        exit 0
        ;;
    -c|--clean)
        check_root
        clean_config
        print_success "Configuration cleaned"
        exit 0
        ;;
    -v|--verify)
        verify_config
        exit $?
        ;;
    -s|--show)
        show_config
        exit 0
        ;;
    "")
        main
        ;;
    *)
        print_error "Unknown option: $1"
        show_help
        exit 1
        ;;
esac