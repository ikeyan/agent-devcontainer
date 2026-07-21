#!/usr/bin/env bash
# redact サンドボックスの「意味的」不変条件を回帰 pin する (make check-redact-invariants)。
#
# core/Makefile の check-* は構文を見る (bash -n / JSON / docker compose config /
# dnsmasq --test)。だが Codex review が捕まえた不具合は「構文的には valid だが意味が違う」もの
# ばかりだった (有効な JSON だがパスの解釈が違う / 有効な dnsmasq 設定だが転送してしまう 等)。
# ここではそれらを文字列/挙動レベルで pin し、回帰したら fail させる。各規則に negative probe を
# 併設し、検査器自体が違反を実際に弾くことを担保する。副作用は mktemp に隔離して残さない。
#
# pin する不変条件 (括弧内は元の Codex 指摘番号):
#   1. core/bin/redact-flow は危険な app 名 (. / .. / 不正文字 / 空) を、mkdir/docker より前に exit 2 で
#      弾く。app 名はディレクトリ名兼 mount パスなので「外部値→パス」の封じ込めクラス (#2/#10)。
#   2. dnsmasq.conf は転送しない: no-resolv があり server=/.../ が無い。server=/host/ は suffix 一致で
#      <secret>.api.anthropic.com まで upstream へ流す DNS exfil 経路になる (#8)。
#   3. __APP__ を含む runtime テンプレ (interview.md / settings.json) を entrypoint が展開し、claude
#      には展開後 ($RENDER) を渡す。未展開だと __APP__ ディレクトリ宛になり子の編集が拒否される (#3)。
#   4. settings.json は writable mount を // 絶対パスで許可する。単一 / は project-root 相対と解釈され
#      (/workspace/workspace/extract/...)、許可が writable bind と一致しなくなる (#5)。
#   5. compose.yaml の NO_PROXY (project/.env の PROJECT_NO_PROXY 込み) は proxy 経由ホスト (rules.yaml の
#      api/uploads.github.com) を suffix 一致でバイパスしない。NO_PROXY は末尾一致なので bare github.com が
#      api.github.com (gh の substitute 先) を直結させ token 置換が無効化された回帰の pin (curl/Go 共通。
#      docs/verified-facts/network.md「proxy bypass / NO_PROXY」)。
#   6. secrets-proxy の Web UI (mitmweb) は既定 mitmdump / opt-in の分岐を保ち、bind は container loopback
#      (127.0.0.1) 固定・token (web_password) 必須。0.0.0.0/:: 化や token 抜けは平文 cred が dev/LAN から
#      見える経路になる (SECURITY-MODEL 不変条件 1/4)。
#   7. secrets-proxy Web UI の override (compose.mitmweb.yaml) は 8081 を publish せず web_host も上書きしない
#      (loopback bind 方針を崩さない)。既定 compose.yaml 側には Web UI 設定を一切漏らさない。
#   8. devcontainer.json (project 所有) は core が要求する配線を保つ: dockerComposeFile が
#      [project/compose.yaml, core/compose.yaml] の順 (.env 補間と後勝ちの前提)、settings.local.json の
#      local-mask、postStartCommand の firewall+CA 再実行。
#   9. project/allow-domains.txt は「core/Dockerfile が COPY し init-firewall.sh が読む」の 2 点が揃って
#      初めて効く (project 層の直結許可ドメイン抽出。片方が外れると fail-silent で許可されなくなる)。
#  10. マージ済み compose config のセキュリティ姿勢を直接 pin する: dev の cap 集合 / privileged 不在 /
#      network 所属 / secrets-proxy 専用 volume の非漏洩 / docker.sock bind 不在 / project の name: 宣言。
#      根拠: 複数 -f の「後勝ちで core 優先」が効くのは core が明示宣言したスカラー限定で、list merge
#      (cap_add/networks/volumes) や core 未宣言キーは project 値が通る (docs/verified-facts/docker.md
#      「compose 複数 -f」)。ゆえに後勝ちの一般則に頼らず「マージ結果そのもの」を検査する。
#  11. 複数ファイルに同じ literal が現れて初めて機能する配線 (PROJECT_GH_USER の sudoers env_keep /
#      compose environment / init-firewall reader の 3 点、redact auth volume 名の redact-flow /
#      redact/compose.yaml の 2 点) の一致。片側だけの改名は CI 緑のまま実行時に fail する (§9 と同類)。
#
# 方針: #1〜#9 は git と grep だけで動く。#10 だけは compose config が要る (docker/podman compose +
# PyYAML)。CI/devcontainer には両方あるので常時走らせ、無い環境では #10 が自然エラーになる (skip しない
# — green が「全 pin が実際に走った」を意味する原則を保つ)。
set -euo pipefail

