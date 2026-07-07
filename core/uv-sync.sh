#!/usr/bin/env bash
# uv の project env (Dockerfile の UV_PROJECT_ENVIRONMENT=/workspace/.devcontainer/.venv。compose の
# named volume でコンテナ専用) を作る/更新する。初回 (やロック更新時) だけでよい。以後 `uv run` は既存 env を使う。
#
# interpreter 選択 (system python 3.13 / managed CPython を取得しない) は Dockerfile の
# UV_PYTHON_DOWNLOADS=never と .python-version=3.13 が担う。本スクリプト固有の仕事は CA の結合だけ:
# sfw CA 単体では uv が実物 CA を検証できず UnknownIssuer で落ちるので system バンドルと結合して渡し、
# 実体 uv (/usr/local/bin/uv) を直接呼ぶ (shim 経由だと内側 sfw が結合バンドルを潰す)。機構と出典は
# docs/verified-facts/network.md「uv / sfw の CA 結合」。
set -euo pipefail

# $SSL_CERT_FILE / $@ は内側の sfw bash で展開させたいので単一引用符のまま (SC2016 は意図通り)
# shellcheck disable=SC2016
exec sfw bash -c '
  set -euo pipefail
  bundle=$(mktemp)
  trap "rm -f \"$bundle\"" EXIT
  # sfw がこの実行用に発行した CA ($SSL_CERT_FILE) を system バンドルへ結合
  # (間に改行を挟み、system バンドル末尾が改行なしでも PEM 境界が融合しないようにする)
  { cat /etc/ssl/certs/ca-certificates.crt; echo; cat "$SSL_CERT_FILE"; } > "$bundle"
  SSL_CERT_FILE="$bundle" UV_SYSTEM_CERTS=1 /usr/local/bin/uv sync "$@"
' _ "$@"
