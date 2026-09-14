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

# --- Runtime Callback Whitelist (ActionsCat integration) ---
if [ -n "$RUNTIME_CALLBACK_URL" ]; then
    echo "   -> Configuring firewall whitelist for RUNTIME_CALLBACK_URL: $RUNTIME_CALLBACK_URL"
    cb_proto_removed="${RUNTIME_CALLBACK_URL#*://}"
    cb_host_port="${cb_proto_removed%%/*}"
    case "$cb_host_port" in
        *:*)
            cb_host="${cb_host_port%:*}"
            cb_port="${cb_host_port##*:}"
            ;;
        *)
            cb_host="$cb_host_port"
            case "$RUNTIME_CALLBACK_URL" in
                https://*) cb_port="443" ;;
                http://*) cb_port="80" ;;
                *) cb_port="" ;;
            esac
            ;;
    esac
    if [ -n "$cb_port" ]; then
        iptables -A OUTPUT -p tcp -d "$cb_host" --dport "$cb_port" -j ACCEPT 2>/dev/null || true
    else
        iptables -A OUTPUT -d "$cb_host" -j ACCEPT 2>/dev/null || true
    fi
fi

# --- Allowed Hosts Whitelist ---
if [ -n "$ALLOWED_HOSTS" ]; then
    echo "   -> Configuring firewall whitelist for ALLOWED_HOSTS: $ALLOWED_HOSTS"
    # Allow Docker DNS (127.0.0.11) and specifically configured nameservers only (prevent ANY:53 exfiltration bypass)
    iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT 2>/dev/null || true
    iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT 2>/dev/null || true
    if [ -f /etc/resolv.conf ]; then
        grep '^nameserver' /etc/resolv.conf | awk '{print $2}' | while read -r ns; do
            if [ -n "$ns" ]; then
                iptables -A OUTPUT -d "$ns" -p udp --dport 53 -j ACCEPT 2>/dev/null || true
                iptables -A OUTPUT -d "$ns" -p tcp --dport 53 -j ACCEPT 2>/dev/null || true
            fi
        done
    fi

    OLD_IFS="$IFS"
    IFS=","
    for item in $ALLOWED_HOSTS; do
        item_trimmed=$(echo "$item" | tr -d ' ')
        if [ -n "$item_trimmed" ]; then
            case "$item_trimmed" in
                *:*)
                    h="${item_trimmed%:*}"
                    p="${item_trimmed##*:}"
                    iptables -A OUTPUT -p tcp -d "$h" --dport "$p" -j ACCEPT 2>/dev/null || true
                    ;;
                *)
                    iptables -A OUTPUT -d "$item_trimmed" -j ACCEPT 2>/dev/null || true
                    ;;
            esac
        fi
    done
    IFS="$OLD_IFS"
fi

# Determine effective network mode
EFFECTIVE_NET_MODE="${NETWORK_MODE:-}"
if [ -z "$EFFECTIVE_NET_MODE" ]; then
    if [ "$INTERNET_ACCESS" = "true" ]; then
        EFFECTIVE_NET_MODE="public"
    else
        EFFECTIVE_NET_MODE="isolated"
    fi
elif [ "$EFFECTIVE_NET_MODE" = "none" ]; then
    EFFECTIVE_NET_MODE="isolated"
fi

if [ "$EFFECTIVE_NET_MODE" = "public" ]; then
    echo "   -> Configuring OUTPUT rules for PUBLIC mode..."
    iptables -P OUTPUT ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # 允许 Docker DNS (127.0.0.11)
    iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT

    # 禁止所有私有 IP 范围 (已在上方 ACCEPT 的回调或允许主机例外优先匹配)
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

    echo "   -> Public internet access ENABLED (private IPs blocked)."
else
    echo "   -> Configuring OUTPUT rules for $EFFECTIVE_NET_MODE mode..."
    iptables -P OUTPUT DROP
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    echo "   -> Mode $EFFECTIVE_NET_MODE enforced (whitelisted destinations permitted)."
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
