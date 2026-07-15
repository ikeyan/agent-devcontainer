#!/bin/bash
# コンテナの egress を allowlist 方式で制限する。
# anthropics/claude-code の公式 .devcontainer/init-firewall.sh をベースに、
# Socket Firewall (sfw) とこのリポジトリの依存先を追加している。
# 注意: ここを編集したらイメージの rebuild が必要 (COPY されるため)。
# CDN の IP ローテーションで通信が切れたら `sudo /usr/local/bin/init-firewall.sh` を再実行する。
set -euo pipefail
IFS=$'\n\t'

ALLOWED_DOMAINS=(
    # --- Claude Code ---
    "registry.npmjs.org"
    "api.anthropic.com"
    "docs.claude.com"
    "code.claude.com"
    "platform.claude.com"
    "console.anthropic.com"
    "claude.ai"
    "sentry.io"
    "statsig.com"
    # --- https://github.com/ikeyan/agent-files/blob/main/claude_trusted_domains.txt
    # クラウド環境のネットワークアクセスの許可されたドメインリスト
    "jsr.io"
    "api.jsr.io"
    "npm.jsr.io"
    # --- Socket Firewall (sfw) ---
    "api.socket.dev"
    "firewall-api.socket.dev"
    # --- GitHub Releases のアセット配信 (sfw 自動更新 / uv の Python 取得) ---
    "objects.githubusercontent.com"
    "release-assets.githubusercontent.com"
    # --- VS Code ---
    "marketplace.visualstudio.com"
    "vscode.blob.core.windows.net"
    "update.code.visualstudio.com"
    # --- kit 本体 (uv-sync) が要る ---
    "pypi.org"
    "files.pythonhosted.org"
)

