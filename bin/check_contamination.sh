#!/usr/bin/env bash
# core/ と templates/ への consumer 固有値の再混入検査 (kit issue #10 の再発防止)。
#   usage: bin/check_contamination.sh <dir>...   (root Makefile の check-contamination が呼ぶ)
#
# 禁止 token は `ikeyan` (kit 自身の canonical 参照だけ allowlist) と旧 project 名の残滓
# `tools-redact` / `tools_`。`ikeyan.github.io` / `ikeyan/tools` は `ikeyan` 規則が包含する。
# allowlist は行単位でなく token 単位 (sed で無害化してから再 grep) — 同一行に allowlist 対象と
# 違反が同居しても違反が残って検出される。
#   - repo slug は左右とも境界を固定する: 右は直後の `/` まで含め (接頭辞拡張
#     ikeyan/agent-devcontainer-x = 別 repo は `ikeyan` が残って検出)、左は直前が slug 文字
#     [A-Za-z0-9-] でないこと (user 名拡張 myikeyan/... も検出)。裸 slug (末尾 `/` 無し) も
#     allowlist 外 = 検出される。scan の入力は常に grep -rn の "path:行:内容" 形なので、
#     内容先頭の token にも必ず直前文字 (`:`) がある。
#   - `tools-redact`/`tools_` は直前が英数の場合のみ無害化: 別語の部分一致 (例: wheel 正規化名の
#     setuptools_scm) を誤検出しない。残滓の実形 (tools_proxy-bw 等) は行頭/記号の後に現れ検出が残る。
# core/docs/verified-facts/ は実測記録の歴史的台帳なので対象外。
# fail-closed: 走査エラー (grep exit>1 = 読めないファイル等 / find 非 0 / sanitize sed の失敗 /
# 対象 dir へ cd 不可) を「クリーン」へ反転させず PIPESTATUS・サブシェル exit 2 で明示 fail し (-a で binary も本文走査)、
# ファイル/ディレクトリ名への混入も同じ流儀 (probe 付き) で走査する。
# negative probe: FORBIDDEN_RE の各 token (regex の | 区切りから導出 — token 追加に fixture が
# 自動追従。token は literal 前提) + allowlist 同居行 + slug の左右接頭辞拡張 + 大小文字変種 +
# ファイル名 fixture を検出でき、かつ純 allowlist/別語 fixture を誤検出しないことを確認してから
# 本走査する。probe も本走査も対象ディレクトリへ cd して相対パスで走査する — mktemp (TMPDIR) や
# checkout の絶対パスに禁止 token が含まれる環境でもパス由来の誤検出をしない。
# 大小文字は無視する (GitHub user 名も DNS ホスト名も case-insensitive — Ikeyan 等の変種も混入):
# grep は -i、allowlist sed は GNU の I flag (この検査は kit repo 専用 = GNU sed 前提)。
set -uo pipefail # -e は使わない: grep の exit 1 (= no match) を制御フローに使う

FORBIDDEN_RE='ikeyan|tools-redact|tools_'

sanitize() {
    sed -e 's#\([^A-Za-z0-9-]\)ikeyan/agent-devcontainer/#\1ALLOWED/#gI' \
        -e 's#\([^A-Za-z0-9-]\)ikeyan/agent-files/#\1ALLOWED/#gI' \
        -e 's#\([A-Za-z0-9]\)tools-redact#\1ALLOWED#gI' \
        -e 's#\([A-Za-z0-9]\)tools_#\1ALLOWED_#gI'
}

# 本文走査。exit: 0 = 残余あり (stdout に hits) / 1 = クリーン / 2 = 走査エラー。cwd は走査 root。
scan() {
    local raw st ps
    raw=$(grep -rnaiE "$FORBIDDEN_RE" "$@")
    st=$?
    [ $st -le 1 ] || return 2
    # ledger (docs/verified-facts) の hit だけをパス限定で落とす (実測記録の歴史台帳で consumer 値を
    # 含みうる)。grep --exclude-dir は basename glob なので `verified-facts` という名の他 dir まで
    # 除外してしまい bypass を生む — grep 出力の行頭パスを `^\./docs/verified-facts/` で狙って落とす。
    printf '%s\n' "$raw" | grep -v '^\./docs/verified-facts/' | sanitize | grep -iE "$FORBIDDEN_RE"
    ps=("${PIPESTATUS[@]}")   # [printf, grep -v, sed, grep]
    [ "${ps[2]}" -eq 0 ] || return 2   # sanitize sed の失敗だけ走査エラー扱い (grep -v の exit 1 は正常)
    return "${ps[3]}"
}

# ファイル/ディレクトリ名走査。exit 規約は scan と同じ (allowlist は名前には適用しない)。
# ledger は exact path で prune する (verified-facts 名の他 dir は prune しない)。cwd は走査 root。
name_scan() {
    local all ps
    all=$(find . -path ./docs/verified-facts -prune -o -print) || return 2
    printf '%s\n' "$all" | grep -iE "$FORBIDDEN_RE"
    ps=("${PIPESTATUS[@]}")
    return "${ps[1]}"
}

