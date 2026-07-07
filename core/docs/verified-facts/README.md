# 検証済み事実 — 外部ツールの確定仕様 ledger

記憶からの再導出を断つための台帳。Codex review で「記憶で書いて間違えた」契約を、出典つきで固定する。
新たに一次情報で確認した仕様は該当トピックのファイルに追記してから使う (無ければ新規ファイルを足す)。

鮮度規約: 実測の引用 (日付つき「実測」節・具体的なコマンド出力・当時の再現手順) は測定当時のパス/値を
そのまま保持する (歴史的事実なので後から書き換えない)。一方、処方 (現在どうすべきか) の行はリポジトリの
live 状態に追従させて更新する。両者が食い違うときは処方行を正とし、実測側には「当時のパス」注記を添える。

confidence: `[docs]`=公式文書, `[man]`=man/manual, `[empirical]`=実機/実挙動で確認,
`[review]`=Codex 指摘 (一次未確認のものはその旨明記)。

## トピック

- [network.md](network.md) — dnsmasq / Docker の iptables・DNS / NO_PROXY suffix 一致
- [capabilities.md](capabilities.md) — Linux capabilities (`cap_drop: ALL` 下)
- [docker.md](docker.md) — Docker compose / `/proc` マスク / Docker Desktop (macOS) runc
- [devcontainers-cli.md](devcontainers-cli.md) — devcontainers CLI の compose `-f` 渡し順 / `--project-directory` 不使用 / project 名 fallback
- [podman.md](podman.md) — rootless Podman (newuidmap, overlay, userns, build format)
- [claude-code.md](claude-code.md) — Claude Code CLI
- [nodejs.md](nodejs.md) — Node.js (undici の proxy 対応 / .ts 型ストリッピングの直接実行)
- [gh.md](gh.md) — gh CLI (認証, config マイグレーション)
- [git.md](git.md) — merge-backend rebase の phantom-dirty (virtiofs で core.checkStat=minimal)
- [github-releases-sha256.md](github-releases-sha256.md) — GitHub Releases 成果物の sha256
- [mitmproxy.md](mitmproxy.md) — mitmweb 11.1.3 の web_* オプション/認証常時ON/flow_detail 差/Host allowlist 無し
- [dependabot.md](dependabot.md) — Dependabot docker ecosystem / OCI image の digest 固定
- [tree-sitter-containerfile.md](tree-sitter-containerfile.md) — Dockerfile parse の AST node 名 (check_dockerfile_deps)
- [biome.md](biome.md) — Biome `files.includes` の negation (`!pattern`) はルートアンカー
- [github-actions.md](github-actions.md) — GitHub Actions hosted runner (ubuntu-24.04) の docker/compose 同梱