# core/ の親 (consumer では .devcontainer/、kit repo ではリポジトリ root) を基準にする。
# git root 基準だと kit repo (core/ が直下) でパスが壊れる。
cd "$(cd "$(dirname "$0")/../.." && pwd)"

R=core/redact
P=project
fail() { echo "NG: $*" >&2; exit 1; }

# --- 1. redact-flow の app 名検証 (negative probe = 拒否方向だけ pin) ---------
# 正常名は docker build まで進む (重い副作用) ので走らせない。危険名が検証段階で exit 2 する
# ことだけ確認する (どれも mkdir / docker 到達前に弾かれるので副作用なし)。
for bad in '.' '..' 'a/b' '../x' 'a b' ''; do
    ec=0
    core/bin/redact-flow "$bad" /dev/null >/dev/null 2>&1 || ec=$?
    [ "$ec" -eq 2 ] || fail "redact-flow が不正 app 名 '$bad' を弾かない (exit=$ec)"
done

# --- 2. dnsmasq は転送しない -------------------------------------------------
grep -q '^no-resolv' "$R/dnsmasq.conf" || fail "dnsmasq.conf に no-resolv が無い (転送停止が外れた)"
! grep -Eq '^[[:space:]]*server=/' "$R/dnsmasq.conf" \
    || fail "dnsmasq.conf に server=/.../ がある (suffix 一致で DNS exfil 経路 #8)"
probe=$(mktemp); printf 'server=/api.anthropic.com/127.0.0.11\n' > "$probe"
grep -Eq '^[[:space:]]*server=/' "$probe" || { rm -f "$probe"; fail "negative: server= 検出器が転送行を見逃す"; }
rm -f "$probe"

# --- 3. __APP__ 展開の一貫性 -------------------------------------------------
for t in interview.md settings.json; do
    if grep -q '__APP__' "$R/$t"; then
        grep -Eq "s\|__APP__\|.*$t" "$R/entrypoint.sh" \
            || fail "entrypoint が $t の __APP__ を展開していない (#3)"
    fi
done
grep -Eq -- '--settings[[:space:]]+"\$RENDER/settings.json"' "$R/entrypoint.sh" \
    || fail 'claude が展開済み settings ($RENDER) を渡していない (#3)'

# --- 4. settings.json の writable mount は // 絶対パス -----------------------
for tool in Write Edit; do
    grep -q "$tool(//workspace/extract/__APP__/\*\*)" "$R/settings.json" \
        || fail "settings.json: $tool が //workspace 絶対で writable mount を許可していない (#5)"
done
probe=$(mktemp); printf '"Write(/workspace/extract/__APP__/**)"\n' > "$probe"
if grep -q "Write(//workspace/extract/__APP__/\*\*)" "$probe"; then
    rm -f "$probe"; fail "negative: // 絶対の pin が単一 / 絶対を誤許容"
fi
rm -f "$probe"

