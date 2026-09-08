#!/usr/bin/env bash
# bin/workspace-repos の受理/拒否と sync の挙動を temp scaffold + ローカル bare repo で検査する
# (make check-repos)。ネット不要。<ws>/.devcontainer/core/bin/{workspace-root,workspace-repos} を実際に置き、
# workspace root の導出 (consumer = .devcontainer の親 / kit checkout = その checkout 自身) ごと exercise する。
# git はユーザー/システム設定と環境注入 (GIT_CONFIG_*) から隔離して走らせる (insteadOf / gpgsign 等で fixture が
# 揺れないように)。
set -euo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_COUNT=0 GIT_TERMINAL_PROMPT=0
export HOME="$T/home"; mkdir -p "$HOME"
git() { command git -c user.email=probe@example.invalid -c user.name=probe -c init.defaultBranch=main "$@"; }

scaffold() { mkdir -p "$1/core/bin" "$1/project"; cp -p "$SELF_DIR/workspace-root" "$SELF_DIR/workspace-repos" "$1/core/bin/"; }
WS="$T/ws"
scaffold "$WS/.devcontainer"
R="$WS/.devcontainer/project/repos.txt"
run() { bash "$WS/.devcontainer/core/bin/workspace-repos" "$1" >"$T/out" 2>&1; }
expect_ok() { # $1=mode $2=説明
    run "$1" || { echo "workspace-repos $1 が失敗: $2" >&2; cat "$T/out" >&2; exit 1; }
}
expect_fail() { # $1=mode $2=説明 $3=期待するエラー文言 (別理由の失敗を素通しにしない)
    if run "$1"; then echo "negative: workspace-repos $1 が $2 を素通し" >&2; cat "$T/out" >&2; exit 1; fi
    grep -qF -- "$3" "$T/out" || { echo "negative: $2 の拒否理由が期待と違う (期待: $3):" >&2; cat "$T/out" >&2; exit 1; }
}

# --- 受理する構文 (list で dir/url の導出値を pin) ------------------------------------------
cat > "$R" <<'REPOS'
# コメント行

example-org/example-repo                 # slug → dir は <repo>
example-org/example-repo.git dotgit      # 明示 dir
https://example.invalid/x/y.git ydir
git@example.invalid:x/z.git zdir
ssh://git@example.invalid:2222/x/w.git wdir
file:///nowhere/v.git v.dir
REPOS
expect_ok list "受理すべき構文"
diff -u - "$T/out" <<'EXPECT' || { echo "list の導出値が期待と違う (上の diff)" >&2; exit 1; }
example-repo	https://github.com/example-org/example-repo.git
dotgit	https://github.com/example-org/example-repo.git
ydir	https://example.invalid/x/y.git
zdir	git@example.invalid:x/z.git
wdir	ssh://git@example.invalid:2222/x/w.git
v.dir	file:///nowhere/v.git
EXPECT
expect_ok check "未 clone の宣言 (在るものだけ整合を見る)"
grep -qF "ok  repos (6 件宣言 / workspace: $WS)" "$T/out" \
    || { echo "check の要約行が無い / 件数か workspace root (consumer = .devcontainer の親) が違う:" >&2; cat "$T/out" >&2; exit 1; }
# kit checkout (core/ と並んで templates/project + install.sh がある root layout) は自身が workspace root —
# 親 dir を workspace と誤認しない (redact サンドボックスの ro mount 範囲と extract/ の置き場に直結)
KIT="$T/kit"; scaffold "$KIT"; mkdir -p "$KIT/templates/project"; : > "$KIT/install.sh"
printf 'example-org/example-repo\n' > "$KIT/project/repos.txt"
bash "$KIT/core/bin/workspace-repos" check >"$T/out" 2>&1 || { echo "kit layout の check が失敗:" >&2; cat "$T/out" >&2; exit 1; }
grep -qF "workspace: $KIT)" "$T/out" || { echo "kit layout の workspace root が checkout 自身でない:" >&2; cat "$T/out" >&2; exit 1; }

# --- 拒否する構文 (1 行でも不正なら exit 1) ----------------------------------------------------
reject() { printf '%s\n' "$1" > "$R"; expect_fail check "不正行 '$1'" "$2"; }
reject 'a/b/c'                          '<owner>/<repo> でも git URL でもない'
reject 'plain'                          '<owner>/<repo> でも git URL でもない'
reject '-o/x'                           '<owner>/<repo> でも git URL でもない'
reject 'owner/repo/'                    '<owner>/<repo> でも git URL でもない'
reject '/etc/passwd x'                  '<owner>/<repo> でも git URL でもない'
reject 'https://example.invalid/x.git'  'URL 形式は <dir> が必須'
reject 'owner/repo ..'                  'dir が不正'
reject 'owner/repo .hidden'             'dir が不正'
reject 'owner/repo a/b'                 'dir が不正'
reject 'owner/repo ../x'                'dir が不正'
reject 'owner/repo -x'                  'dir が不正'
reject 'owner/repo a b'                 '項目が 3 つ以上'
printf 'a/b x\nc/d x\n' > "$R"; expect_fail check "dir 重複" "が重複"
printf 'example-org/crlf crlfdir\r\n' > "$R"; expect_ok list "CRLF 行"
[ "$(cat "$T/out")" = "$(printf 'crlfdir\thttps://github.com/example-org/crlf.git')" ] || { echo "CRLF 行の CR が dir/url に残る:" >&2; od -c "$T/out" >&2; exit 1; }

