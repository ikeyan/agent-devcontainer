#!/usr/bin/env bash
# core/Dockerfile (marker 入り template) の marker 行を project/Dockerfile.{top,dev} の内容で
# 置換した生成 Dockerfile を stdout へ出す。書込と drift 検査は Makefile (gen-dockerfile /
# check-dockerfile-gen)。marker: @@PROJECT:top@@ (トップレベル) / @@PROJECT:dev@@ (dev ステージ内)。
# $1 で template を差し替えられる (check-dockerfile-gen の negative probe 用。既定は core/Dockerfile)。
# marker が top/dev ちょうど 1 回ずつでない template は fail-closed で弾く (部分置換を成功にしない)。
# ヘッダ 2 行も含め全出力を awk 内で buffer し、count 検証を通ってから出力する — 失敗時は stdout を
# 0 バイトに保つので、`>` の truncate 先に部分生成物が残らず、下流の diff も空入力を食って drift 扱いに
# なる。pipe 越しの exit 伝播自体は呼び出し側 recipe の `set -o pipefail` が担う (check-dockerfile-gen)。
set -euo pipefail
core="$(cd "$(dirname "$0")/.." && pwd)"
project="$core/../project"
template="${1:-$core/Dockerfile}"
for f in "$project/Dockerfile.top" "$project/Dockerfile.dev"; do
    [ -f "$f" ] || { echo "無い: $f (project 層が未 scaffold)" >&2; exit 1; }
done
awk -v top="$project/Dockerfile.top" -v dev="$project/Dockerfile.dev" -v tmpl="$template" '
    BEGIN {
        out = "# GENERATED FILE — 手編集しない (core/Dockerfile と project/Dockerfile.{top,dev} を編集し" ORS
        out = out "# `make -C .devcontainer/core gen-dockerfile` で再生成する)。" ORS
    }
    /^# @@PROJECT:top@@$/ { ntop++; while ((getline l < top) > 0) out = out l ORS; close(top); next }
    /^# @@PROJECT:dev@@$/ { ndev++; while ((getline l < dev) > 0) out = out l ORS; close(dev); next }
    { out = out $0 ORS }
    END {
        if (ntop + 0 != 1) { printf "marker @@PROJECT:top@@ が %d 回 (ちょうど 1 回が必要): %s\n", ntop + 0, tmpl > "/dev/stderr"; exit 1 }
        if (ndev + 0 != 1) { printf "marker @@PROJECT:dev@@ が %d 回 (ちょうど 1 回が必要): %s\n", ndev + 0, tmpl > "/dev/stderr"; exit 1 }
        printf "%s", out
    }
' "$template"
