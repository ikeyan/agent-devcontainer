# 事故の記録 (incidents)

実害またはレビューを貫通して後段で発覚した欠陥の台帳。各エントリは「何が起きたか → 原因クラス → どこに反映したか (規律の文言 / 検査 / ledger)」を短く記す。規律の本文 (AGENTS.md) には事故の列挙を置かない — ここに追記し、AGENTS.md 側は文言の抽象化とクラス単位の防止で応える。verified-facts と同じく「必要時に読む」。このファイルは verified-facts と違い混入検査 (check-contamination) の対象 — consumer 固有名は書かず一般化する。

## 契約の記憶断定 (→ 一次情報で確認してから書く + ledger)

- dnsmasq `log-queries=no` 不正値で起動不能 (log-queries は値を取らない)
- dnsmasq `server=/host/` の suffix 一致 — `<secret>.api.anthropic.com` まで upstream へ流れる DNS exfil 経路。反映: `redact_invariants_check.sh` §2
- compose volume 名の project スコープ — 固定名のつもりが project 接頭辞つきで別 volume になる。反映: `docker.md`
- Claude Code permission パスの `/` vs `//` — 単一 `/` は project-root 相対。反映: `redact_invariants_check.sh` §4
- 必要 capability の取りこぼし (iptables -m set の NET_RAW 等)。反映: `capabilities.md` + compose の cap_add コメント
- 2026-07 (PR #23): レビュー対応で導入した gh seed の起動時検査を 3 実装連続で作り直し (compose entrypoint → /proc/1/environ 読取 → sudoers env_keep)。3 つとも外部依存の実行時契約 — devcontainers CLI の overrideCommand が compose 宣言 entrypoint を破棄 / cap_drop:ALL の root は他 uid の /proc/*/environ を読めない / sudo の env_reset — を一次情報で確認せず記憶で書いたことが原因で、ロジック単体の検証は全部通っていた。反映: AGENTS.md ワークフロー先頭 (契約の事前確認) + 検証節 (実行時契約の実測)、ledger `devcontainers-cli.md` / `capabilities.md` / `sudo.md`、build 時 env_keep probe (core/Dockerfile)
- 2026-07 (PR #23): 混入検査 (check-contamination) の bypass 2 件 — `grep --exclude-dir=verified-facts` の glob が basename 一致で任意の同名 dir を除外 / `grep -r` が symlink target を非追従 — を xhigh multi-agent レビュー 6 ラウンドが見落とし、Codex bot が拾った。原因は同じ「外部ツールの契約を実測せず意味論を仮定した」クラスで、**レビューエージェント自身もツール契約由来の欠陥には盲点を持つ**。反映: ledger 除外をパス限定化 + symlink 拒否 (`bin/check_contamination.sh` の probe で pin)。教訓: 検査器がツールの挙動 (glob 意味論・symlink・exit code) に依存する箇所は、レビューに頼らず negative probe で実挙動を pin する
- 2026-07 (v0.2.0 の consumer 初 rebuild): gh seed 整合検査 (init-firewall.sh, postStart で sudo root) が `/home/node/.config/gh/hosts.yml` を読むが、dev は cap_drop:ALL で root に DAC_OVERRIDE が無く、node home の base 既定 700 を traverse できず `awk: Permission denied` で postStartCommand が exit → **devcontainer が開けない**。/proc/1/environ EACCES と同じ「capped root は node 所有物を読めない」クラスの再発 (通算 3 回目) で、hosts.yml 自体は 644 なのに親 dir の traverse で詰まる盲点。CI の build-images はビルドのみで postStart を実行せず、end-to-end smoke test (issue #25) が無いため runtime で初めて露見。反映: Dockerfile で `/home/node` を o+x (711) にし root の traverse だけ許す (files の perms は不変)。教訓: **root で node 所有パスを読む検査は、対象 file だけでなくパス全体の traverse 権 (親 dir の o+x) を確認する。postStart 経路は smoke test #25 で end-to-end 実行して回帰を pin する**
- 2026-07 (v0.2.4→v0.2.5 / tools #27 レビュー): secrets-proxy の観測ログ (`log_requests`) と BLOCK-ENDPOINT ログが request path をそのまま docker log に載せ、path 埋め込み credential (webhook 鍵・signed URL token 等) が漏れうる欠陥を Codex が指摘 (BLOCK 側は query も落としていなかった)。secret 流出を防ぐ proxy 自身が log 経由の流出源になる本末転倒。当初 `_safe_path` で query 除去 + token 状 segment redaction にしたが、マージ前 review + Codex が「segment の秘密性を長さ・文字種で推定する heuristic は `/hook/password` 等の短い/低エントロピー秘密を漏らし fail-closed でない」と P1 指摘 → **host のみ記録する fail-closed に切替え** (path/query を一切出さない。allowlist は domain ベースなので discovery は host で足り、詳細な path 調査は信頼された mitmweb debug で行う)。両 call site を `test_addon.py` で pin。req.path 契約は `mitmproxy.md`。却下した同 PR の指摘: dns-egress test の busybox pull 残留は標準 base image・冪等再利用・明示実行 target のため環境汚染でなく共有 cache 温存として受容。教訓: **秘密を扱う経路のログは、値を sanitize するのでなく秘密が載りうる要素 (ここでは path/query 全体) を fail-closed で出さない。heuristic な redaction は「安全に見えて短い秘密を漏らす」**

## セキュリティ照合の省略 (→ SECURITY-MODEL の不変条件を上から照合)

- `/proc/<pid>/environ` の同一 uid 読取 — 平文 cred が env 経由で漏れる
- 平文 cred の tool 可読 (ファイル所有・mode の見落とし)
- durable 書込先への spill (--rm で消えない場所へ秘密が残る)
- 許可済み宛先 api.anthropic.com 自体が exfil 経路 (許可リストの内側からの持ち出し)

## 検査器自身の欠陥 (→ negative probe + fail-closed な status 処理)

- 2026-07 (PR #23 レビュー): 混入検査の allowlist sed が接頭辞拡張を無害化して素通し / TMPDIR・checkout の絶対パス由来の偽赤 / `cd && scan` の失敗が「クリーン」(return 1) に合流する fail-open。反映: `bin/check_contamination.sh` の probe 群・PIPESTATUS 処理・cd 失敗を exit 2 で fail-closed 化。教訓: 検査器の制御フローで exit 1 (= grep no-match = クリーン) を多義に使うと別経路の失敗が誤って緑になる。
- 2026-07 (PR #23 レビュー): 起動時検査の実装が実行時契約を記憶で書いて 3 実装連続で作り直し (compose entrypoint → /proc/1/environ 読取 → sudoers env_keep) は上記「契約の記憶断定」に既出。加えて `make setup` の charset gate が make のコマンドライン `$` 展開 (`$USER`→`SER`) を検証前に受ける件は、`export` でシェル injection は封じたが make 展開は inherent — 開発者自己入力なので echo 表示で足りると判断。