# --- 5. NO_PROXY が proxy 経由ホストを suffix 一致でバイパスしない --------------
# NO_PROXY エントリは末尾一致でサブドメインも捕まえる (curl/Go 共通)。rules.yaml で MITM + substitute
# するホストを compose の NO_PROXY が suffix 一致すると proxy をバイパスし substitute/leak 検知が効かない。
DC=core
noproxy=$(sed -n 's/^x-no-proxy:.*"\(.*\)".*/\1/p' "$DC/compose.yaml")
[ -n "$noproxy" ] || fail "compose.yaml の x-no-proxy アンカー値が読めない (#5)"
# project 層の PROJECT_NO_PROXY (project/.env) も同じ suffix 一致検査にかける
proj_noproxy=$(sed -n 's/^PROJECT_NO_PROXY=//p' "$P/.env")
noproxy="$noproxy,$proj_noproxy"
check_no_suffix() {  # $1 = proxy 経由にすべきホスト。それを suffix 一致する NO_PROXY エントリがあれば fail
    local target="$1" e
    local IFS=,
    for e in $noproxy; do
        e="${e//[[:space:]]/}"; e="${e#.}"   # 先頭ドット (.github.com も suffix 一致) を正規化
        [ -n "$e" ] || continue
        if [ "$target" = "$e" ] || [[ "$target" == *".$e" ]]; then
            fail "NO_PROXY '$e' が proxy 経由ホスト '$target' を suffix 一致でバイパスする (substitute 無効化) (#5)"
        fi
    done
}
for gh_host in api.github.com uploads.github.com; do
    # rules.yaml が当該ホストを proxy 経由 (hosts: キー or allow_hosts エントリ) にしている時だけ要求する。
    # bind 配列やコメント内の出現で誤発火しないよう、行頭の list/key 形のみに一致させる。
    esc="${gh_host//./\\.}"
    if grep -qE "^[[:space:]]*(-[[:space:]]+)?\"$esc\"[[:space:]]*:?[[:space:]]*(#.*)?$" "$P/rules.yaml"; then
        check_no_suffix "$gh_host"
    fi
done
# negative probe: 検出述語が github.com → api.github.com の suffix 一致を実際に捕まえる
[[ "api.github.com" == *".github.com" ]] || fail "negative: #5 の suffix 一致述語が github.com→api.github.com を見逃す"
# negative probe: 先頭ドット (.github.com) も dot 正規化後に捕まえる
{ e=".github.com"; e="${e#.}"; [[ "api.github.com" == *".$e" ]]; } || fail "negative: #5 が先頭ドット NO_PROXY エントリを取りこぼす"
# negative probe: PROJECT_NO_PROXY 側に置かれた github.com も検査対象に入る (env 読取の回帰 pin)
probe=$(mktemp); printf 'PROJECT_NO_PROXY=github.com\n' > "$probe"
[ "$(sed -n 's/^PROJECT_NO_PROXY=//p' "$probe")" = "github.com" ] \
    || { rm -f "$probe"; fail "negative: .env の PROJECT_NO_PROXY 読取が壊れている"; }
rm -f "$probe"

# --- 6. secrets-proxy: 既定 mitmdump / web は loopback bind の mitmweb を起動 ------
# entrypoint は SECRETS_PROXY_WEB ゲートで既定 mitmdump / opt-in mitmweb を切替える。
# mitmweb のフロー画面には注入済み平文 cred が映る。dev/LAN に晒さない防御は 2 段:
#   (a) 経路: Web UI を container loopback (127.0.0.1) に bind する。dev は別コンテナなので
#       route_localnet=0 の下で他コンテナの loopback へ到達できない (martian drop。weak host model
#       でも迂回不可)。publish も張らない (section 7)。host は docker exec 踏み台経由でだけ届く。
#   (b) token: 常時 ON の web_password token (dev は取得不能) が defense-in-depth。
# (docs/verified-facts/mitmproxy.md / SECURITY-MODEL 不変条件 1/4)
EP=core/secrets-proxy/entrypoint.sh
grep -Eq 'SECRETS_PROXY_WEB' "$EP" || fail "entrypoint に SECRETS_PROXY_WEB ゲートが無い"
grep -Eq '^[[:space:]]*exec mitmdump' "$EP" || fail "entrypoint の既定分岐が mitmdump を exec しない"
grep -Eq '^[[:space:]]*exec mitmweb'  "$EP" || fail "entrypoint に mitmweb 分岐が無い"
# 経路防御の要: mitmweb の web_host 既定は container loopback (127.0.0.1)。0.0.0.0/実 IP/:: に化けると
# dev/LAN から到達可能になるので pin する (これが崩れると token 一本に戻る)。
grep -Eq 'SECRETS_PROXY_WEB_HOST:-127\.0\.0\.1' "$EP" \
    || fail "mitmweb の web_host 既定が 127.0.0.1 (loopback) でない (dev から到達可能になる)"