[ $# -ge 1 ] || { echo "usage: check_contamination.sh <dir>..." >&2; exit 2; }
for d in "$@"; do
    [ -d "$d" ] || { echo "走査対象が無い: $d" >&2; exit 1; }
done

# symlink 拒否: core/templates に正当な symlink は無い。grep -r は target を追わず、installer の
# cp -Rp は link を保存するので、中立な名前で target に consumer 値を持つ link が混入検査を迂回する
# (実測)。target を追うと走査 root 外へ逃げうるので、link 自体を拒否する (fail-closed)。
syms=$(find "$@" -type l) || { echo "symlink 走査が find エラーで不完全" >&2; exit 1; }
[ -z "$syms" ] || { echo "core/templates に symlink があります (混入検査を迂回しうる — 実ファイルにしてください):" >&2; printf '%s\n' "$syms" >&2; exit 1; }

IFS='|' read -ra TOKS <<< "$FORBIDDEN_RE"

# 全 probe dir をスコープ終端で解放する (獲得と解放を対に。REVIEW.md 簡素化)。
probe='' clean='' nprobe='' lprobe='' sprobe=''
trap 'rm -rf "$probe" "$clean" "$nprobe" "$lprobe" "$sprobe"' EXIT

# cd 失敗を scan の「クリーン」(return 1) と混同しない: サブシェルを exit 2 (走査エラー) で抜ける。
scan_in()      { ( cd "$1" || exit 2; scan . ); }
name_scan_in() { ( cd "$1" || exit 2; name_scan . ); }

# --- negative probe: 検出すべき fixture を実際に弾くか --------------------------------
probe=$(mktemp -d)
{
    for tok in "${TOKS[@]}"; do echo "plain $tok"; done
    echo "coexist github.com/ikeyan/agent-devcontainer/blob と ikeyan.github.io の同居"
    echo "prefix1 ikeyan/agent-devcontainer-x"
    echo "prefix2 ikeyan/agent-files-x"
    echo "prefix3 myikeyan/agent-devcontainer/x"
    echo "case IkEyAn variant"
} > "$probe/f"
hits=$(scan_in "$probe")
st=$?
[ $st -eq 0 ] || { echo "negative: 検出 probe が走査エラー/空振り (st=$st)" >&2; exit 1; }
for tok in "${TOKS[@]}" ikeyan.github.io agent-devcontainer-x agent-files-x myikeyan IkEyAn; do
    echo "$hits" | grep -qF "$tok" \
        || { echo "negative: 混入検出器が fixture の $tok を素通し" >&2; exit 1; }
done

# --- negative probe: 純 allowlist/別語 fixture を誤検出しないか ------------------------
clean=$(mktemp -d)
printf '%s\n%s\n' \
    'https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh' \
    'setuptools_scm-8.whl と mytools-redact-probe' > "$clean/f"
scan_in "$clean" >/dev/null
st=$?
[ $st -eq 1 ] || { echo "negative: 純 allowlist/別語 fixture を誤検出か走査エラー (st=$st)" >&2; exit 1; }

# --- negative probe: ファイル名検出器 ---------------------------------------------------
nprobe=$(mktemp -d)
mkdir "$nprobe/ikeyan-dir"
: > "$nprobe/tools_probe"
nhits=$(name_scan_in "$nprobe")
st=$?
[ $st -eq 0 ] || { echo "negative: ファイル名 probe が走査エラー/空振り (st=$st)" >&2; exit 1; }
{ echo "$nhits" | grep -q 'ikeyan-dir' && echo "$nhits" | grep -q 'tools_probe'; } \
    || { echo "negative: ファイル名検出器が fixture を素通し" >&2; exit 1; }

# --- probe: ledger (docs/verified-facts) だけを除外し、verified-facts 名の他 dir は検出する ---
lprobe=$(mktemp -d)
mkdir -p "$lprobe/docs/verified-facts" "$lprobe/verified-facts"
echo "ikeyan-in-ledger" > "$lprobe/docs/verified-facts/f"   # 除外される (歴史台帳)
echo "ikeyan-nonledger" > "$lprobe/verified-facts/f"        # 検出される (別 dir)
lhits=$(scan_in "$lprobe")
echo "$lhits" | grep -qF 'ikeyan-nonledger' \
    || { echo "negative: verified-facts 名の非 ledger dir を誤って除外している" >&2; exit 1; }
echo "$lhits" | grep -qF 'ikeyan-in-ledger' \
    && { echo "negative: ledger (docs/verified-facts) を除外できていない" >&2; exit 1; }

# --- probe: symlink 検出 (find -type l) が link を拾えるか (拒否の前提) ---
sprobe=$(mktemp -d)
echo "ikeyan.github.io" > "$sprobe/target"; ln -s target "$sprobe/link"
[ -n "$(find "$sprobe" -type l)" ] \
    || { echo "negative: symlink 検出 (find -type l) が link を拾えない" >&2; exit 1; }

# --- 本走査 (対象ごとに cd して相対走査 — checkout 自体の絶対パスに token を含む環境でも偽赤にしない) ---
for d in "$@"; do
    hits=$(scan_in "$d")
    st=$?
    [ $st -ne 2 ] || { echo "混入走査が走査エラーで不完全 (読めないファイル/sed 失敗/進入不可?): $d" >&2; exit 1; }
    [ $st -eq 1 ] || {
        echo "consumer 固有値が $d に混入 (project 層へ移すか allowlist を見直す):" >&2
        echo "$hits" | sed "s#^\./#$d/#" >&2
        exit 1
    }
    names=$(name_scan_in "$d")
    st=$?
    [ $st -ne 2 ] || { echo "ファイル名走査が走査エラーで不完全: $d" >&2; exit 1; }
    [ $st -eq 1 ] || {
        echo "consumer 固有値がファイル/ディレクトリ名に混入 ($d):" >&2
        echo "$names" | sed "s#^\./#$d/#" >&2
        exit 1
    }
done
echo "ok  contamination ($* に consumer 固有値なし)"
