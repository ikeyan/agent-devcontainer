# redact-flow 実装計画 (handoff)

アプリ固有の redact 関数を、**サンドボックス化した子 claude** がユーザーと相談しながら
実装する仕組みを作る。このファイルは外部の claude への引き継ぎ。リポジトリルートから読むこと。

---

## 1. 背景 / パイプライン

mitmproxy で捕獲した生 flow から、アプリの API クライアント (valibot スキーマ) を起こすまでの流れ:

```
raw.flows (mitmproxy)
  └─(redact)→ flows_redacted.jsonl (NDJSON)   ← .devcontainer/core/redact/flows_to_ndjson.py + extract/<app>/redact_flow.py
       └─ bin/flows.ts (find/get/infer)        ← 探索・型推論
            └─ extract/<app>/client/**         ← 派生クライアント
```

redact は「特定パターン (JWT/FCM) だけ」では不十分で、本来**アプリの flow を観察しながら**
そのアプリ固有に実装すべきもの。そこで「生 flow を読める／`extract/<app>/` にだけ書ける子 claude」を
立ち上げ、ユーザーへインタビューしつつ redact を作る仕組みにする。

---

## 2. すでに完了している部分 (commit `2ef7cd2`、※未 push の可能性あり要確認)

- **`.devcontainer/core/redact/flows_to_ndjson.py`** … 汎用エンジン化済み。
  `flows_to_ndjson(redact: Callable[[str], str] = lambda s: s)` が stdin の flows を読み、
  HTTP flow ごとに 1 行の NDJSON を stdout に出す。各行 (JSON 文字列) を `redact` に通す。
  直接実行すると恒等 redact (無 redaction)。JWT/FCM パターンはここから**撤去済み**。
- **`extract/shiraseru-bus/redact_flow.py`** … 最初のアプリ別インスタンス。
  `flows_to_ndjson` を import (リポジトリルート相対 `parents[2]/"bin"` を sys.path に追加) し、
  JWT+FCM の `redact` を定義して `flows_to_ndjson(redact)` を呼ぶ。shebang は `#!/usr/bin/env -S uv run`。
  使い方: `uv run extract/shiraseru-bus/redact_flow.py < raw.flows > flows_redacted.jsonl`
- 検証済み (本物の mitmproxy flows を生成して round-trip)。redact 版は JWT/FCM をプレースホルダ化し
  生トークンを残さない／エンジン単体は素通し／両者とも valid NDJSON。

---

## 3. これから作るもの

### 3.1 `bin/redact-flow <app> <raw.flows>` (シェル launcher)

1. `OUT=extract/<app>` を作成 (無ければ)。
2. `OUT/redact_flow.py` が無ければ **identity テンプレ** (§5) から scaffold。
3. `.devcontainer/core/Dockerfile` 由来のサンドボックスイメージで `docker run --rm -it`。
   entrypoint = `claude` に §6 のインタビュー指示を与える。マウント／ネットワークは §4。
4. 子 claude はユーザーへインタビューしつつ `OUT/redact_flow.py` を編集→実行→検証を反復。

> このリポジトリには docker / bwrap 等が無い環境もある (現に開発中の devcontainer 内には docker 無し)。
> launcher は docker 前提で書き、docker が無ければ明示エラーで落とす。**ビルド/実行確認は docker のある
> ホストで行うこと** (この計画を書いた環境では未実行)。

### 3.2 サンドボックスイメージ

`.devcontainer/core/Dockerfile` を**ベースとして流用**する。理由: claude-code / uv+mitmproxy
(redact_flow.py を実行できる) / node+tsx (bin/flows.ts で中身を確認できる) / firewall egress 制御を
すでに持つ。dev 用 devcontainer との差分だけを薄い派生 Dockerfile か compose override で表現する
(§4 のマウント・ネットワーク・entrypoint)。

---

## 4. サンドボックス設計

### 4.1 ファイルシステム: 「何でも読める／`$OUT` にだけ書ける」(カーネルで強制)

```
-v $REPO:/workspace:ro                                  # リポジトリ全体を read-only で
-v $REPO/extract/<app>:/workspace/extract/<app>:rw      # 書けるのはここだけ (より具体的な mount が勝つ)
-v $(realpath <raw.flows>):/input/raw.flows:ro          # 生 flow (リポジトリ外の場合あり)
```

念のため (多層防御) claude の `--settings` で `$OUT` 外への `Write`/`Edit` を deny する:

```json
{ "permissions": {
    "deny":  ["Write(//**)", "Edit(//**)"],
    "allow": ["Write(/workspace/extract/<app>/**)", "Edit(/workspace/extract/<app>/**)"]
} }
```

> ただし**真の強制は read-only マウント**。settings の glob 構文は claude-code の版で確認すること。

### 4.2 ネットワーク / 機密漏洩モデル ★ここが設計の肝

子 claude は**生 flow (本物の秘密) をコンテキストに読み込む**。出ていく口を全部塞ぐ:

