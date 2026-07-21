# 事故の記録 (incidents)

実害またはレビューを貫通して後段で発覚した欠陥の台帳。各エントリは「何が起きたか → 原因クラス → どこに反映したか (規律の文言 / 検査 / ledger)」を短く記す。規律の本文 (AGENTS.md) には事故の列挙を置かない — ここに追記し、AGENTS.md 側は文言の抽象化とクラス単位の防止で応える。verified-facts と同じく「必要時に読む」。このファイルは verified-facts と違い混入検査 (check-contamination) の対象 — consumer 固有名は書かず一般化する。

## 契約の記憶断定 (→ 一次情報で確認してから書く + ledger)

- dnsmasq `log-queries=no` 不正値で起動不能 (log-queries は値を取らない)
- dnsmasq `server=/host/` の suffix 一致 — `<secret>.api.anthropic.com` まで upstream へ流れる DNS exfil 経路。反映: `redact_invariants_check.sh` §2
- compose volume 名の project スコープ — 固定名のつもりが project 接頭辞つきで別 volume になる。反映: `docker.md`
- Claude Code permission パスの `/` vs `//` — 単一 `/` は project-root 相対。反映: `redact_invariants_check.sh` §4
- 必要 capability の取りこぼし (iptables -m set の NET_RAW 等)。反映: `capabilities.md` + compose の cap_add コメント
- 2026-07 (PR #23): レビュー対応で導入した gh seed の起動時検査を 3 実装連続で作り直し (compose entrypoint → /proc/1/environ 読取 → sudoers env_keep)。3 つとも外部依存の実行時契約 — devcontainers CLI の overrideCommand が compose 宣言 entrypoint を破棄 / cap_drop:ALL の root は他 uid の /proc/*/environ を読めない / sudo の env_reset — を一次情報で確認せず記憶で書いたことが原因で、ロジック単体の検証は全部通っていた。反映: AGENTS.md ワークフロー先頭 (契約の事前確認) + 検証節 (実行時契約の実測)、ledger `devcontainers-cli.md` / `capabilities.md` / `sudo.md`、build 時 env_keep probe (core/Dockerfile)

## セキュリティ照合の省略 (→ SECURITY-MODEL の不変条件を上から照合)

- `/proc/<pid>/environ` の同一 uid 読取 — 平文 cred が env 経由で漏れる
- 平文 cred の tool 可読 (ファイル所有・mode の見落とし)
- durable 書込先への spill (--rm で消えない場所へ秘密が残る)
- 許可済み宛先 api.anthropic.com 自体が exfil 経路 (許可リストの内側からの持ち出し)

## 検査器自身の欠陥 (→ negative probe + fail-closed な status 処理)

- 2026-07 (PR #23 レビュー): 混入検査の allowlist sed が接頭辞拡張を無害化して素通し / TMPDIR・checkout の絶対パス由来の偽赤 / `cd && scan` の失敗が「クリーン」(return 1) に合流する fail-open。反映: `bin/check_contamination.sh` の probe 群・PIPESTATUS 処理・cd 失敗を exit 2 で fail-closed 化。教訓: 検査器の制御フローで exit 1 (= grep no-match = クリーン) を多義に使うと別経路の失敗が誤って緑になる。
- 2026-07 (PR #23 レビュー): 起動時検査の実装が実行時契約を記憶で書いて 3 実装連続で作り直し (compose entrypoint → /proc/1/environ 読取 → sudoers env_keep) は上記「契約の記憶断定」に既出。加えて `make setup` の charset gate が make のコマンドライン `$` 展開 (`$USER`→`SER`) を検証前に受ける件は、`export` でシェル injection は封じたが make 展開は inherent — 開発者自己入力なので echo 表示で足りると判断。
