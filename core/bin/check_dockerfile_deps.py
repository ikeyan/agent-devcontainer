#!/usr/bin/env python3
"""Dockerfile の外部 image 依存を AST で機械検査する (make -C .devcontainer/core check-dockerfile-deps)。

規則 (根拠は docs/verified-facts/dependabot.md):
  1. FROM の image 参照が先行ステージ alias でない (= 外部 image) なら @sha256:<digest> 必須。
     Dependabot の docker ecosystem は FROM 行しか解析しないので、digest まで固定した外部 image だけが
     版・digest の自動追従に載る (digest は tag 再push の改竄/取り違え検知も兼ねる)。
     scratch (registry image でなく digest 固定不能な予約ベース) は除外する。
  2. COPY --from=<ref> の <ref> は先行ステージ alias でなければならない。COPY --from の image 参照は
     Dependabot が追従しない (FROM 行でない) ので、COPY で焼く image は FROM <image> AS <stage> の
     名前付きステージにして --from=<stage> で参照する。

tree-sitter-containerfile の AST node 名 (from_instruction / image_spec / image_name / image_digest /
image_alias / copy_instruction / param) は実 parse で確認済み: docs/verified-facts/tree-sitter-containerfile.md。

self-probe (redact_selfcheck.py と同じ流儀): 埋め込み fixture の digest 無し FROM / COPY --from=外部image
を検出できなければ fail、正常 fixture (alias 参照 + digest 付き FROM) を誤検知したら fail。検査器自体が
機能していることを毎回 pin する。
"""
from __future__ import annotations

import sys
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_containerfile as tsc

_PARSER = Parser(Language(tsc.language()))


def _named(node, type_name: str) -> list:
    return [c for c in node.named_children if c.type == type_name]


def find_violations(src: bytes) -> list[str]:
    """src (Dockerfile bytes) の規則違反メッセージを返す (空なら合格)。"""
    root = _PARSER.parse(src).root_node
    aliases: set[str] = set()
    errs: list[str] = []
    for inst in root.named_children:
        line = inst.start_point[0] + 1
        if inst.type == "from_instruction":
            specs = _named(inst, "image_spec")
            if not specs:
                continue
            spec = specs[0]
            names = _named(spec, "image_name")
            image = names[0].text.decode() if names else spec.text.decode()
            # 先行ステージ alias への参照 (FROM base AS dev) と scratch (registry image でなく
            # digest 固定不能な予約ベース) は外部 image でないので digest 不要。
            if image not in aliases and image != "scratch" and not _named(spec, "image_digest"):
                errs.append(
                    f"L{line}: FROM {spec.text.decode()} が @sha256:<digest> 未固定 "
                    "(外部 image は digest 必須)"
                )
            # このステージが定義する alias は、以降の FROM/COPY からのみ参照可 (自己参照は不可)。
            for al in _named(inst, "image_alias"):
                aliases.add(al.text.decode())
        elif inst.type == "copy_instruction":
            for p in _named(inst, "param"):
                t = p.text.decode()
                if t.startswith("--from="):
                    ref = t[len("--from="):]
                    if ref not in aliases:
                        errs.append(
                            f"L{line}: COPY --from={ref} が先行ステージ alias でない "
                            "(外部 image は COPY で焼かず FROM ステージにする)"
                        )
    return errs


_D = b"@sha256:" + b"a" * 64
_BAD_FROM = b"FROM alpine:3\n"
_BAD_COPY = b"FROM node:24" + _D + b" AS base\nCOPY --from=alpine:3 /bin/sh /bin/sh\n"
_GOOD = b"FROM node:24.17.0-trixie" + _D + b" AS base\nFROM base AS dev\nCOPY --from=base /a /b\n"
_GOOD_SCRATCH = b"FROM scratch AS empty\n"


def _self_probe() -> list[str]:
    problems = []
    if not find_violations(_BAD_FROM):
        problems.append("self-probe: digest 無し FROM を検出できなかった")
    if not find_violations(_BAD_COPY):
        problems.append("self-probe: COPY --from=外部image を検出できなかった")
    for label, src in (("正常", _GOOD), ("scratch", _GOOD_SCRATCH)):
        fp = find_violations(src)
        if fp:
            problems.append(f"self-probe: {label} fixture を誤検知した: {fp}")
    return problems


def main(argv: list[str]) -> int:
    files = argv[1:]
    if not files:
        print("usage: check_dockerfile_deps.py <Dockerfile> ...", file=sys.stderr)
        return 2
    failed = False
    for p in _self_probe():
        print(p, file=sys.stderr)
        failed = True
    for f in files:
        path = Path(f)
        if not path.exists():
            print(f"NG  {f}: ファイルが無い (生成漏れ?)", file=sys.stderr)
            failed = True
            continue
        errs = find_violations(path.read_bytes())
        if errs:
            failed = True
            print(f"NG  {f}", file=sys.stderr)
            for e in errs:
                print(f"      {e}", file=sys.stderr)
        else:
            print(f"ok  {f}")
    if failed:
        print("dockerfile-deps FAILED", file=sys.stderr)
        return 1
    print("ok  dockerfile-deps (FROM digest 固定 + COPY --from alias / self-probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
