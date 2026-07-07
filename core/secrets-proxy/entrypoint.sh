#!/usr/bin/env bash
# secrets-proxy のエントリポイント:
#   1. 安定した CA を1度だけ生成し、公開証明書だけを共有ボリュームに publish する
#      (秘密鍵 = mitmproxy-ca.pem はこのコンテナ内に留め、dev には渡さない)
#   2. 保存済みの BW_SESSION で Vaultwarden の vault を解錠する
#   3. mitmdump (addon.py) を起動する。addon が BW_SESSION 経由で秘密を解決する
#
# 認証情報は bootstrap で対話入力し、得られた BW_SESSION だけを
# proxy 専用ボリューム (proxy-bw = $BITWARDENCLI_APPDATA_DIR) に保存する。
# dev にはこのボリュームも BW_SESSION も渡らない。
#
# bootstrap モード (初回 1 回):
#   make -C .devcontainer/core bootstrap
#   (単一 -f の生 compose を直に叩くと project 名が `core` になり session が別 volume namespace
#    (core_proxy-bw) へ書かれ、通常起動 (project 名 tools) から読めず復旧不能ループになる。make target は
#    project 層込みの 2 ファイル -f で project 名 tools を保つ。docs/verified-facts/devcontainers-cli.md)
#   BW_SERVER / API キー / マスターパスワードを対話入力 → login + unlock し、
#   BW_SESSION を $SESSION_FILE に保存する。マスターパスワードは保存しない。
set -euo pipefail

CONFDIR="${MITM_CONFDIR:-/home/mitm/.mitmproxy}"
CA_OUT=/ca/proxy-ca-cert.pem
export BITWARDENCLI_APPDATA_DIR="${BITWARDENCLI_APPDATA_DIR:-/home/mitm/.bw}"
# BW_SESSION の保存先。proxy-bw ボリューム内なのでリビルドを跨いで永続する。
SESSION_FILE="${BW_SESSION_FILE:-$BITWARDENCLI_APPDATA_DIR/session.key}"

mkdir -p "$CONFDIR" "$BITWARDENCLI_APPDATA_DIR" /ca

# bw の vault 状態 (unauthenticated|locked|unlocked) を取り出すヘルパ。
# python:3.12-slim には jq が無いので python で JSON を読む。
bw_status() {
  bw status 2>/dev/null \
    | python -c 'import sys,json;print(json.load(sys.stdin).get("status","?"))' 2>/dev/null \
    || echo '?'
}

# --- bootstrap モード ------------------------------------------------------
# 認証情報は全てここで対話入力する (env で渡してあればそれを優先)。stdout には
# 何も秘密を出さない (BW_SESSION はファイルに保存する)。プロンプトは stderr へ。
if [ "${1:-}" = "bootstrap" ]; then
  server="${BW_SERVER:-}"
  if [ -z "$server" ]; then
    read -r -p "[bootstrap] Vaultwarden/Bitwarden URL (BW_SERVER 未設定): " server || true
  fi
  bw config server "$server" >/dev/null
  echo "[bootstrap] server = $server" >&2

  # 既に login 済み (proxy-bw に data.json がある) ならスキップ
  if ! bw login --check >/dev/null 2>&1; then
    cid="${BW_CLIENTID:-}"
    csec="${BW_CLIENTSECRET:-}"
    if [ -z "$cid" ]; then
      read -r -p "[bootstrap] API client_id (BW_CLIENTID): " cid || true
    fi
    if [ -z "$csec" ]; then
      read -rs -p "[bootstrap] API client_secret (BW_CLIENTSECRET): " csec || true
      echo >&2
    fi
    [ -n "$cid" ] && [ -n "$csec" ] || { echo "[bootstrap] FATAL: client_id/secret が空です" >&2; exit 1; }
    echo "[bootstrap] bw login --apikey" >&2
    BW_CLIENTID="$cid" BW_CLIENTSECRET="$csec" bw login --apikey --nointeraction >&2
    unset cid csec
  fi

  # apikey login 直後は locked。マスターパスワードで unlock してセッションを得る。
  echo "[bootstrap] vault を unlock します (マスターパスワードはどこにも保存しません)" >&2
  read -rs -p "[bootstrap] master password: " _pw || true
  echo >&2
  [ -n "$_pw" ] || { echo "[bootstrap] FATAL: master password が空です" >&2; exit 1; }
  session="$(BW_PWD="$_pw" bw unlock --passwordenv BW_PWD --raw)"
  unset _pw

  # セッションを proxy 専用ボリュームに保存 (mitm のみ読める 600)
  ( umask 077; printf '%s' "$session" > "$SESSION_FILE" )
  chmod 600 "$SESSION_FILE"
  BW_SESSION="$session" bw sync >/dev/null 2>&1 || true
  echo "[bootstrap] BW_SESSION を $SESSION_FILE に保存しました。" >&2
  echo "[bootstrap] 完了。devcontainer を (再)起動すれば proxy が起動します (VS Code: Reopen in Container / CLI: devcontainer up --workspace-folder .)。" >&2
  exit 0