! grep -Eq 'web_host=("?)(0\.0\.0\.0|::)' "$EP" \
    || fail "mitmweb を 0.0.0.0/:: に bind している (dev/LAN へ露出)"
# negative probe: loopback-default 検出器が非 loopback 既定を確かに弾く
probe=$(mktemp); printf 'web_host="${SECRETS_PROXY_WEB_HOST:-0.0.0.0}"\n' > "$probe"
grep -Eq 'SECRETS_PROXY_WEB_HOST:-127\.0\.0\.1' "$probe" \
    && { rm -f "$probe"; fail "negative: loopback-default 検出器が 0.0.0.0 既定を誤許容"; }
rm -f "$probe"
# flow_detail は mitmdump(dumper) 専用。mitmweb では未登録なので defer され黙って無視される (落ちはしない)
# が、mitmdump 専用オプションを混ぜず invocation を正直に保つため mitmweb 行には置かない (mitmdump 行だけ)。
# exec は複数行に \ 継続する (mitmweb 分岐は現に3行の継続を持つ) ので、line-anchor な grep のままだと
# 「継続行として flow_detail が足された」回帰を見逃す。バックスラッシュ継続を論理行に畳んでから判定する。
EP_JOINED=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba}' "$EP")
grep -Eq '^[[:space:]]*exec mitmdump.*flow_detail' <<<"$EP_JOINED" \
    || fail "flow_detail が mitmdump 分岐に無い"
! grep -Eq '^[[:space:]]*exec mitmweb.*flow_detail' <<<"$EP_JOINED" \
    || fail "flow_detail を mitmweb に渡している (mitmweb では無意味な mitmdump 専用オプション; 継続行も含めて判定)"
# negative probe: 継続行として紛れ込んだ flow_detail (mitmweb の既存3継続行と同じ書き方) も畳んだ後に捕まえる
probe=$(mktemp)
printf 'exec mitmweb "${COMMON_OPTS[@]}" \\\n  --set web_open_browser=false \\\n  --set flow_detail=0 \\\n  --set web_port="${web_port}"\n' > "$probe"
probe_joined=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba}' "$probe")
grep -Eq '^[[:space:]]*exec mitmweb.*flow_detail' <<<"$probe_joined" \
    || { rm -f "$probe"; fail "negative: flow_detail 検出器が継続行に混ぜたケースを見逃す"; }
rm -f "$probe"
# web_open_browser=false だと mitmweb は token をログに出さない (v11.1.3 webaddons.py: web_url を出すのは
# browser 起動失敗パスのみ。docs/verified-facts/mitmproxy.md)。entrypoint が token を生成し web_password に
# 固定して URL を echo することで token を surface する。これが外れると「token 取得不能」に逆戻りするので pin。
grep -Eq '^[[:space:]]*exec mitmweb.*web_password' <<<"$EP_JOINED" \
    || fail "mitmweb 分岐が web_password を設定していない (token がログに出ず取得不能になる)"
grep -Eq 'token=\$\{?web_token' "$EP" \
    || fail "entrypoint が mitmweb の token URL (?token=<生成 token>) を echo していない"
