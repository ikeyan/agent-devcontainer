#!/bin/bash
# secrets-proxy が共有ボリュームに publish した CA 公開証明書を、dev コンテナの
# システム信頼ストアに追加する。これにより proxy が MITM する HTTPS 接続を
# curl / git / node / python が検証できるようになる。
# (直結する NO_PROXY ホストは実物の証明書のままなので影響しない: 追記であって置換ではない)
# postStartCommand から sudo で実行される (sudoers で許可済み)。
set -euo pipefail

CA_SRC=/etc/secrets-proxy-ca/proxy-ca-cert.pem
CA_DST=/usr/local/share/ca-certificates/secrets-proxy.crt

echo "Waiting for secrets-proxy CA at $CA_SRC ..."
for _ in $(seq 1 60); do
  [ -f "$CA_SRC" ] && break
  sleep 1
done

if [ ! -f "$CA_SRC" ]; then
  echo "ERROR: secrets-proxy CA not found after 60s (is the secrets-proxy container up?)" >&2
  exit 1
fi

install -m 644 "$CA_SRC" "$CA_DST"
update-ca-certificates >/dev/null
echo "Installed secrets-proxy CA into the system trust store."
