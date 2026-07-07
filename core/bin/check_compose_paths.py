#!/usr/bin/env python3
"""マージ済み compose config のホスト側パスの実在を検証する (make check-compose-paths)。

`docker/podman compose ... config` の出力 (stdin) を読み、各サービスの
  - build.context (ホストのビルドコンテキスト)
  - type: bind の source (ホスト bind 元)
  - security_opt の seccomp=<file> (unconfined/builtin を除くファイルパス)
を集め、ホスト上に実在するかを stat で assert する。1 つでも不在なら exit 1。

なぜ要るか: devcontainer.json は compose を [project/compose.yaml, core/compose.yaml] の順で
-f 列挙し、CLI は --project-directory を付けない (docs/verified-facts/devcontainers-cli.md)。
よって compose の project directory は最初の -f = .devcontainer/project/ になり、core/compose.yaml
内の相対ホストパスは core/ ではなく project/ 基準で解決される。`./podman/...` のような core/ を
意図した相対パスが project/ 基準で解決されて不在ファイルを指す誤アンカーは、`compose config` 自体は
成功してしまう (seccomp はこの環境の podman では config 時に inline されず、bind source の不在も
config は検査しない) ため、この stat 検査で fail-closed に捕まえる。

config 出力では build.context / bind source は絶対パスに解決済み。security_opt の seccomp は相対の
まま残る (config は inline しない) ので --project-dir 基準で解決する。相対で来た他のパスも保険で
--project-dir 基準に解決する。
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml


def _is_pathish(v: str) -> bool:
    return v.startswith("/") or v.startswith(".")


def collect_host_paths(doc: dict, project_dir: str) -> list[tuple[str, str, str]]:
    """(service, kind, resolved_abs_path) を返す。"""
    out: list[tuple[str, str, str]] = []

    def resolve(p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(project_dir, p))

    for name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue

        build = svc.get("build")
        ctx = None
        if isinstance(build, str):
            ctx = build
        elif isinstance(build, dict):
            ctx = build.get("context")
        # context が git URL / http(s) の場合はホストパスでないので除外する。
        if isinstance(ctx, str) and _is_pathish(ctx):
            out.append((name, "build.context", resolve(ctx)))

        for v in svc.get("volumes") or []:
            if isinstance(v, dict):
                if v.get("type") == "bind":
                    src = v.get("source")
                    if isinstance(src, str) and _is_pathish(src):
                        out.append((name, "bind.source", resolve(src)))
            elif isinstance(v, str):
                # 短縮形 src:dst[:mode]。src がパス形なら bind とみなす (config 後は通常 long 形だが保険)。
                src = v.split(":", 1)[0]
                if _is_pathish(src):
                    out.append((name, "bind.source", resolve(src)))

        for opt in svc.get("security_opt") or []:
            if isinstance(opt, str) and opt.startswith("seccomp="):
                val = opt[len("seccomp=") :]
                if val in ("unconfined", "builtin") or not val:
                    continue
                out.append((name, "seccomp", resolve(val)))

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--project-dir",
        required=True,
        help="compose の project directory (相対パスの解決基準 = 最初の -f のディレクトリ)",
    )
    args = ap.parse_args()
    project_dir = os.path.abspath(args.project_dir)

    doc = yaml.safe_load(sys.stdin)
    if not isinstance(doc, dict):
        print("check_compose_paths: stdin が compose config の mapping でない", file=sys.stderr)
        return 1

    paths = collect_host_paths(doc, project_dir)
    if not paths:
        print("check_compose_paths: ホスト側パスが 1 つも抽出できない (config 出力が空?)", file=sys.stderr)
        return 1

    missing = [(s, k, p) for (s, k, p) in paths if not os.path.exists(p)]
    if missing:
        for s, k, p in missing:
            print(f"NG: service '{s}' の {k} が実在しない: {p}", file=sys.stderr)
        return 1

    for s, k, p in paths:
        print(f"ok  {s} {k}: {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
