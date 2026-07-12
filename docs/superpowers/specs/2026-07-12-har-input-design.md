# redact パイプライン HAR 入力対応 — 設計

ブランチは PR #17 (docs 再編) のブランチから派生させる。

## 背景と実測済みの現状

- redact パイプラインの入力は mitmproxy flows ファイルを想定してきたが、browser devtools 由来の `.har` も入力したい。
- 実測 (kit venv の mitmproxy 12.2.3): `core/redact/flows_to_ndjson.py` は `io.FlowReader(stdin).stream()` を使っており、`FlowReader` が先頭 byte で HAR (`{`、Fiddler BOM 許容) / tnetstring を自動判別するため、**エンジン無変更で `.har` が end-to-end で正しく NDJSON 化される**。
- 上流 `mitmproxy/io/har.py` は base64 応答本文・Firefox の本文なし entry・Chrome/Slack の header 形式差を処理する。`response` の無い entry はファイル全体が `FlowReadException` になる (fail-closed)。

## 目的

「動くようにする」ではなく「動くことを構造で保証する」: 検査で pin し、契約文書と ledger に記録する。

## 変更点

1. `core/bin/redact_selfcheck.py` — 検査の拡張
   - 合成入力に HAR を追加: 2 entries (素直な JSON 応答 + `"encoding": "base64"` 応答)。
   - 各 `extract/*/redact_flow.py` の round-trip を flows 形式と HAR 形式の両方で実行。assert は共通: valid NDJSON / 行数 = entry 数 / 例外なく完了。
   - negative probe を追加: 先頭が `{` の壊れた入力を流し、0 行の静かな成功でなく非零 exit で fail することを pin。入力パースは全 app 共有の経路なので、per-app でなくエンジン直接実行 (`flows_to_ndjson.py`) に対して 1 回行う。
2. `core/redact/flows_to_ndjson.py` — docstring のみ。「mitmproxy flows file または HAR (FlowReader が自動判別)」と契約に明記。コード変更なし。
3. `core/bin/redact-flow` — usage/header のみ。`<raw.flows|raw.har>` を受けることを明記。mount 名は `/input/raw.flows` のまま (中身で自動判別されるため固定名でよい)。
4. `core/docs/verified-facts/mitmproxy.md` — 新節「FlowReader は HAR を自動判別して読む」。判別機構・har.py の処理範囲・response 無し entry の全体 fail・12.2.3 実測を、同梱 `io/io.py`・`io/har.py` を出典に記録。

## エラー処理

壊れた HAR は上流の `FlowReadException` による全体 fail をそのまま採用する (部分成功を成功扱いしない)。negative probe で pin する。

## 検証

- `make -C core check` (selfcheck 含む) 全緑。
- selfcheck の新規 assert が実際に検出することをドリルで確認 (壊れた HAR、行数不一致)。
