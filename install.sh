#!/usr/bin/env bash
# agent-devcontainer installer: .devcontainer/core を同期し、project 層と root glue を初回だけ scaffold する。
#   curl -fsSL https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --ref v0.1.0 --dir .devcontainer
# 再実行 = 更新 (冪等)。core/ は毎回全置換 (手編集禁止領域)、scaffold 済みファイルは二度と触らない。
# 実行場所: 導入先リポジトリの root。
set -euo pipefail

REPO_SLUG="ikeyan/agent-devcontainer"
REF=""
DIR=".devcontainer"
SRC=""            # --src <dir>: ダウンロードせずローカル checkout を payload に使う (CI / 開発用)
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

tmp=""
# if 文 (`[ -n "$tmp" ] && rm -rf "$tmp"` の && 短絡ではなく): tmp が空 (--src 使用時) だとテスト自体の
# 非 0 が cleanup の最終終了コードになり、set -e 下では EXIT trap 経由でその非 0 がスクリプト全体の
# 終了コードに漏れる (成功時も非 0 終了してしまう bash の trap/errexit 相互作用。実測で確認済み — if 文は
# 条件が偽でも then 節を実行しなければ exit status 0 になる POSIX 仕様のため回避できる)。
cleanup() { if [ -n "$tmp" ]; then rm -rf "$tmp"; fi; }
trap cleanup EXIT

if [ -z "$SRC" ]; then
  if [ -z "$REF" ]; then
    # 既定 ref = 最新 release tag (jq 非依存で tag_name を抜く。release 未作成なら空になり、
    # 下の tarball 取得が 404 で自然に失敗する — その場合は --ref main を指定する)
    REF=$(curl -fsSL "https://api.github.com/repos/$REPO_SLUG/releases/latest" \
      | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
  fi
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/$REPO_SLUG/archive/$REF.tar.gz" | tar -xz -C "$tmp"
  # tarball の top dir 名は ref 表記に依存する (docs/verified-facts/github-tarball.md) — glob で拾う
  set -- "$tmp"/*/
  SRC="${1%/}"
else
  REF="${REF:-local:$SRC}"
fi

# --- core: 全置換同期 (rsync --delete と同じ事後条件。rsync 非依存) -----------------
mkdir -p "$DIR"
rm -rf "$DIR/core"
cp -Rp "$SRC/core" "$DIR/core"
printf '%s\n' "$REF" > "$DIR/core/VERSION"
echo "synced: $DIR/core ($REF)"

# --- project 層 / glue: 無ければ scaffold、有れば触らない ---------------------------
# compose project 名 = カレントディレクトリ名を compose の名前制約 [a-z0-9][a-z0-9_-]* に整形
name=$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-' | sed 's/^[_-]*//')
[ -n "$name" ] || name=devcontainer

scaffold() { # $1=templates 内の src ($SRC 相対) $2=先 (導入先 repo 相対)。既存なら 1 を返す
  if [ -e "$2" ]; then return 1; fi
  mkdir -p "$(dirname "$2")"
  cp -Rp "$SRC/templates/$1" "$2"
  echo "scaffolded: $2"
}

if scaffold project "$DIR/project"; then
  sed -i.bak "s/@@PROJECT_NAME@@/$name/" "$DIR/project/compose.yaml" && rm -f "$DIR/project/compose.yaml.bak"
fi
if scaffold devcontainer.json "$DIR/devcontainer.json"; then
  sed -i.bak "s/@@PROJECT_NAME@@/$name/" "$DIR/devcontainer.json" && rm -f "$DIR/devcontainer.json.bak"
fi
scaffold github/workflows/devcontainer-check.yml .github/workflows/devcontainer-check.yml || true
scaffold github/workflows/devcontainer-kit-update.yml .github/workflows/devcontainer-kit-update.yml || true
scaffold github/dependabot.yml .github/dependabot.yml \
  || { echo "note: .github/dependabot.yml は既存。次の内容の取り込みを検討:"; sed 's/^/  | /' "$SRC/templates/github/dependabot.yml"; }
scaffold claude/settings.json .claude/settings.json \
  || { echo "note: .claude/settings.json は既存。次の SessionStart hook の追記を検討:"; sed 's/^/  | /' "$SRC/templates/claude/settings.json"; }

# --- Dockerfile 生成 (core template + project fragments) ----------------------------
bash "$DIR/core/bin/gen-dockerfile.sh" > "$DIR/Dockerfile"
echo "generated: $DIR/Dockerfile"

cat <<EOF
done. 次の手順:
  1. git diff で取り込み内容をレビューして commit
  2. project/ を編集 (allow-domains.txt / .env / rules.yaml / Dockerfile.{top,dev})。
     Dockerfile fragment 変更後: make -C $DIR/core gen-dockerfile
  3. 検証: make -C $DIR/core check
  4. secrets-proxy 初回認証: make -C $DIR/core bootstrap
EOF