# project 層の追加許可ドメイン (project/allow-domains.txt を image へ COPY したもの。編集後 rebuild)。
# fail-closed: 行コメント除去 → 前後空白のみ trim (内部空白は消さない) → 空行はスキップ → 残りは
# ドメイン形を厳格に検証し、外れたら黙って連結せず起動失敗する。旧実装は「空白全除去」で不正行
# (内部空白/制御文字混じり) を無言で連結し、意図しない許可先を作りうる穴だった。
PROJECT_DOMAINS_FILE=/etc/agent-devcontainer/allow-domains.txt
if [ -f "$PROJECT_DOMAINS_FILE" ]; then
    while read -r d; do
        d="${d%%#*}"                       # 行コメント除去
        d="${d#"${d%%[![:space:]]*}"}"     # 先頭空白 trim
        d="${d%"${d##*[![:space:]]}"}"     # 末尾空白 trim
        [ -n "$d" ] || continue            # 空行/コメントのみ行はスキップ
        [[ "$d" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
            || { echo "invalid domain in allow-domains.txt: $d" >&2; exit 1; }
        ALLOWED_DOMAINS+=("$d")
    done < "$PROJECT_DOMAINS_FILE"
fi

# 1. Extract Docker DNS info BEFORE any flushing
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

# Flush existing rules and delete existing ipsets
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# 再実行を冪等にする: flush はチェーンのポリシーをリセットしないため、
# 前回実行の DROP が残っていると以降の GitHub メタ情報取得や DNS 解決が
# すべて失敗し、全遮断の中途半端な状態で詰む (VS Code は postStartCommand を
# 再実行することがある)。一旦 ACCEPT に戻し、構築完了後に DROP へ切り替える
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT

# 2. Selectively restore ONLY internal Docker DNS resolution
if [ -n "$DOCKER_DNS_RULES" ]; then
    echo "Restoring Docker DNS rules..."
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
else
    echo "No Docker DNS rules to restore"
fi

# 公式スクリプトとの差分: 「任意ホストへの udp/53 (DNS) / tcp/22 (SSH) 全許可」を削除した。
# - DNS: /etc/resolv.conf のリゾルバ宛てのみ許可する。全許可だと任意ホストへの
#   DNS トンネリングで持ち出しができてしまう
#   (Docker Desktop ではリゾルバは 192.168.65.x など内部ネットワーク上にいる)
# - SSH: GitHub への git+ssh は allowed-domains ipset (GitHub IP レンジ) で通る
while read -r ns; do
    echo "Allowing DNS to resolver $ns"
    iptables -A OUTPUT -d "$ns" -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d "$ns" -p tcp --dport 53 -j ACCEPT
done < <(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf)
# Allow localhost (sfw のローカルプロキシもここを通る)
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Create ipset with CIDR support
ipset create allowed-domains hash:net

# Fetch GitHub meta information and aggregate + add their IP ranges
echo "Fetching GitHub IP ranges..."
gh_ranges=$(curl -s https://api.github.com/meta)
if [ -z "$gh_ranges" ]; then
    echo "ERROR: Failed to fetch GitHub IP ranges"
    exit 1
fi

if ! echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null; then
    echo "ERROR: GitHub API response missing required fields"
    exit 1
fi

echo "Processing GitHub IPs..."
while read -r cidr; do
    if [[ ! "$cidr" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        echo "ERROR: Invalid CIDR range from GitHub meta: $cidr"
        exit 1
    fi
    echo "Adding GitHub range $cidr"
    ipset add allowed-domains "$cidr"
done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q)

# Resolve and add other allowed domains
# 解決失敗は警告して続行する: ここで abort すると DROP ポリシー適用前に
# スクリプトが止まり、firewall なしのまま起動してしまう (fail-open) ため
FAILED_DOMAINS=()
for domain in "${ALLOWED_DOMAINS[@]}"; do
    echo "Resolving $domain..."
    ips=$(dig +noall +answer A "$domain" | awk '$4 == "A" {print $5}')
    if [ -z "$ips" ]; then
        echo "WARNING: Failed to resolve $domain - skipping (this domain will be BLOCKED)"
        FAILED_DOMAINS+=("$domain")
        continue
    fi

    while read -r ip; do
        if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "ERROR: Invalid IP from DNS for $domain: $ip"
            exit 1
        fi
        echo "Adding $ip for $domain"
        ipset add allowed-domains "$ip" 2>/dev/null || true
    done < <(echo "$ips")
done

# Get host IP from default route
# (Docker Desktop では host.docker.internal もこのネットワークに含まれる)
HOST_IP=$(ip route | grep default | cut -d" " -f3)
if [ -z "$HOST_IP" ]; then
    echo "ERROR: Failed to detect host IP"
    exit 1
fi

HOST_NETWORK=$(echo "$HOST_IP" | sed "s/\.[0-9]*$/.0\/24/")
echo "Host network detected as: $HOST_NETWORK"

# Set up remaining iptables rules
iptables -A INPUT -s "$HOST_NETWORK" -j ACCEPT
iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT

# secrets-proxy への egress を許可する (docker の埋め込み DNS でサービス名を解決)。
# 秘密注入を要する Web/API トラフィックはすべてここを通る。直結はできない。
echo "Resolving secrets-proxy..."
PROXY_IPS=$(getent ahostsv4 secrets-proxy 2>/dev/null | awk '{print $1}' | sort -u || true)
if [ -z "$PROXY_IPS" ]; then
    echo "WARNING: could not resolve secrets-proxy - proxied traffic will be BLOCKED"
else
    while read -r pip; do
        echo "Allowing secrets-proxy $pip:8080"
        iptables -A OUTPUT -d "$pip" -p tcp --dport 8080 -j ACCEPT
    done < <(echo "$PROXY_IPS")
fi

# Set default policies to DROP first
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# First allow established connections for already approved traffic
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Then allow only specific outbound traffic to allowed domains
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT

# Explicitly REJECT all other outbound traffic for immediate feedback
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "Firewall configuration complete"
if [ "${#FAILED_DOMAINS[@]}" -gt 0 ]; then
    echo "WARNING: the following domains could not be resolved and are NOT allowed:"
    printf '  - %s\n' "${FAILED_DOMAINS[@]}"
fi
echo "Verifying firewall rules..."
if curl --connect-timeout 5 https://example.com >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - was able to reach https://example.com"
    exit 1
else
    echo "Firewall verification passed - unable to reach https://example.com as expected"
fi

# Verify GitHub API access
if ! curl --connect-timeout 5 https://api.github.com/zen >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - unable to reach https://api.github.com"
    exit 1
else
    echo "Firewall verification passed - able to reach https://api.github.com as expected"
fi