fi

# 0) rules.yaml の解決 -----------------------------------------------------
# /config-src は secrets-proxy ディレクトリの read-only マウント。
# rules.yaml は必須 (無ければ起動しない)。書き込み可能な home へコピーして使う。
# ルールの構造は rules.schema.json (JSON Schema) を参照。
RULES_SRCDIR="${SECRETS_PROXY_RULES_SRCDIR:-/config-src}"
RULES_OUT=/home/mitm/rules.yaml
if [ -f "$RULES_SRCDIR/rules.yaml" ]; then
  cp "$RULES_SRCDIR/rules.yaml" "$RULES_OUT"
else
  echo "[entrypoint] FATAL: rules.yaml が見つかりません ($RULES_SRCDIR/rules.yaml)" >&2
  exit 1
fi
export SECRETS_PROXY_RULES="$RULES_OUT"

# 1) CA --------------------------------------------------------------------
if [ ! -f "$CONFDIR/mitmproxy-ca.pem" ]; then
  echo "[entrypoint] generating proxy CA"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout /tmp/ca.key -out /tmp/ca.crt -subj "/CN=secrets-proxy CA" >/dev/null 2>&1
  # mitmproxy は confdir/mitmproxy-ca.pem (鍵+証明書) を CA として使う
  cat /tmp/ca.key /tmp/ca.crt > "$CONFDIR/mitmproxy-ca.pem"
  cp /tmp/ca.crt "$CONFDIR/mitmproxy-ca-cert.pem"
  shred -u /tmp/ca.key 2>/dev/null || rm -f /tmp/ca.key
  rm -f /tmp/ca.crt
fi
cp "$CONFDIR/mitmproxy-ca-cert.pem" "$CA_OUT"
chmod 644 "$CA_OUT"
echo "[entrypoint] published CA cert -> $CA_OUT"

# 2) Vaultwarden unlock (rules が bw(item) 源を使う時だけ) -------------------
# rules.yaml が Vaultwarden(item) 源を1つでも使うなら、bootstrap で保存した $SESSION_FILE の
# BW_SESSION で vault を解錠する。env/file だけ (または inject 無し) の rules は Vaultwarden を
# 必要としない (rules.schema.json / addon.py がそう謳う最小サンドボックス向けの源) ので、unlock を
# 省略してそのまま mitmdump を起動する。判定は /app/rules_bw.py (addon と同じ「item キー = bw」定義)。
# rc==1 (bw 源なし) の時だけ省略し、それ以外 — item あり(0) / rules を parse 不能(0) /
# rules_bw 自体の実行失敗(>=2) — は全て fail-closed で BW を要求する。
rc=0
python /app/rules_bw.py "$RULES_OUT" || rc=$?
if [ "$rc" -eq 1 ]; then
  echo "[entrypoint] rules に Vaultwarden(item) 源が無い → BW_SESSION 解錠を省略 (env/file のみ)"
