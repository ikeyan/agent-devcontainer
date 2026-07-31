#!/usr/bin/env bash
# redact サンドボックスの entrypoint。root で動き、egress を固めてから node に降格して
# 子 claude を起動する。流れ:
#   1. upstream resolver を控え、Anthropic 2 名を dig で初期解決して hostsdir + ipset を埋める
#   2. dnsmasq (転送しない hostsdir フィルタ resolver) を専用 uid で起動し、resolv.conf を向ける
#   3. init-firewall.sh で egress を Anthropic:443 + (root の) upstream DNS に固める
#   4. CDN ローテーション追従の DNS refresher を background で回す
#   5. node の ephemeral home に trust 種を入れ、事前認証 volume があれば OAuth access token を
#      env で渡し (cred ファイルは置かない)、node に降格して claude を起動
# 何か前提が崩れたら set -e で即死する (fail-closed = 秘密を読む子を無防備に走らせない)。
set -euo pipefail

APP="${REDACT_APP:?REDACT_APP 未設定 (launcher が渡す)}"
OUT="/workspace/extract/$APP"
RENDER=/run/redact
HOSTSDIR=/run/redact/hosts.d
ALLOWED_NAMES="api.anthropic.com platform.claude.com"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-/home/node/.claude}"
AUTH_RO=/root/.claude-auth   # 事前認証 volume の参照元。root 700 配下 = node(子) から不可視

[ -r /input/raw.flows ] || { echo "FATAL: /input/raw.flows が読めない" >&2; exit 1; }
[ -r "$OUT/redact_flow.py" ] || { echo "FATAL: $OUT/redact_flow.py が無い (launcher が scaffold するはず)" >&2; exit 1; }

mkdir -p "$RENDER" "$HOSTSDIR"

# 1) upstream resolver を resolv.conf から拾う (resolv.conf を書き換える前に控える)。
UPSTREAM_DNS="$(awk '$1=="nameserver"{print $2; exit}' /etc/resolv.conf)"
[ -n "$UPSTREAM_DNS" ] || { echo "FATAL: upstream resolver 不明" >&2; exit 1; }
DNS_USER=redact-dns

# 許可 IP セット。中身 (Anthropic の解決 IP) は下の refresher が投入・更新する。
ipset create allowed-domains hash:ip family inet 2>/dev/null || true

# Anthropic 2 名を upstream から dig で直接引き、hostsdir の A レコードと ipset を更新する。
# 子に DNS 転送をさせない設計のキモ: dnsmasq は転送せず hostsdir からのみ応答するので、CDN
# ローテーション追従はこの root refresher が担う (firewall は root→upstream だけ許可する)。
# 全許可名を解決して hostsdir + ipset を更新する。一部でも解決に失敗したら非0を返す
# (初期ゲートは全名の解決を要求 = SECURITY-MODEL.md 不変条件 8。refresher は解決できた名前だけ
#  都度更新する best-effort なので戻り値を || true で無視する)。
resolve_into_hostsdir() {
    local name ips ip tmp rc=0
    for name in $ALLOWED_NAMES; do
        ips="$(dig +short +time=3 +tries=1 "@$UPSTREAM_DNS" "$name" A 2>/dev/null \
               | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
        [ -n "$ips" ] || { rc=1; continue; }
        tmp="$(mktemp "$RENDER/.hosts.XXXXXX")"
        while IFS= read -r ip; do
            printf '%s %s\n' "$ip" "$name" >> "$tmp"
            ipset add allowed-domains "$ip" 2>/dev/null || true
        done <<<"$ips"
        chmod 644 "$tmp"            # dnsmasq (redact-dns) が読めるように
        mv -f "$tmp" "$HOSTSDIR/$name"   # 同一 fs で atomic に差し替え
    done
    return $rc
}

# 初期解決 (firewall 適用前 = upstream 到達可能なうちに hostsdir と ipset を埋める)。
resolve_into_hostsdir || { echo "FATAL: 許可名 ($ALLOWED_NAMES) の初期解決に失敗" >&2; exit 1; }

# 2) dnsmasq を起動 (hostsdir を読む / 転送しない)。子が引くのは dnsmasq だけにする。
dnsmasq --conf-file=/etc/redact/dnsmasq.conf --user="$DNS_USER" --pid-file="$RENDER/dnsmasq.pid"
printf 'nameserver 127.0.0.1\noptions ndots:0\n' > /etc/resolv.conf

# dnsmasq が hostsdir 由来で応答するまで待つ (この時点で OUTPUT はまだ ACCEPT)。
ok=
for _ in $(seq 1 25); do
    if getent hosts api.anthropic.com >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.2
done
[ -n "$ok" ] || { echo "FATAL: dnsmasq 経由で api.anthropic.com を解決できない" >&2; exit 1; }

# 3) egress をロックする (upstream DNS は root の refresher だけ、HTTPS は Anthropic だけ)。
UPSTREAM_DNS="$UPSTREAM_DNS" /usr/local/bin/redact-init-firewall.sh

# 4) CDN ローテーション追従: root の refresher を background で回す (30s 毎に hostsdir+ipset 更新)。
#    exec で子に置き換わった後も別プロセスとして残り、コンテナ終了で kill される。
( while sleep 30; do resolve_into_hostsdir || true; done ) &

