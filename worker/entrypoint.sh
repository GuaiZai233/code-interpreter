#!/bin/sh
# worker/entrypoint.sh

# Exit immediately if a command exits with a non-zero status.
set -e

echo "🔒 Worker Entrypoint: Running as ROOT to configure system..."

# --- Step 1: Configure the IPTables Firewall Jail ---
echo "   -> Configuring firewall..."
if [ -z "$GATEWAY_INTERNAL_IP" ]; then
    echo "FATAL: GATEWAY_INTERNAL_IP environment variable is not set."
    exit 1
fi

# Get internet access configuration
INTERNET_ACCESS="${WORKER_INTERNET_ACCESS:-false}"
echo "   -> Worker Internet Access: $INTERNET_ACCESS"

# ============ INPUT 规则（入站流量）============
iptables -P INPUT DROP
# 核心安全边界：严格禁止非 Gateway 流量访问 Worker 控制端口 8000
# 彻底阻断容器内部回环 (lo) 或自身 IP 访问控制端口，防止沙箱执行的命令直接调用 /shell/exec 等端点
iptables -A INPUT -p tcp --dport 8000 ! -s "$GATEWAY_INTERNAL_IP" -j DROP
iptables -A INPUT -i lo -p tcp --dport 8000 -j DROP

iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 8000 -s "$GATEWAY_INTERNAL_IP" -j ACCEPT

# ============ OUTPUT 规则（出站流量）============
# 核心安全边界：禁止容器内发往端口 8000 的所有出站连接（Worker 为服务端，不需要主动连接 8000）
iptables -A OUTPUT -p tcp --dport 8000 -j DROP

if [ "$INTERNET_ACCESS" = "true" ]; then
    echo "   -> Configuring OUTPUT rules for INTERNET-ENABLED mode..."
    iptables -P OUTPUT ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # 允许 Docker DNS (127.0.0.11)
    iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT

    # 禁止所有私有 IP 范围
    iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
    iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
    iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
    iptables -A OUTPUT ! -o lo -d 127.0.0.0/8 -j DROP
    iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
    iptables -A OUTPUT -d 100.64.0.0/10 -j DROP
    iptables -A OUTPUT -d 0.0.0.0/8 -j DROP
    iptables -A OUTPUT -d 224.0.0.0/4 -j DROP
    iptables -A OUTPUT -d 240.0.0.0/4 -j DROP
    iptables -A OUTPUT -d 255.255.255.255 -j DROP
    # 云元数据服务 IP (AWS/GCP/Azure 等)
    iptables -A OUTPUT -d 169.254.169.254 -j DROP

    echo "   -> Internet access ENABLED (private IPs blocked)."
else
    echo "   -> Configuring OUTPUT rules for ISOLATED mode..."
    iptables -P OUTPUT DROP
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    echo "   -> Internet access DISABLED (fully isolated)."
fi

echo "   -> Firewall configured. Gateway IP: $GATEWAY_INTERNAL_IP"

# --- Step 2: Mount the Virtual Disk ---
echo "   -> Mounting virtual disk device..."
# The /sandbox directory should already exist as the user's home directory.
# Mount the virtual block device (passed in by the Gateway) to /sandbox.
mount /dev/vdisk /sandbox
echo "   -> Virtual disk mounted to /sandbox."

# --- Step 3: Set Final Permissions ---
# After mounting, change the ownership of the new filesystem's root.
chown -R sandbox:sandbox /sandbox
echo "   -> Changed ownership of /sandbox to 'sandbox' user."

# --- Step 4: Drop Privileges and Start Services ---
echo "🚀 Dropping privileges and starting Supervisor..."
# Use exec to replace this script's process with the supervisord process.
# Tini will act as the init system.
# Supervisord will now start fastapi as the 'sandbox' user.
exec /usr/bin/tini -- /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
