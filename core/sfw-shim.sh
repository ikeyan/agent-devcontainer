#!/bin/bash
# パッケージマネージャを常に Socket Firewall (sfw) 経由で実行する shim。
# /usr/local/share/sfw-shims/<cmd> -> .sfw-exec の symlink として置かれ、
# PATH の先頭でホンモノより先に解決される。
# shim ディレクトリ自身を PATH から外してから sfw に渡すことで、
# sfw が実体コマンドを解決でき、shim の無限再帰を防ぐ。
set -euo pipefail
shim_dir=$(cd "$(dirname "$0")" && pwd)
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$shim_dir" | paste -sd:)
export PATH
exec sfw "$(basename "$0")" "$@"
