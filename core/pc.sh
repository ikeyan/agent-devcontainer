#!/bin/sh
# HTTP_PROXY を尊重しないコマンドを secrets-proxy 経由に通す薄いラッパ。
#   pc <cmd> [args...]      例: pc curl https://api.example.com/
# proxychains の [ProxyList] は数値 IP しか受けない (hostname 不可) ので、compose の secrets-proxy を
# 起動時に解決し、テンプレート /etc/proxychains4.conf の placeholder を実 IP へ差し替えた一時 conf で走る。
set -eu

ip=$(getent hosts secrets-proxy | awk 'NR==1 {print $1}')
if [ -z "${ip}" ]; then
	echo "pc: secrets-proxy を解決できない (compose network 上で実行しているか確認)" >&2
	exit 1
fi

conf=$(mktemp)
trap 'rm -f "$conf"' EXIT
sed "s/@@SECRETS_PROXY_IP@@/${ip}/" /etc/proxychains4.conf >"$conf"

exec proxychains4 -f "$conf" "$@"