| 経路 | 対策 | 信頼の前提 |
|---|---|---|
| (1) モデル呼び出し `api.anthropic.com` | 不可避。許可する | **Anthropic を信じるしかない** |
| (2) claude-code テレメトリ (statsig/sentry) | env で無効化: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 等 | — |
| (3) ツール経由の任意 egress (Bash で curl/git/npm/pip) | **firewall allowlist を `api.anthropic.com` のみに**。dev の npm/pypi/github は**外す** | — |
| (4) DNS exfil (サブドメインにデータ符号化) | 残存リスク。可能なら resolver 固定 / IP ピン留め / UDP53 を制御 resolver のみ許可 | — |
| (5) MCP サーバ | redact サンドボックスでは**無効** (`--mcp-config` 渡さない / 既定を空に) | — |

**既定方針 (推奨)**: Anthropic 信頼は受け入れ、**egress を Anthropic-only に固める**
(テレメトリ/MCP 無効、firewall は api.anthropic.com だけ、DNS を絞る)。
こうすると信頼の前提が Anthropic だけになり、それは不可避なので受け入れる。

> 検討して**不採用**にした案 (記録): 「flow が出る前に diff レビュー」=モデル呼び出し経路に無力
> (読み込んだ瞬間に送信済み)。「no-network + paste transcript」=対話 claude がモデルに到達できず非現実的。
> `.devcontainer/core/init-firewall.sh` の `ALLOWED_DOMAINS` を**最小化**して流用するのが現実的。

---

## 5. `redact_flow.py` identity テンプレート

scaffold 時に置くひな型 (`<app>` を置換)。コメントは日本語、コードは既存に倣う。

```python
#!/usr/bin/env -S uv run
"""<app> 用の redact_flow。生 flow を stdin で受け、秘密を伏せた NDJSON を stdout に出す。

このアプリの flow を観察して、秘密 (トークン/Cookie/個人情報など) を伏せる redact を実装する。
最初は恒等 (無 redaction)。flow を見て pattern を足していく。

使い方 (リポジトリルートから):
  uv run extract/<app>/redact_flow.py < raw.flows > flows_redacted.jsonl
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from flows_to_ndjson import flows_to_ndjson  # noqa: E402


def redact(s: str) -> str:
    # TODO: このアプリ固有の秘密を伏せる。例:
    #   import re; s = re.compile(r"...").sub("<REDACTED>", s)
    return s


if __name__ == "__main__":
    flows_to_ndjson(redact)
```

---

## 6. 子 claude へのインタビュー用システムプロンプト (要点)

- あなたの仕事は `extract/<app>/redact_flow.py` の `redact` を、このアプリの flow に合わせて実装すること。
- 生 flow は `/input/raw.flows` にある。**本物の秘密を含む**。あなたの egress はモデル API のみに固めてあるが、
  秘密を不要に持ち出さない (生トークンを永続物へ貼らない)。
- 書けるのは `/workspace/extract/<app>/` だけ。他は read-only。
- 反復ループ:
  1. ユーザーにインタビュー (認証方式、どのヘッダ/Cookie/本文フィールド/URL に秘密が出るか、個人情報の有無)。
  2. `redact` を編集。
  3. `uv run extract/<app>/redact_flow.py < /input/raw.flows > /tmp/out.jsonl` で実行。
  4. `/tmp/out.jsonl` に秘密が残っていないか確認 (トークン形状の grep、行数が生と一致するか、
     `bin/flows.ts` で中身を覗く)。
- **不変条件**: 出力 NDJSON に本物の秘密素材が 1 つも残らないこと。迷ったら伏せる側に倒す。

---

## 7. 検証 (AGENTS.md: Makefile ターゲット化する)

ルート `Makefile` に `check-redact` を追加する案 (現状ルート Makefile は無い。`.devcontainer/core/Makefile`
の流儀に倣う = 相互独立・検査器が無ければ skip して exit 0・negative probe・副作用なし):

- 各 `extract/*/redact_flow.py` について、合成 flow (JWT/FCM/Cookie 等を含む) を mitmproxy io で作り、
  round-trip して「プレースホルダが出る／生トークンが残らない／valid NDJSON／行数一致」を assert。
- `mitmproxy` が import できなければ skip (`uv` の Python download がこの環境では firewall で失敗し得る点に注意)。
- `__pycache__` を残さない (`PYTHONDONTWRITEBYTECODE=1`)。

> 参考: この計画段階での手元検証は、sfw shim を PATH から外し実 `uv` を
> `--system-certs --no-project --python /usr/bin/python3 --with mitmproxy` で走らせて round-trip を確認した
> (この環境固有の firewall 回避。正規 devcontainer では素の `uv run` でよい)。

---

## 8. リポジトリ規約 (AGENTS.md より、守ること)

- 論理単位ごとにレビュー (diff でなく結果ファイルを初見読者として読む) → OK ならコミット。
- 検証はワンライナーでなく Makefile ターゲット経由。副作用を残さない。
- 自然言語コメントは読者を想定して書く。コード識別子は英語、説明は日本語が既存の流儀。
- commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。Conventional Commits (英語)。

---

## 9. 未決定 / ユーザー確認が要る点

- §4.2 のネットワーク既定方針 (Anthropic-only egress、テレメトリ/MCP 無効、DNS 制御) でよいか。
- `$OUT` は `extract/<app>/` で確定 (ユーザー承認済み)。
- launcher は `bin/redact-flow` (シェル) でよいか。生 flow の置き場 (リポジトリ管理しない想定?) の扱い。
- ルート `Makefile` を新設して `check-redact` を置いてよいか (現状ルート Makefile が無い)。