# 5) 子 claude の home は ephemeral: volume を rw マウントしていない = image の writable layer
#    (node 所有) を使う。--rm で消えるので、子が raw 秘密を home へ退避しても永続しない
#    (durable spill 防止)。home は node 所有で、root は cap_drop ALL+限定 cap_add のため
#    DAC_OVERRIDE/CHOWN を持たず node 所有 dir に書けない。よって trust 種は runuser で node
#    として行う。
#
# settings の Write/Edit allow と interview.md は __APP__ を実 app 名へ展開してから渡す
# (未展開だと __APP__ ディレクトリ宛になり子の編集が拒否される)。setup_home.py も RENDER に置く。
sed "s|__APP__|$APP|g" /etc/redact/interview.md > "$RENDER/interview.md"
sed "s|__APP__|$APP|g" /etc/redact/settings.json > "$RENDER/settings.json"
cat > "$RENDER/setup_home.py" <<'PY'
import json, os
home = os.environ["CLAUDE_HOME"]
os.makedirs(home, exist_ok=True)
# claude に /workspace を最初から信頼させる (folder trust dialog で対話が止まらないように)。
# per-project trust 状態は .claude.json に入るが、CLAUDE_CONFIG_DIR が .claude.json 自体まで
# 移すのか .claude/ ディレクトリだけなのかは版依存で公式に未文書。さらに claude 起動時に HOME を
# 明示していない (runuser 既定の HOME=node home に依存)。取り違えると初回起動が trust dialog で
# 止まるので、$CLAUDE_CONFIG_DIR/.claude.json と node home の ~/.claude.json の両方へ種を書く。
node_home = os.environ.get("NODE_HOME") or os.path.expanduser("~")
targets, seen = [], set()
for p in (os.path.join(home, ".claude.json"), os.path.join(node_home, ".claude.json")):
    rp = os.path.realpath(p)
    if rp not in seen:
        seen.add(rp)
        targets.append(p)
for p in targets:
    try:
        with open(p) as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    entry = d.setdefault("projects", {}).setdefault("/workspace", {})
    entry["hasTrustDialogAccepted"] = True
    entry["projectOnboardingSeenCount"] = max(1, entry.get("projectOnboardingSeenCount", 0))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(p, 0o600)
PY
chmod -R a+rX "$RENDER"

runuser -u node -- env CLAUDE_HOME="$CLAUDE_HOME" NODE_HOME=/home/node \
    python3 "$RENDER/setup_home.py"

# 事前認証 volume ($AUTH_RO は root 700 配下 = node から不可視) があれば、OAuth *access* token
# だけを取り出し env で claude へ渡す。.credentials.json を node の home にも node 可視パスにも
# 置かない設計の要:
#   - prompt-injection された tool 子プロセス (node) が cat できる cred ファイルが存在しない。
#   - refresh token は持ち込まない。access token は時限的なので、万一 /proc/<pid>/environ を
#     同一 uid で読まれても被害は短命、かつ egress 固定でネット流出も不可。
# token は export して runuser に環境ごと引き継がせる (argv に出さない = /proc/cmdline 非露出)。
if [ -r "$AUTH_RO/.credentials.json" ]; then
    tok="$(python3 - "$AUTH_RO/.credentials.json" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
for path in (("claudeAiOauth", "accessToken"), ("oauth", "accessToken"), ("accessToken",), ("access_token",)):
    cur, ok = d, True
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            ok = False
            break
    if ok and isinstance(cur, str) and cur:
        sys.stdout.write(cur)
        sys.exit(0)
# 取り出せない: 値は出さず top-level キーだけ stderr に出してデバッグの手がかりにする。
sys.stderr.write("keys=%r\n" % (list(d.keys()) if isinstance(d, dict) else type(d).__name__))
sys.exit(1)
PY
)"
    if [ -n "$tok" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$tok"
        unset tok
        echo "auth: 事前認証 volume の OAuth access token を env で渡す (cred ファイルは置かない)"
    else
        echo "WARN: $AUTH_RO/.credentials.json から accessToken を取り出せなかった。ANTHROPIC_API_KEY があればそれを使う。" >&2
    fi
fi

# claude を起動する。MCP は --strict-mcp-config + 設定無しで無効、テレメトリは env で無効。
#   --safe-mode: auth volume / 被 redact repo の .claude (hooks/plugins/CLAUDE.md/skills/commands/
#     agents 等) を一切読み込まず、子を常に同一・最小構成で起動する (auth・権限・built-in tools は
#     通常動作)。封じ込めの実体は OS mount (repo は ro、書けるのは extract/<app> のみ) と firewall
#     なので、これは予測可能性のための hygiene。要 claude >= 2.1.169。
#   --dangerously-skip-permissions: 権限プロンプトを出さず全ツールを即実行する。封じ込めは
#     permission allowlist ではなく OS mount + firewall が担う設計なので、対話の摩擦を消すために
#     有効化する (node 実行 = root 拒否に当たらない)。allow ルールは無意味になるが、cred の実保護は
#     egress 固定 + ephemeral home + 「cred ファイルを置かない (token は env で短命 access のみ)」
#     なので問題にならない (settings の deny は念のため残す)。
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 (=1 にはしない): =1 は cred 隔離を bubblewrap で行うため
# cap_drop:ALL (CAP_SYS_ADMIN 無し) では claude が起動不能。かつ scrub は同一 uid の子には無効で、
# cred の実保護は別層 (egress 固定 + ephemeral home + cred ファイルを置かない) が担う。詳細は
# canon: facts/claude-code/subprocess-env-scrub-requires-bwrap / SECURITY-MODEL.md 不変条件 2。
cd /workspace
exec runuser -u node -- \
    env CLAUDE_CONFIG_DIR="$CLAUDE_HOME" \
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
        CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 \
        DISABLE_TELEMETRY=1 \
        DISABLE_ERROR_REPORTING=1 \
        DISABLE_AUTOUPDATER=1 \
    claude \
        --safe-mode \
        --dangerously-skip-permissions \
        --strict-mcp-config \
        --settings "$RENDER/settings.json" \
        --append-system-prompt "$(cat "$RENDER/interview.md")"