else
  # bootstrap で保存した $SESSION_FILE から読む。
  if [ ! -s "$SESSION_FILE" ]; then
    echo "[entrypoint] FATAL: BW_SESSION が未保存です ($SESSION_FILE)。" >&2
    echo "[entrypoint] 1度だけ bootstrap して認証情報を入力してください:" >&2
    echo "[entrypoint]   make -C .devcontainer/core bootstrap" >&2
    echo "[entrypoint] proxy は起動せず、dev も fail-close で起動しません。" >&2
    exit 1
  fi
  BW_SESSION="$(cat "$SESSION_FILE")"
  export BW_SESSION

  # BW_SESSION が実際に vault を解錠できるか検証する。
  #   unlocked        … OK
  #   locked          … セッション失効/無効
  #   unauthenticated … login 状態が失われた (proxy-bw が消えた等)
  status="$(bw_status)"
  if [ "$status" != "unlocked" ]; then
    echo "[entrypoint] FATAL: 保存済み BW_SESSION で vault を解錠できません (status=$status)。" >&2
    echo "[entrypoint] セッション失効の可能性があります。再 bootstrap してください:" >&2
    echo "[entrypoint]   make -C .devcontainer/core bootstrap" >&2
    exit 1
  fi
  bw sync >/dev/null 2>&1 || true
fi

# 3) proxy 起動 ------------------------------------------------------------
# 既定は mitmdump (ヘッドレス)。SECRETS_PROXY_WEB=1 のときだけ mitmweb (Web UI 付き) にする。
# Web UI の公開/隔離 (dev から到達不可な admin IP + host loopback publish) は override
# (compose.mitmweb.yaml) が与える。flag は mitmproxy 11.1.3 で確認済み
# (docs/verified-facts/mitmproxy.md)。
COMMON_OPTS=(
  --listen-host 0.0.0.0 --listen-port 8080
  --set confdir="$CONFDIR"
  --set block_global=false
  --set stream_large_bodies=10m
  -s /app/addon.py
)
if [ "${SECRETS_PROXY_WEB:-0}" = "1" ]; then
  web_port="${SECRETS_PROXY_WEB_PORT:-8081}"
  # web_host は既定 127.0.0.1 (container loopback)。dev は別コンテナなので、route_localnet=0 の下では
  # 他コンテナの loopback へ到達できない (martian drop。weak host model でも迂回不可)。よって publish は
  # 張らず、host は ../proxy-web-tunnel.py が docker exec で張る踏み台経由でだけ Web UI に届く
  # (dev は docker socket を持たず exec できない)。これで token に頼らず経路でも dev を締め出す。
  web_host="${SECRETS_PROXY_WEB_HOST:-127.0.0.1}"
  # 認証は mitmweb 既定で常時 ON。ただし web_open_browser=false だと mitmweb は token を一切
  # ログに出さない (webaddons.py: web_url を出すのは browser 起動失敗パスのみ。v11.1.3 で確認、
  # docs/verified-facts/mitmproxy.md)。よって token をこちらで生成し web_password に固定して URL を
  # 表示する。?token=<web_password> がそのまま認証に通る (is_valid_password = compare_digest)。
  # host からは 127.0.0.1 に publish しているので 127.0.0.1 で開く。
  web_token="$(python -c 'import secrets;print(secrets.token_hex(16))')"
  [ -n "$web_token" ] || { echo "[entrypoint] FATAL: web token の生成に失敗" >&2; exit 1; }
  echo "[entrypoint] starting mitmweb: proxy :8080, web UI ${web_host}:${web_port}"
  echo "[entrypoint] mitmweb open: http://127.0.0.1:${web_port}/?token=${web_token}"
  # flow_detail は mitmweb に無いオプションなので渡さない (docs/verified-facts/mitmproxy.md)。
  exec mitmweb "${COMMON_OPTS[@]}" \
    --set web_open_browser=false \
    --set web_host="${web_host}" \
    --set web_port="${web_port}" \
    --set web_password="${web_token}"
else
  echo "[entrypoint] starting mitmdump on :8080"
  exec mitmdump "${COMMON_OPTS[@]}" --set flow_detail=0
fi
