#!/usr/bin/env bash
# hooks/pre-push (agent-sync が配る。PUSH_OK=1 の無い push を止める) を、git が実際に呼ぶ
# $GIT_COMMON_DIR/hooks/pre-push へ写す。linked worktree も同じ場所を共有し、checkout で消えない
# (core.hooksPath で作業ツリーの hooks/ を指すと、hooks/ の無い commit を checkout した worktree から hook 無しで push が通る)。
# 使い方: bin/install_pre_push_hook.sh [<作業ツリー>] (既定は cwd)。写す元はこのスクリプトのリポの hooks/pre-push。
# 前提: 作業ツリーが git の管理下 (でなければ git のエラーで止まる)。core.hooksPath (どの scope でも) が hook を $GIT_COMMON_DIR/hooks 以外へ向けていれば落とす。設定は書かない。
# 写す先の pre-push の状態ごとの扱い:
#   - 無い (壊れた symlink を含む): 写す
#   - hooks/pre-push と同じ中身で実行可能: 何もしない
#   - 上の 2 つのどちらでもない (旧版・利用者が置いた別の hook・実行ビットの無いもの): このリポのものかを中身から決められないので、上書きせず落として置き換えのコマンドを示す
set -euo pipefail

[ $# -le 1 ] || { echo "使い方: bin/install_pre_push_hook.sh [<作業ツリー>]" >&2; exit 2; }
src=$(cd "$(dirname "$0")/.." && pwd)/hooks/pre-push
# cmp は src が無いときも「違う」と同じく非 0 を返し、既存の hook を別物と誤診断するので、先に原因を分ける。
[ -f "$src" ] || { echo "$src が無い (.agent-sync/sync.sh が置く)" >&2; exit 1; }
cd "${1:-.}"

common=$(git rev-parse --path-format=absolute --git-common-dir)
hooks_dir=$(git rev-parse --path-format=absolute --git-path hooks)
if [ "$hooks_dir" != "$common/hooks" ]; then
	echo "hook の置き場が $hooks_dir で、$common/hooks でない。core.hooksPath を設定している全ての scope:" >&2
	git config --show-origin --show-scope --get-all core.hooksPath >&2 || true
	echo "それぞれ Execute: git config --file <その file> --unset-all core.hooksPath" >&2
	exit 1
fi
hook=$common/hooks/pre-push
if [ -x "$hook" ] && cmp -s "$src" "$hook"; then
	exit 0
fi
if [ -e "$hook" ]; then
	echo "$hook: hooks/pre-push と同じ実行可能なファイルでない (前に写した旧版か、利用者の別の hook。上書きしない)。中身を確かめ、置き換えてよければ Execute: install -m 755 '$src' '$hook'" >&2
	exit 1
fi
# 壊れた symlink は -e が偽なのでここに来る。同じディレクトリの一時ファイルに書いてから rename で置くので、
# symlink を辿らず、書きかけの hook を git や並行の起動に見せない。
mkdir -p "$common/hooks"
tmp=$(mktemp "$common/hooks/.pre-push.XXXXXX")
trap 'rm -f "$tmp"' EXIT
install -m 755 "$src" "$tmp"
mv -f "$tmp" "$hook"
echo "$hook: hooks/pre-push を写した"