# negative probe: web_password 未設定の mitmweb exec を検出器が確かに捕まえる
probe=$(mktemp)
printf 'exec mitmweb "${COMMON_OPTS[@]}" \\\n  --set web_host="${SECRETS_PROXY_WEB_HOST}"\n' > "$probe"
probe_joined=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba}' "$probe")
grep -Eq '^[[:space:]]*exec mitmweb.*web_password' <<<"$probe_joined" \
    && { rm -f "$probe"; fail "negative: web_password 検出器が未設定を見逃す"; }
rm -f "$probe"

# --- 7. secrets-proxy web override: publish 無し (loopback bind + exec トンネル) / 既定漏れ無し ---
# mitmweb は loopback bind なので host publish は張らない。publish を張ると (実 IP publish + 0.0.0.0
# bind に化けた場合に) 露出しうるので、override が 8081 を publish しないこと・web_host を上書きしない
# ことを pin する。host アクセスは proxy-web-tunnel.py の docker exec 踏み台で行う。
DCW=core/compose.mitmweb.yaml
CY=core/compose.yaml   # 既定 compose.yaml ファイル ($DC は #5 で core ディレクトリを指すので別名)
[ -f "$DCW" ] || fail "compose.mitmweb.yaml が無い"
grep -Eq 'SECRETS_PROXY_WEB:[[:space:]]*"?1' "$DCW" || fail "override が SECRETS_PROXY_WEB=1 を与えていない"
! grep -Eq '8081:8081' "$DCW" || fail "override が 8081 を publish している (loopback bind 方針では publish しない)"
! grep -Eq 'SECRETS_PROXY_WEB_HOST' "$DCW" || fail "override が web_host を上書きしている (loopback 既定を崩す恐れ)"
# 既定 compose.yaml に Web UI 設定を漏らさない
! grep -Eq '8081|mitmweb|SECRETS_PROXY_WEB|web_host' "$CY" \
    || fail "既定 compose.yaml に Web UI 設定が漏れている"
# negative probe: 8081 publish 検出器が publish 行を確かに捕まえる
probe=$(mktemp); printf '      - "127.0.0.1:8081:8081"\n' > "$probe"
grep -Eq '8081:8081' "$probe" || { rm -f "$probe"; fail "negative: 8081 publish 検出器が見逃す"; }
rm -f "$probe"

# --- 8. devcontainer.json: project 層の必須配線 --------------------------------
# devcontainer.json は project 所有ファイルになったため、core が要求する配線 (compose の
# [project, core] 順 = .env と後勝ち保証 / settings.local.json の mask / postStart の firewall+CA)
# が編集で消える drift をここで pin する。
DJ=devcontainer.json
grep -Eq '"dockerComposeFile":[[:space:]]*\[[[:space:]]*"project/compose.yaml",[[:space:]]*"core/compose.yaml"[[:space:]]*\]' "$DJ" \
    || fail 'devcontainer.json の dockerComposeFile が ["project/compose.yaml", "core/compose.yaml"] でない'
grep -q 'settings.local.json' "$DJ" || fail "devcontainer.json の initializeCommand が settings.local.json を用意しない (local-mask が空振りする)"
grep -q 'init-firewall.sh && sudo /usr/local/bin/install-proxy-ca.sh' "$DJ" \
    || fail "devcontainer.json の postStartCommand が firewall+CA を再実行しない"
# negative probe: 逆順 [core, project] を順序検出器が弾く
probe=$(mktemp); printf '"dockerComposeFile": ["core/compose.yaml", "project/compose.yaml"]\n' > "$probe"
grep -Eq '"dockerComposeFile":[[:space:]]*\[[[:space:]]*"project/compose.yaml",[[:space:]]*"core/compose.yaml"[[:space:]]*\]' "$probe" \
    && { rm -f "$probe"; fail "negative: compose 順序検出器が逆順を誤許容"; }
rm -f "$probe"

