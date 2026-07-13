# このリポジトリでの作業規則

この repo の `core/` は installer で各 consumer repo に同期される source of truth — ここでの変更が下流全部に効く。consumer 側の「`core/` 手編集禁止」の裏返しで、kit 由来物の修正はすべてここで行う。redact サンドボックスの脅威モデルは `core/redact/SECURITY-MODEL.md`、外部ツールの確定仕様は verified-facts ledger (`core/docs/verified-facts/`) を参照 (必要時に読む)。

## ワークフロー

- 作業が論理単位に達したら `/code-review` でレビューする (このリポジトリの観点は `REVIEW.md` に定義、レビューエージェントへ自動注入される)。findings は採否を判断して直し、OK ならコミットする。
  - `REVIEW.md` の正本は [`ikeyan/agent-files`](https://github.com/ikeyan/agent-files)。変更は正本側へコミットする (repo 固有のパス・語彙を持ち込まない)。正本との一致は CI (`make check-review-md`) が検査する。
- レビュー指摘は「その 1 箇所を直す」で終えず、同種の指摘が二度出にくい構造にする。指摘のたびに「この class の指摘が再発しないための構造変更は何か」を自問する。
  - 誤用は型・既定値で構造的に防ぐ
  - 検査の抜けはその場で検査器に足して pin する (「検証」節)
  - fail-open な既定は fail-closed にする
- コメントや文字列で自然言語を書く場合、その読者を想定して書く。

## 検証 (make check / Makefile)

- `make check` が全検証を集約する (fresh clone では先に `make setup`)。CI (`.github/workflows/check.yml`) が自動実行する。CI 限定でここに入らない検査はルート Makefile 冒頭コメント参照。
- 変更後は `make check` を緑にしてから done と言う — ここでの「緑」は、今回の変更が生む新しい不変条件・失敗様式まで検査が捕まえることを含む。既存検査が捕まえないなら検査が漏れており、その場でルート Makefile か `core/Makefile` の合う class の検査 (例: `core/bin/redact_invariants_check.sh`) に足す。
- 検証・調査・実験は、その場限りのワンライナーでなく Makefile のターゲットとして定着させ、次回から最小コマンドで再利用できるようにする。Makefile を検証・調査のアーミーナイフとして育て、重複・陳腐化したターゲットは整理して切れ味を保つ。
- Makefile 経由かアドホックかに関わらず、環境に副作用を残さない。
  - 例: `python3 -m py_compile` は `__pycache__` を残す → 文字列を `compile()` する。
- 検査の流儀 (相互独立・道具の実在前提・negative probe・重い検査の扱い) は `core/Makefile` 冒頭のコメントが規範。新規ターゲットにも既存検査器への追加にも適用する。

## 再発防止の規律 — 過去レビュー頻出の弱点クラス

1. 外部ツールの契約は一次情報で確認してから書く。dnsmasq / iptables / docker compose / Linux capability / Claude Code CLI 等の flag・設定値・semantics を記憶で断定しない。ledger に確定済みならそれを引き、無ければ `man` / 公式 docs / `--test` で裏を取り、ledger に出典つきで追記してから使う。
   - 過去の事故:
     - dnsmasq `log-queries=no` 不正値で起動不能
     - `server=/host/` の suffix 一致
     - compose volume 名の project スコープ
     - Claude Code permission パスの `/` vs `//`
     - 必要 capability の取りこぼし
2. セキュリティ変更は、同一 uid の敵対的子プロセスを前提に `SECURITY-MODEL.md` の不変条件を上から順に照合してから done と言う。推論だけで検証した気にならない。
   - 過去の事故:
     - `/proc/<pid>/environ` の同一 uid 読取
     - 平文 cred の tool 可読
     - durable 書込先への spill
     - 許可済み宛先 api.anthropic.com 自体が exfil 経路
3. 外部入力をパスに使うときは正規化して領域内に閉じる。`.`/`..`/symlink を拒否し、`realpath` で領域内を確認する。
   - リファレンス実装: `core/bin/redact-flow` の app 名検証。
4. 既定は fail-closed。部分成功 (例: 一部の名前だけ解決した) を成功扱いにせず、必要な事後条件を明示 assert する。
   - 事後条件の例:
     - 全許可名の解決
     - placeholder の展開
     - 必要 capability の存在

## 終了方法 (Node.js)

- 回復不可能な例外はキャッチしない (Node.js が自動的に終了する)。
- 明示的に終了する場合は `process.exit(code)` を使う。
