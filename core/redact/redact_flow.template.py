#!/usr/bin/env -S uv run
"""__APP__ 用の redact_flow。生 flow を stdin で受け、秘密を伏せた NDJSON を stdout に出す。

このアプリの flow を観察して、秘密 (トークン/Cookie/個人情報など) を伏せる redact を実装する。
最初は恒等 (無 redaction)。flow を見て pattern を足していく。

使い方 (リポジトリルートから):
  uv run extract/__APP__/redact_flow.py < raw.flows > flows_redacted.jsonl

注: redact サンドボックス内では egress を固めてあり uv は外部取得できない。
代わりに mitmproxy を焼き込んだ python で直接実行する:
  python3 extract/__APP__/redact_flow.py < /input/raw.flows > /tmp/out.jsonl

対象ドメイン外の flow を行ごと落とす場合は、受理ホストを module 直下でリテラル宣言する:
  SELFCHECK_HOSTS = ("api.example.com",)
これが無いと check-redact-selfcheck は「1 flow = 1 行」を要求し、filter するアプリは
落ちる。宣言すると検査器が受理ホストで合成 flow を作り、非対象ホストの 1 本が実際に
落ちることまで検査する (根拠は core/bin/redact_selfcheck.py の docstring)。
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