# --- 9. project allow-domains の配線: COPY -> firewall 読込 ----------------------
# project/allow-domains.txt は「Dockerfile が COPY し init-firewall.sh が読む」の 2 点が揃って
# 初めて効く。どちらかが外れると黙って許可されなくなる (fail-silent) ので両方 pin する。
# $DC は #5 で束ねた core ディレクトリ (core) をそのまま使う (#7 の compose.yaml
# ファイルパスは別名 $CY にしたので $DC は core ディレクトリを指したまま)。
grep -q 'COPY project/allow-domains.txt /etc/agent-devcontainer/allow-domains.txt' "$DC/Dockerfile" \
    || fail "core/Dockerfile が project/allow-domains.txt を COPY していない"
grep -q '/etc/agent-devcontainer/allow-domains.txt' "$DC/init-firewall.sh" \
    || fail "init-firewall.sh が project allow-domains を読んでいない"
[ -f "$P/allow-domains.txt" ] || fail "project/allow-domains.txt が無い"

# --- 10. マージ済み compose のセキュリティ姿勢 (config を直接検査) ----------------
# 複数 -f の「後勝ちで core 優先」が守るのは core が明示宣言したスカラーだけ。list (cap_add/networks/
# volumes) は追記マージ、core 未宣言キーは project 値が通る (docs/verified-facts/docker.md「compose 複数
# -f」)。よって project 層の編集で崩れうる「マージ結果そのもの」の姿勢を、後勝ちの一般則でなく
# config 出力を直接検査して pin する。検査項目と negative probe (違反注入を検出器が弾く --selftest) は
# bin/check_compose_posture.py。compose は #1〜#9 と違いこの段でだけ要る (無ければ自然エラー)。
if command -v docker >/dev/null 2>&1; then COMPOSE=(docker compose); else COMPOSE=(podman compose); fi
POSTURE=core/bin/check_compose_posture.py
# PyYAML は kit venv から供給する (check_compose_paths.py と同じ一元化。system python3 に yaml が
# 入っている保証は無い — Dockerfile は apt python3-yaml を持たない)。cd 済みの repo root 相対。
PY=.venv/bin/python
# 期待する project 名は project/compose.yaml 自身の name: から読む (kit 化により @@PROJECT_NAME@@ 置換後の
# 値は consumer ごとに違うのでリテラル固定できない。ここで読んだ値と、マージ済み compose config が
# 実際に持つ name を突き合わせるのが check_compose_posture.py の役目)。導出は redact-flow が使う
# 実経路 (bin/compose-project-name) を通す — 独立 parser (PyYAML) との parity は check-redact-config
# が別途 pin しており、ここで別実装を重複させない。
EXPECTED_NAME=$(bash "$DC/bin/compose-project-name" "$P/compose.yaml") \
    || fail "§10: project/compose.yaml から project 名を導出できない"
merged=$(mktemp); mergederr=$(mktemp)
# stderr は握りつぶさない: 失敗原因はほぼ常に stderr にある (例: project/.env の必須変数
# (`${VAR:?}` 宣言のもの) の不足による interpolation エラー)。隠すと「compose 未導入/不正」への誤診断を誘う。
# env -u: ambient な COMPOSE_PROJECT_NAME は file の name: を黙って上書きし (docker.md「project 名の
# 優先順位」) EXPECTED_NAME との突合が偽赤になる。COMPOSE_ENV_FILES は補間に使う .env を余所へ
# 差し替える (同「補間に使う .env の差替え」)。
env -u COMPOSE_PROJECT_NAME -u COMPOSE_ENV_FILES "${COMPOSE[@]}" -f "$P/compose.yaml" -f "$DC/compose.yaml" config > "$merged" 2>"$mergederr" \
    || { cat "$mergederr" >&2; rm -f "$merged" "$mergederr"; fail "§10: compose config が失敗 (原因は直上の stderr)"; }
rm -f "$mergederr"
"$PY" "$POSTURE" "$EXPECTED_NAME" < "$merged" \
    || { rm -f "$merged"; fail "§10: マージ済み compose のセキュリティ姿勢に違反 (上の NG 行を参照)"; }
