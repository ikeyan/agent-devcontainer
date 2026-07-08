# CLAUDE.md

`ikeyan/agent-devcontainer` (devcontainer kit) での作業規則 (Claude 向け)。この repo の `core/` は
installer で各 consumer に同期される source of truth — **ここでの変更が下流全部に効く**ので、consumer 側の
「`core/` 手編集禁止」の裏返しとして、修正はすべてここで行い negative probe つきで pin する。redact
サンドボックスの脅威モデルは `core/redact/SECURITY-MODEL.md`、外部ツールの確定仕様は
`core/docs/verified-facts/` を参照 (必要時に読む)。

## 検証の規律 — レビュー指摘の再発防止

過去のレビューで繰り返し出た弱点クラスと、その再発を既定で防ぐための規則:

1. **外部ツールの契約は一次情報で確認してから書く。**
   dnsmasq / iptables / docker compose / Linux capability / Claude Code CLI 等の flag・設定値・semantics を
   記憶で断定しない。`man` / 公式 docs / `--test` で裏を取る。確定したら ledger
   (`core/docs/verified-facts/<topic>.md`) に出典つきで追記し、次回から記憶でなく ledger を引く。

2. **同一 uid の敵対的子プロセスを前提に脅威モデルを上から照合する。**
   セキュリティ変更を「done」と言う前に `core/redact/SECURITY-MODEL.md` の不変条件を順に確認する。
   推論だけで検証した気にならない。

3. **既定は fail-closed。** 部分成功 (一部の名前だけ解決した等) を成功扱いにしない。必要な事後条件
   (全許可名の解決 / placeholder の展開 / 必要 capability の存在) を明示 assert する。

4. **機械的に検証する。** 変更後は `make -C core check` を緑にしてから done と言う。「緑」は既存検査が
   通ったこと止まり — 今回の変更が生む新しい不変条件・失敗様式を既存検査が捕まえないなら、その検査が
   漏れている。その場で `core/Makefile` / `core/bin/redact_invariants_check.sh` に negative probe つきで
   足してから done と言う。重い/daemon 要の検査は `check` 集約でなく明示 target + CI step にする
   (例: `check-redact-image`, `check-redact-dns-egress`)。

5. **レビュー指摘は「その 1 箇所を直す」で終えず、同種が二度出にくい構造にする。** 指摘のたびに
   「この class の指摘が再発しないための構造変更は何か」を自問する — 誤用を型・既定値で構造的に防ぐ、
   抜けを negative probe で pin する、fail-open な既定を fail-closed にする、等。点の修正で閉じない。
   (この節全体がその実践 = 弱点 class を既定で潰す。)

## 検証ハブ (`make -C core check`)

- `core/Makefile` の `check` が構文 (bash -n / shellcheck / hadolint)・compose config・dnsmasq `--test`・
  JSON Schema・redact 不変条件 (`core/bin/redact_invariants_check.sh`)・redact エンジン契約
  (`core/bin/redact_selfcheck.py`)・addon テストを集約する。CI (`.github/workflows/check.yml`) が
  templates を scaffold して全緑を確認し、install 冪等性とイメージ実ビルドもゲートする。
- 検査器は CI に実在する前提で組み、明示の skip 分岐は置かない (無ければ自然にエラーになる。green が
  「全 check が実際に走った」ことを意味するようにする)。daemon/ネット要の重い検査だけ `check` 集約から
  外し、CI の専用 step で明示実行する。
