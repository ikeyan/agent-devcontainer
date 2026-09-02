#!/usr/bin/env -S uv run
"""__APP__ 用の redact_flow。生 flow を stdin で受け、秘密を伏せた NDJSON を stdout に出す。

このアプリの flow を観察して、秘密 (トークン/Cookie/個人情報など) を伏せる redact を実装する。
最初は恒等 (無 redaction)。flow を見て pattern を足していく。

特定ドメイン以外の flow を丸ごと落とす作りにする場合は、
SELFCHECK_DOMAINS = ("受理する root ドメイン", ...) を module トップレベルに 1 回だけ宣言し、
その同じ tuple で filter を実装する — 単純な形なら
flows_to_ndjson(redact, keep_domains=SELFCHECK_DOMAINS)、__main__ を自作するなら engine の
iter_flow_dicts(sys.stdin.buffer, SELFCHECK_DOMAINS) で読む (自前の正規表現や読み取りループを
書き直さない。宣言と実装がずれるし、Host ヘッダ偽装に強い実接続先 host 基準の filter も
engine 側にある)。check-redact-selfcheck が「1 flow = 1 行」の代わりに受理境界を検査し、
受理した各 flow は「1 flow = 1 つの JSON object の行、url の host が読める形」で出力する
契約になる (redact で url の host まで伏せない。詳細は
.devcontainer/core/bin/redact_selfcheck.py の docstring)。

使い方 (リポジトリルートから):
  uv run extract/__APP__/redact_flow.py < raw.flows > flows_redacted.jsonl

注: redact サンドボックス内では egress を固めてあり uv は外部取得できない。
代わりに mitmproxy を焼き込んだ python で直接実行する:
  python3 extract/__APP__/redact_flow.py < /input/raw.flows > /tmp/out.jsonl
"""

import sys
from pathlib import Path

# 汎用エンジンは .devcontainer/core/redact/flows_to_ndjson.py。リポジトリルート相対で import する
# (このファイルは extract/<app>/redact_flow.py = ルートから 2 階層下)。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".devcontainer" / "core" / "redact"))
from flows_to_ndjson import flows_to_ndjson  # noqa: E402


def redact(s: str) -> str:
    # TODO: このアプリ固有の秘密を伏せる。例:
    #   import re; s = re.compile(r"...").sub("<REDACTED>", s)
    return s


if __name__ == "__main__":
    flows_to_ndjson(redact)