"$PY" "$POSTURE" "$EXPECTED_NAME" --selftest < "$merged" \
    || { rm -f "$merged"; fail "§10: negative probe が違反注入を検出できない (上の NG 行を参照)"; }
rm -f "$merged"

# --- 11. 複数ファイル literal 結合の配線 pin (§9 と同じ fail-silent 防止クラス) -----
# (a) PROJECT_GH_USER は sudoers env_keep (Dockerfile) / compose (build.args と environment の 2 箇所) /
#     init-firewall.sh の reader が同名で揃って初めて postStart の整合検査が機能する。片側だけの
#     改名・削除は CI 緑のまま全 consumer の起動が「PROJECT_GH_USER が sudo 環境に無い」で止まる。
#     compose は 2 箇所を数で pin (片方だけの削除を素通ししない)、reader はコメントでなく実際の
#     展開形 (${PROJECT_GH_USER) で pin する。sudoers の束縛先パスが COPY 先と一致することも pin
#     (Defaults! はパス完全一致)。
grep -qF 'env_keep += "PROJECT_GH_USER"' "$DC/Dockerfile" \
    || fail "§11: core/Dockerfile の sudoers に PROJECT_GH_USER の env_keep が無い"
grep -qF 'Defaults!/usr/local/bin/init-firewall.sh' "$DC/Dockerfile" \
    || fail "§11: sudoers の Defaults! が init-firewall.sh の COPY 先パスを指していない"
grep -q 'COPY core/init-firewall.sh /usr/local/bin/' "$DC/Dockerfile" \
    || fail "§11: core/Dockerfile が init-firewall.sh を /usr/local/bin へ COPY していない"
[ "$(grep -cE '^ +PROJECT_GH_USER:' "$DC/compose.yaml")" -eq 2 ] \
    || fail "§11: core/compose.yaml の PROJECT_GH_USER が 2 箇所 (build.args + environment) に無い"
grep -qF '${PROJECT_GH_USER' "$DC/init-firewall.sh" \
    || fail "§11: init-firewall.sh が PROJECT_GH_USER を展開形で読んでいない (コメントだけでは不可)"
# (a') gh seed のパス literal も同じ結合: Dockerfile (COPY 先 + sed/assert 対象) と init-firewall.sh
#      (awk reader) の両方に /home/node/.config/gh/hosts.yml が現れて初めて検査が成立する。
grep -qF '/home/node/.config/gh/hosts.yml' "$DC/Dockerfile" \
    || fail "§11: core/Dockerfile に gh seed パス /home/node/.config/gh/hosts.yml が無い"
grep -qF '/home/node/.config/gh/hosts.yml' "$DC/init-firewall.sh" \
    || fail "§11: init-firewall.sh が gh seed パス /home/node/.config/gh/hosts.yml を読んでいない"
# (a'') gh auth login の実トークン焼込を封じる唯一のバリアは gh dir/hosts.yml の root 所有 (旧 ro bind は
#       撤去済み)。imperative な RUN 1 行なので、これが落ちても他の検査は緑のまま = silent 退行しうる。
#       source を pin する (chown -R root:root で dir、hosts.yml は 644 で node 非所有のまま)。
grep -qF 'chown -R root:root /home/node/.config/gh' "$DC/Dockerfile" \
    || fail "§11: Dockerfile が gh dir を root 所有にしていない (gh auth login のトークン焼込バリアが失効)"
grep -qF 'chmod 644 /home/node/.config/gh/hosts.yml' "$DC/Dockerfile" \
    || fail "§11: Dockerfile が hosts.yml を 644 にしていない (node 書込可だとトークン焼込を封じられない)"
# (a''') init-firewall.sh の user 抽出 parser は共有 awk (gh-hosts-user.awk) に切り出し、Dockerfile が
#        image へ COPY する。実挙動は check-gh-seed の fixture が exercise する (parser↔seed 形式の結合)。
grep -qF 'COPY core/gh-hosts-user.awk /usr/local/bin/' "$DC/Dockerfile" \
    || fail "§11: Dockerfile が gh-hosts-user.awk を COPY していない"
