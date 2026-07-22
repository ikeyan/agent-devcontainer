#!/usr/bin/env bash
# bin/compose-project-name (redact-flow の実導出経路) の検査。make check-redact-config が呼ぶ。
#   usage: check_projectname.sh <python> <project-compose.yaml>
# - 実 project 層での導出が PyYAML parse と一致すること (literal parse の形式 drift 検知)。
#   PyYAML 側は BaseLoader (全 scalar を str) — safe_load は YAML 1.1 解決で unquoted の no/010 を
#   bool/int にし、compose (yaml.v3 ≒ str) と食い違って偽赤になる (Norway problem)。
# - 受理すべき形 (unquoted+コメント / UTF-8 BOM / quoted±コメント) の fixture と、拒否すべき形
#   (name: 無し / 値密着の # / 引用内の空白+# / colon 直後に空白なし) の negative fixture。
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: check_projectname.sh <python> <project-compose.yaml>" >&2; exit 2; }
PY=$1
COMPOSE_FILE=$2
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
DERIVE="$SELF_DIR/compose-project-name"

# compose-project-name の失敗はその精密な stderr (違反行つき) をそのまま出す (set -e が止める)。
# 汎用文言を重ねない (REVIEW.md 簡素化「基盤ツールの失敗はそのまま見せる」)。
derived=$(bash "$DERIVE" "$COMPOSE_FILE")
expected=$("$PY" -c 'import sys, yaml; print(yaml.load(open(sys.argv[1]), Loader=yaml.BaseLoader)["name"])' "$COMPOSE_FILE") \
    || { echo "PyYAML で $COMPOSE_FILE の name: を読めない" >&2; exit 1; }
[ "$derived" = "$expected" ] \
    || { echo "compose-project-name の導出 '$derived' が PyYAML parse '$expected' と不一致 (literal parse の形式 drift)" >&2; exit 1; }

fx=$(mktemp)
trap 'rm -f "$fx"' EXIT
expect_ok() { # $1=期待名 $2=説明 (fixture は stdin)
    cat > "$fx"
    [ "$(bash "$DERIVE" "$fx")" = "$1" ] \
        || { echo "compose-project-name が $2 を読めない" >&2; exit 1; }
}
expect_reject() { # $1=説明 (fixture は stdin)
    cat > "$fx"
    if bash "$DERIVE" "$fx" >/dev/null 2>&1; then
        echo "negative: compose-project-name が $1 を素通し" >&2
        exit 1
    fi
}
printf 'name: plainprobe # comment\nservices: {dev: {}}\n' | expect_ok plainprobe "unquoted+コメント付き name:"
printf '\357\273\277name: bomprobe\nservices: {dev: {}}\n' | expect_ok bomprobe "UTF-8 BOM 付きファイルの name:"
printf 'name: "qprobe" # c\n' | expect_ok qprobe "double-quote+コメント付き name:"
printf "name: 'sqprobe'\n" | expect_ok sqprobe "single-quote の name:"
printf 'services:\n  dev: {image: busybox}\n' | expect_reject "name: 無し (directory fallback)"
printf 'name: glued#tag\n' | expect_reject "値密着の # (YAML ではコメントでない)"
printf 'name: "demo # x"\n' | expect_reject "引用内に空白+# を含む値 (silent 切詰めの穴)"
printf 'name:demo\n' | expect_reject "colon 直後に空白が無い行 (YAML では mapping でない)"
echo "ok  project 名導出 (PyYAML parity + fixtures)"