# --- sync: ローカル bare を clone、冪等、exclude ----------------------------------------------
seed="$T/seed"; git init -q "$seed"; git -C "$seed" commit -q --allow-empty -m init
for n in a b; do
    git init -q --bare "$T/bare/$n.git"
    git -C "$seed" push -q "$T/bare/$n.git" HEAD:refs/heads/main
done
printf 'file://%s/bare/a.git a\nfile://%s/bare/b.git b\n' "$T" "$T" > "$R"
expect_ok sync "2 repo の clone"
[ -d "$WS/a/.git" ] && [ -d "$WS/b/.git" ] || { echo "sync が a/b を clone していない" >&2; exit 1; }
[ ! -e "$WS/.git" ] || { echo "git 管理外の workspace に .git ができた" >&2; exit 1; }
expect_ok sync "冪等 (2 回目)"
[ "$(grep -c 'clone 済み' "$T/out")" -eq 2 ] || { echo "2 回目の sync が clone 済みと報告しない:" >&2; cat "$T/out" >&2; exit 1; }
expect_ok check "clone 済み宣言の整合"
git init -q "$WS"
expect_ok sync "git 管理下の workspace"
for d in a b; do
    [ "$(grep -cxF "/$d/" "$WS/.git/info/exclude")" -eq 1 ] || { echo "exclude に /$d/ が 1 行でない" >&2; cat "$WS/.git/info/exclude" >&2; exit 1; }
done
expect_ok sync "exclude の冪等"
[ "$(grep -cxF "/a/" "$WS/.git/info/exclude")" -eq 1 ] || { echo "exclude が重複追記された" >&2; exit 1; }
# 改行なし末尾の exclude (手編集) に追記しても前の行と癒着しない
printf '*.log' > "$WS/.git/info/exclude"
expect_ok sync "改行なし末尾の exclude への追記"
diff -u - "$WS/.git/info/exclude" <<'EXPECT' || { echo "改行なし末尾の exclude への追記が癒着/欠落 (上の diff)" >&2; exit 1; }
*.log
/a/
/b/
EXPECT
untracked=$(git -C "$WS" status --porcelain | grep -E '^\?\? (a|b)/' || true)
[ -z "$untracked" ] || { echo "clone した repo が workspace の git status に出る: $untracked" >&2; exit 1; }
# workspace root が repo の subdir でも exclude は toplevel 相対 (--show-prefix) になる
sub="$T/mono/sub"; scaffold "$sub/.devcontainer"; git init -q "$T/mono"
printf 'file://%s/bare/a.git a\n' "$T" > "$sub/.devcontainer/project/repos.txt"
bash "$sub/.devcontainer/core/bin/workspace-repos" sync >"$T/out" 2>&1 || { echo "subdir workspace の sync が失敗:" >&2; cat "$T/out" >&2; exit 1; }
grep -qxF '/sub/a/' "$T/mono/.git/info/exclude" || { echo "subdir workspace の exclude が toplevel 相対 (/sub/a/) でない:" >&2; cat "$T/mono/.git/info/exclude" >&2; exit 1; }

# --- origin の同一視 (scp 形 / host の大文字 / .git 有無) と、path の大文字小文字違いは別 repo ----------
git init -q "$WS/r"; git -C "$WS/r" remote add origin git@GitHub.com:example-org/example-repo.git
printf 'example-org/example-repo r\n' > "$R"; expect_ok check "scp 形 + host 大文字の origin と slug 宣言の同一視"
grep -q '^ok  r (clone 済み)' "$T/out" || { echo "r を clone 済みと判定しない:" >&2; cat "$T/out" >&2; exit 1; }
git -C "$WS/r" remote set-url origin https://github.com/example-org/example-repo
expect_ok check ".git 無し https origin と slug 宣言の同一視"
git -C "$WS/r" remote set-url origin https://github.com/Example-Org/Example-Repo.git
expect_fail check "path の大文字小文字違い (case-sensitive な remote では別 repo)" "origin が宣言と不一致"

# --- 拒否 (sync/check とも): 非 git dir / origin 不一致 / symlink / origin 無し / 到達不能 URL ---
mkdir "$WS/c"; printf 'file://%s/bare/a.git c\n' "$T" > "$R"
expect_fail sync "既存の非 git dir" "git repo でない"; expect_fail check "既存の非 git dir" "git repo でない"
git init -q "$WS/d"; git -C "$WS/d" remote add origin https://example.invalid/other.git
printf 'file://%s/bare/a.git d\n' "$T" > "$R"; expect_fail sync "origin 不一致" "origin が宣言と不一致"
git init -q "$WS/e"; printf 'file://%s/bare/a.git e\n' "$T" > "$R"; expect_fail check "origin 無し" "origin remote を読めない"
mkdir "$T/outside"; ln -s "$T/outside" "$WS/s"
printf 'file://%s/bare/a.git s\n' "$T" > "$R"; expect_fail check "symlink dir" "実ディレクトリでない"
ln -s "$T/nowhere" "$WS/dangling"   # GNU realpath は dangling を解決し (→ 実体不一致)、BSD は失敗する — どちらの拒否文言も symlink を指す
printf 'file://%s/bare/a.git dangling\n' "$T" > "$R"; expect_fail check "壊れた symlink" "symlink?"
printf 'file://%s/bare/missing.git m\n' "$T" > "$R"; expect_fail sync "到達不能 URL" "fatal"
[ ! -e "$WS/m" ] || { echo "失敗した clone が partial dir を残した: $WS/m" >&2; exit 1; }
rm "$R"; expect_fail check "repos.txt 不在" "無い:"
echo "ok  workspace-repos (構文 6 受理 / 13 拒否 / sync・冪等・exclude / origin 同一視 / 封じ込め fixtures)"