grep -qF 'gh-hosts-user.awk' "$DC/init-firewall.sh" \
    || fail "§11: init-firewall.sh が gh-hosts-user.awk を参照していない"
# (b) redact の共有 auth volume は external (compose は作らない) — redact-flow が事前作成する名前と
#     compose.yaml の name: が食い違うと、事前作成が空振りして run 時に external volume not found。
#     redact-flow 側は AUTH_VOL 変数に集約済み (inspect/create/案内が参照) なので代入値を突き合わせる。
#     charset は compose の volume 名に合わせ '_' も含める (欠くと正しい underscore 改名が偽赤になる)。
# head へ pipe しない: 2 件目以降のマッチで sed が SIGPIPE (pipefail で 141) → fail() を出さずに即死する。
# sed は全行走査し (小さいファイル)、bash が最初の行だけ取り出す。
vol_flow=$(sed -n 's/^AUTH_VOL=\([A-Za-z0-9_-]*\).*/\1/p' "$DC/bin/redact-flow"); vol_flow=${vol_flow%%$'\n'*}
# volume の name: は字下げされている (volumes: 配下) — 先頭空白必須で top-level name: (0 字下げ、
# ${...} 補間) と区別する。top-level を literal に戻す将来変更でも volume 名だけを拾う。
vol_compose=$(sed -n 's/^  *name: \([A-Za-z0-9_-]*\).*/\1/p' "$DC/redact/compose.yaml"); vol_compose=${vol_compose%%$'\n'*}
[ -n "$vol_flow" ] || fail "§11: redact-flow に AUTH_VOL (auth volume 名) の定義が無い"
[ "$vol_flow" = "$vol_compose" ] \
    || fail "§11: auth volume 名が不一致 (redact-flow '$vol_flow' vs redact/compose.yaml '$vol_compose')"

# --- 12. ambient compose 変数の隔離が全 compose 入口で揃っているか ---------------------
# COMPOSE_PROJECT_NAME / COMPOSE_ENV_FILES は file 宣言を黙って上書き/差替えする (docker.md「project
# 名の優先順位」「補間に使う .env の差替え」)。compose を呼ぶ入口はどれも両方を無害化する必要があり、
# 1 つでも抜けると稼働中 stack と別 namespace/別 .env を操作する (この PR で隔離対象が 1→2 変数に
# 増えた際、全入口を手で直す必要があった)。新しい ambient 変数はこの iso_vars に足す = 全入口の
# 欠落が loud に出る (異なる runtime = make/bash/python に跨るため DRY でなく pin で束ねる)。
# iso_sites は明示列挙: compose 呼び出しは make の $(COMPOSE) / redact-flow の quote 付き引数 /
# checker の config parse 等で形が揃わず、自動 discovery は取りこぼしと誤検出を両方生む (実測)。
# よって新しく compose を呼ぶ入口を足すときは env -u で隔離した上でここに登録する — この登録が唯一の
# 発見面なので、iso_sites への追加漏れは code review で捕まえる (検査は登録済み入口の変数欠落を守る)。
iso_vars=(COMPOSE_PROJECT_NAME COMPOSE_ENV_FILES)
iso_sites=("$DC/Makefile" "$DC/bin/redact-flow" "$DC/bin/redact_invariants_check.sh" "$DC/proxy-web-tunnel.py")
for v in "${iso_vars[@]}"; do
    for s in "${iso_sites[@]}"; do
        grep -Eq -- "-u $v|pop\(['\"]$v" "$s" \
            || fail "§12: $s が ambient 変数 $v を無害化していない (env -u / os.environ.pop)"
    done
done

echo "ok  redact-invariants (redact-flow 名検証 / dnsmasq 無転送 / __APP__ 展開 / settings 絶対パス / NO_PROXY suffix / secrets-proxy web / devcontainer.json 配線 / project allow-domains 配線 / compose 姿勢 §10 / literal 配線 §11 / ambient 隔離 §12)"
