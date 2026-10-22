#!/usr/bin/env python3
"""mktemp の呼び出し形を pin する (make check-mktemp)。

bare `mktemp` (引数なし / -d のみ / -t) は macOS で TMPDIR を見ず `_CS_DARWIN_USER_TEMP_DIR` に書く。
Claude Code の CLI サンドボックスは $TMPDIR 配下しか書かせないので、そこで mktemp は失敗して
**空文字列を返す**。`bad=$(mktemp); printf ... > "$bad"` 型の negative probe は、空パスへの
リダイレクト失敗 → 検査対象が走らないまま後続へ進み、検査器が壊れていても緑になる (fail-open)。
(canon: facts/shell/mktemp-tmpdir-handling-bsd-vs-gnu)

許す形 (どれも呼び出しの直後に `||` が要る — mktemp の失敗をその場で止める。set -e 下でも一律に
要求して、set -e の有無で検査器が分岐しないようにする):
  - shell:    $(mktemp [-d] -p "${TMPDIR:-/tmp}" [-d]) || ...
  - shell:    $(mktemp [-d] "<dir>/...XXXXXX") || ...      (置き場所を明示した template)
  - Makefile: $$($(MKTEMP) [-d]) || ...                   (MKTEMP = mktemp -p "$${TMPDIR:-/tmp}")
`$(MKTEMP)` を使うファイルは MKTEMP をその形で定義していること。

起動ごとに埋込 fixture で検出器自身を試す (negative probe — 悪い形を弾き、良い形を通す)。
CLI: `check_mktemp.py FILE...`
"""
from __future__ import annotations

import re
import sys

# 呼び出しの開始点: $( / $$( / ` の直後の mktemp か $(MKTEMP)。`$$(` を先に試すので
# Makefile の `$$(mktemp` を `$(mktemp` と二重に数えない。
INVOKE = re.compile(r"(?:\$\$?\(|`)\s*(?:mktemp\b|\$\(MKTEMP\))")
GOOD = [
    re.compile(r'\$\(mktemp(?: -d)? -p "\$\{TMPDIR:-/tmp\}"(?: -d)?\)"?\s*\|\|'),
    re.compile(r'\$\(mktemp(?: -d)? "[^"]*/[^"]*XXXXXX"\)"?\s*\|\|'),
    re.compile(r"\$\$\(\$\(MKTEMP\)(?: -d)?\)\s*\|\|"),
]
USES_MAKE_VAR = re.compile(r"\$\(MKTEMP\)")
MAKE_DEF = re.compile(r'^MKTEMP\s*:?=\s*mktemp -p "\$\$\{TMPDIR:-/tmp\}"\s*$', re.M)


def line_ng(line: str) -> bool:
    if line.lstrip().startswith("#"):
        return False
    n_invoke = len(INVOKE.findall(line))
    n_good = sum(len(g.findall(line)) for g in GOOD)
    return n_invoke != n_good


def check_text(text: str) -> list[str]:
    ngs = [f"{i}: {ln.strip()}" for i, ln in enumerate(text.splitlines(), 1) if line_ng(ln)]
    if USES_MAKE_VAR.search(text) and not MAKE_DEF.search(text):
        ngs.append('$(MKTEMP) を使うが `MKTEMP := mktemp -p "$${TMPDIR:-/tmp}"` の定義が無い')
    return ngs


# (行, 通るべきか)。悪い形はどれも実在した / しうる fail-open の型。
FIXTURES = [
    ('bad=$(mktemp); printf x > "$bad"', False),
    ("bad=$(mktemp -d)", False),
    ("bad=$(mktemp -t probe) || exit 1", False),
    ('bad=$(mktemp -p "${TMPDIR:-/tmp}"); printf x > "$bad"', False),
    ("\tbad=$$(mktemp) || exit 1; \\", False),
    ("\tbad=$$($(MKTEMP)); printf x > \"$$bad\"; \\", False),
    ('a=$(mktemp -p "${TMPDIR:-/tmp}") || exit 1; b=$(mktemp)', False),
    ("x=`mktemp` || exit 1", False),
    ('a=$(mktemp -p "${TMPDIR:-/tmp}") || exit 1', True),
    ('d=$(mktemp -d -p "${TMPDIR:-/tmp}") || { echo ng >&2; exit 1; }', True),
    ('a=$(mktemp -p "${TMPDIR:-/tmp}") || exit 1; b=$(mktemp -p "${TMPDIR:-/tmp}") || exit 1', True),
    ('tmp="$(mktemp "$RENDER/.hosts.XXXXXX")" || { rc=1; continue; }', True),
    ("\tbad=$$($(MKTEMP)) || exit 1; printf x > \"$$bad\"; \\", True),
    ("\ttmp=$$($(MKTEMP) -d) || exit 1; \\", True),
    ("# bare mktemp はコメントなら対象外: $(mktemp)", True),
]


def selftest() -> list[str]:
    errs = [f"negative: {'良い形を弾く' if ok else '悪い形を素通し'}: {ln!r}"
            for ln, ok in FIXTURES if line_ng(ln) == ok]
    if not check_text("x:\n\tt=$$($(MKTEMP)) || exit 1\n"):
        errs.append("negative: MKTEMP 未定義の $(MKTEMP) 使用を素通し")
    if check_text('MKTEMP := mktemp -p "$${TMPDIR:-/tmp}"\nx:\n\tt=$$($(MKTEMP)) || exit 1\n'):
        errs.append("negative: MKTEMP を定義した Makefile を弾く")
    return errs


def main(paths: list[str]) -> int:
    errs = selftest()
    if not paths:
        errs.append("検査対象ファイルが無い (呼び出し側の glob が空?)")
    n_invoke = 0
    for p in paths:
        with open(p, encoding="utf-8") as f:
            text = f.read()
        n_invoke += len(INVOKE.findall(text))
        errs += [f"NG {p}:{ng}" for ng in check_text(text)]
    if errs:
        print("\n".join(errs), file=sys.stderr)
        print('mktemp は `$(mktemp -p "${TMPDIR:-/tmp}") || exit 1` (Makefile は $$($(MKTEMP)) || exit 1) の形で呼ぶ'
              " (bare mktemp は macOS sandbox で空を返し negative probe が fail-open になる)", file=sys.stderr)
        return 1
    print(f"ok  mktemp ({len(paths)} files, {n_invoke} 呼び出し: -p TMPDIR + || 付き)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
