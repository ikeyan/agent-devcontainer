#!/usr/bin/env bash
# redact サンドボックス専用の egress 制限。dev の init-firewall.sh より遥かに狭い:
# 許可するのは
#   (1) loopback (子 claude → 127.0.0.1:53 の dnsmasq)
#   (2) root (= entrypoint の DNS refresher) → upstream resolver の udp/tcp 53 だけ。
#       dnsmasq は転送せず hostsdir からのみ応答するので upstream を要しない。upstream を引くのは
#       root の refresher だけ (owner-match)。子 (node) の upstream 直撃は DROP し、dnsmasq 迂回
#       の DNS exfil を塞ぐ。
#   (3) Anthropic の HTTPS:443 (ipset allowed-domains。entrypoint/refresher が解決 IP を投入):
#       api.anthropic.com (推論) + platform.claude.com (Console 認証。公式が必須ドメインに挙げる)
# のみ。github / npm / pypi / secrets-proxy 等は一切許可しない (= git push もできない)。
# 両方とも Anthropic 管理なので「秘密を受け取りうる相手は Anthropic のみ」という境界は保たれる。
#
# enforcement の主役はこの iptables (カーネル強制)。dnsmasq は「子に転送させない」フィルタ。
# ipset/iptables の構築に失敗したら set -e で即死し、子は起動しない
# (fail-closed: 秘密を読む子を egress 開放のまま走らせない)。
#
# entrypoint から UPSTREAM_DNS (refresher の問い合わせ先) を受け取る。
set -euo pipefail
IFS=$'\n\t'

: "${UPSTREAM_DNS:?UPSTREAM_DNS 未設定}"

# Docker 埋め込み DNS (127.0.0.11) の nat ルールを flush 前に退避する。
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X

# 一旦 ACCEPT に戻す (構築途中の DROP で DNS 解決等が詰まらないように)。最後に DROP へ。
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT

# 退避した Docker DNS の nat ルールだけ復元する。
if [ -n "$DOCKER_DNS_RULES" ]; then
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
fi

# upstream への DNS は root (= entrypoint の refresher) からだけ許可する。dnsmasq は転送せず
# hostsdir からのみ応答するので upstream を要しない。upstream を引くのは root の refresher だけ。
# loopback 全許可より「前」に置くのが肝: upstream が 127.0.0.11 等の loopback 上にいても、
# 子 (node uid) からの upstream 直撃をここで DROP し、root だけ通す。
#
# --dport 53 は付けない: Docker 既定の upstream=127.0.0.11 は、復元した nat ルールが :53 を
# Docker DNS proxy の高ポートへ DNAT してから filter OUTPUT に来るため、--dport 53 では DNAT 後の
# パケットに当たらない (子の 127.0.0.11:53 直撃が後段の loopback ACCEPT へ素通りし迂回 = DNS exfil
# の穴になる)。宛先 IP + owner で突き合わせれば DNAT 後の高ポートも捕捉でき、外部 resolver
# (DNAT 無し) の場合も同様に安全。owner match は DNAT 後も発信 socket の uid を見るので機能する。
for proto in udp tcp; do
    iptables -A OUTPUT -d "$UPSTREAM_DNS" -p "$proto" -m owner --uid-owner 0 -j ACCEPT
done
iptables -A OUTPUT -d "$UPSTREAM_DNS" -j DROP

# loopback: OUTPUT は 127.0.0.1 (dnsmasq) だけに絞る。上の `-d UPSTREAM_DNS -j DROP` が既に
# 127.0.0.11 (Docker 埋め込み DNS) を dst IP で塞ぐ (DNAT は dst IP を変えず port だけ高ポートへ
# 移すので --dport 無しの dst IP マッチで DNAT 後も捕捉する) が、DNAT 先 IP 不変の前提に依存しない
# よう loopback egress 自体も宛先を 127.0.0.1 に限定する (子が 127.0.0.11 へ loopback 直撃して
# dnsmasq を迂回する経路を二重に塞ぐ)。INPUT は戻り用に全許可。refresher(root) の 127.0.0.11
# 問い合わせは上の owner-0 ACCEPT で通る。
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -d 127.0.0.1 -j ACCEPT

# 許可 IP を入れる ipset。中身 (Anthropic の解決 IP) は entrypoint の refresher が投入・更新する
# (このスクリプト呼び出し前に初期解決済み)。既に存在すれば残す。
ipset create allowed-domains hash:ip family inet 2>/dev/null || true

# 既定ポリシーを DROP に。
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# 確立済み接続の戻りを許可。
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Anthropic ドメイン (api.anthropic.com / platform.claude.com) への HTTPS だけ許可。
# どちらも Anthropic 管理 = 不可避な信頼境界 (モデル API + Console 認証)。
iptables -A OUTPUT -m set --match-set allowed-domains dst -p tcp --dport 443 -j ACCEPT

# それ以外は即時フィードバックのため REJECT。
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

# IPv6 は経路ごと塞ぐ (network を v6 無効にしてあるが、念のため全 DROP)。
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -F 2>/dev/null || true
    ip6tables -P INPUT DROP 2>/dev/null || true
    ip6tables -P FORWARD DROP 2>/dev/null || true
    ip6tables -P OUTPUT DROP 2>/dev/null || true
    ip6tables -A INPUT  -i lo -j ACCEPT 2>/dev/null || true
    ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
fi

# fail-closed の検証: 許可外に出られない / api.anthropic.com には出られる。
if curl --connect-timeout 5 -s -o /dev/null https://example.com 2>/dev/null; then
    echo "[redact-firewall] FATAL: 検証失敗 — example.com に到達できた" >&2
    exit 1
fi
if ! curl --connect-timeout 5 -s -o /dev/null https://api.anthropic.com/ 2>/dev/null; then
    echo "[redact-firewall] FATAL: 検証失敗 — api.anthropic.com に到達できない" >&2
    exit 1
fi

echo "[redact-firewall] locked: egress = Anthropic (api.anthropic.com + platform.claude.com):443 + dnsmasq DNS のみ"
