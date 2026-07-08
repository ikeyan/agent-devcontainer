#!/usr/bin/env bash
# Docker の `internal:` network が「埋め込み DNS (127.0.0.11) で外部名を**転送しない**」ことを
# 実機で pin する回帰テスト。redact サンドボックスの egress 封じ込めを iptables/dnsmasq から
# 「internal network + MITM proxy」へ寄せる設計は、この 1 点に依存する:
#
#   internal net 上のコンテナは
#     - peer (コンテナ/サービス) 名は解決できる  → proxy へ名前で到達できる
#     - 外部名は SERVFAIL で解決できない          → DNS exfil 経路が構造的に塞がる
#
# これが崩れる (internal net が外部 DNS を転送し始める) と、子は <secret>.attacker.com を引く
# だけで秘密を持ち出せてしまう。だから fail-closed の負プローブにする: internal net で外部名が
# **解決できてしまったら FAIL**。
#
# 健全性のための二重コントロールで誤判定を防ぐ:
#   A. 通常 (非 internal) net では外部名が解決できる   → この環境に転送可能な upstream が在る
#      (無ければ「転送是非」を検査しても無意味なので SKIP)。
#   B. internal net で peer 名は解決できる             → 埋め込み DNS 自体は生きている
#      (死んでいれば外部名失敗は当然なので判定不能 → SKIP)。
# A と B が揃った時だけ本命アサーション (internal net で外部名は解決できない) を課す。
#
# pin するのは docker (embedded DNS) の挙動。podman は DNS 実装が別物 (aardvark-dns) なので対象外。
# 重い & docker daemon/ネットを使うので既定の `make check` 集約には入れない (check-redact-image と
# 同じ扱い) — kit CI と投入ホストで明示実行する。出典: docs/verified-facts/docker.md
# 「internal network の DNS」。
set -euo pipefail

docker info >/dev/null   # daemon 必須 (無ければ docker 自身のエラーで止まる)

# busybox (nslookup 入り) を 1 つ確保する。Docker Hub は無認証 pull が rate-limit されがちなので
# rate-limit の無い MCR を先頭に複数 registry を試す。REDACT_DNSTEST_IMAGE で明示上書き可。
# コンテナを起動する前にローカルへ落とす。
IMG="${REDACT_DNSTEST_IMAGE:-}"
if [ -z "$IMG" ]; then
    for cand in \
        mcr.microsoft.com/azurelinux/busybox:1.36 \
        busybox:latest \
        public.ecr.aws/docker/library/busybox:latest
    do
        if docker image inspect "$cand" >/dev/null 2>&1 || docker pull -q "$cand" >/dev/null 2>&1; then
            IMG="$cand"; break
        fi
    done
fi
docker image inspect "$IMG" >/dev/null 2>&1 \
    || { echo "FATAL dns-egress: busybox 像を用意できない (REDACT_DNSTEST_IMAGE で指定可)" >&2; exit 1; }

INT_NET="redact-dnstest-int-$$"
EXT_NET="redact-dnstest-ext-$$"
PEER="redact-dnstest-peer-$$"
EXT_NAME="example.com"   # 実在し、転送が生きていれば必ず解決できる外部名 (IANA 予約ドメイン)

cleanup() {
    docker rm -f "$PEER" >/dev/null 2>&1 || true
    docker network rm "$INT_NET" "$EXT_NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal "$INT_NET" >/dev/null
docker network create "$EXT_NET" >/dev/null

# internal フラグが本当に立っているか確認 (前提が崩れていれば即死)。
[ "$(docker network inspect "$INT_NET" --format '{{.Internal}}')" = "true" ] \
    || { echo "FATAL dns-egress: $INT_NET が internal でない" >&2; exit 1; }

# nslookup を $1=network で実行し、解決できたら 0 を返す。
# busybox nslookup は解決成功時だけ `Name:` 行を出す (SERVFAIL/NXDOMAIN では出さない)。
# server 行は `Address:\t127.0.0.11:53` なので `Name:` 行で判定すれば server を誤検知しない。
# --dns-search=. で継承 search domain を消す: host の resolv.conf に search domain があると
# (例: kit CI の Azure runner `*.bx.internal.cloudapp.net`)、busybox nslookup は ndots:0 でも bare 名に
# それを付けて引き SERVFAIL になり peer 名解決 (control B) が空振りする。search を外すと bare 名を
# absolute で引く。本命の負プローブにも効く — search 付きだと外部名が search domain 経由で解決して
# 転送の有無を覆い隠しうるが、absolute 固定ならそれが無い (kit CI docker 28.0.4 で確認)。
resolves_on() {  # $1=net $2=name
    local out
    out="$(timeout 15 docker run --rm --dns-search=. --network "$1" "$IMG" nslookup "$2" 2>&1 || true)"
    printf '%s\n' "$out" | grep -qE '^Name:[[:space:]]'
}

# 解決できるはずの名前 (健全性コントロール A/B 用) を、解決するまで数回試す。upstream の初回 miss や
# peer の埋め込み DNS 登録 (`docker run -d` の戻りは登録完了を待たない) で 1 発目が空振りしうるため。
# 本命の負プローブ (internal net で外部名が解決しては困る) には使わない — retry で false pass を作らない。
resolves_soon() {  # $1=net $2=name
    local _
    for _ in 1 2 3 4 5; do
        resolves_on "$1" "$2" && return 0
        sleep 1
    done
    return 1
}

# A) 健全性: 通常 net で外部名が解決できる = この環境に転送可能な upstream が在る。
if ! resolves_soon "$EXT_NET" "$EXT_NAME"; then
    echo "SKIP dns-egress: 通常 net でも $EXT_NAME を解決できない (この環境に外部 DNS 経路が無い) → 転送是非を検査不能"
    exit 0
fi

# B) 健全性: internal net で peer 名は解決できる = 埋め込み DNS 自体は生きている。
docker run -d --rm --name "$PEER" --network "$INT_NET" "$IMG" sleep 120 >/dev/null
if ! resolves_soon "$INT_NET" "$PEER"; then
    echo "SKIP dns-egress: internal net で peer 名すら解決できない (埋め込み DNS 不調?) → 判定不能"
    exit 0
fi

# 本命 (fail-closed の負プローブ): internal net で外部名は解決できてはいけない。
if resolves_on "$INT_NET" "$EXT_NAME"; then
    echo "FATAL dns-egress: internal net が $EXT_NAME を解決した = 外部 DNS 転送が生きている (exfil 穴)" >&2
    exit 1
fi

echo "ok  dns-egress (internal net: peer 名は解決 / 外部名は転送せず SERVFAIL) [img=$IMG]"
