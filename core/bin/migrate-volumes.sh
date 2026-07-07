#!/usr/bin/env bash
# volume 論理名の rename (tools-* → 汎用名, redact auth の kit 名義化) に伴う一回きりのデータ移行。
# ホスト (docker/podman のある側) で実行する。旧が無い/新が既にあるペアは skip する冪等設計。
# node-modules / pnpm-store は再生成可能キャッシュなので対象外 (コンテナ内で pnpm install し直す)。
#   usage: .devcontainer/core/bin/migrate-volumes.sh [compose-project-name (default: tools)]
set -euo pipefail
ENGINE=$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)
PROJECT="${1:-tools}"
migrate() { # $1=旧 volume 名 $2=新 volume 名
    local old="$1" new="$2"
    "$ENGINE" volume inspect "$old" >/dev/null 2>&1 || { echo "skip: $old (無し)"; return; }
    "$ENGINE" volume inspect "$new" >/dev/null 2>&1 && { echo "skip: $new (既存)"; return; }
    "$ENGINE" volume create "$new" >/dev/null
    # cp -a (root 実行) で uid/gid/mode の複製を試みる。alpine の busybox cp -a 相当の再現度で、
    # 完全な属性保存を保証するものではない (差異が出ればコンテナ起動時の chown で吸収される想定)。
    # image は完全修飾する: registry 無しの short-name は podman では short-name 解決の対象で、
    # alias 未定義だと対話プロンプト = 非対話実行では失敗する (docs/verified-facts/podman.md「short-name 解決」)。
    "$ENGINE" run --rm -v "$old":/from:ro -v "$new":/to docker.io/library/alpine:3 sh -c 'cp -a /from/. /to/'
    echo "migrated: $old -> $new"
}
migrate "${PROJECT}_tools-bashhistory"   "${PROJECT}_bashhistory"
migrate "${PROJECT}_tools-claude-config" "${PROJECT}_claude-config"
migrate "${PROJECT}_tools-venv"          "${PROJECT}_venv"
migrate "${PROJECT}_tools-podman"        "${PROJECT}_podman"
migrate "tools-redact-claude"            "agent-devcontainer-redact-auth"
