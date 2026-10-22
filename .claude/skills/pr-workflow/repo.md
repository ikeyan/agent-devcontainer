# PR / ブランチ運用: このリポの値

[SKILL.md](SKILL.md) が読む、このリポ固有の値。SKILL.md と手段のファイルは agent-sync で ikeyan/agent-files からそのまま写すので、リポごとに違うものはこのファイルだけに書く。

## 値

- push 先: `origin` (2026-10-22 に `gh api repos/ikeyan/agent-devcontainer --jq .permissions.push` が true。push が権限で拒否されたら同じコマンドで確かめ直す)
- 寄稿規約: 文書は無い。このリポで通っている規約:
  - コミットメッセージと PR タイトルは「type(scope): 日本語の要約」の形 (type は feat / fix / refactor / build など。scope は省略可)。本文で理由を述べる。
  - PR は squash merge し、タイトル末尾に「(#PR番号)」が付く。
  - ブランチ名は内容を表す kebab-case (例: `selfcheck-domains`)。既定ブランチは `main`。
- CI: `check` (`.github/workflows/check.yml`。job `check` が `make setup` → `make check` を回し、`install-idempotency` と `build-images` が installer とイメージ実ビルドを検査する。pull_request と main への push で走る)

## 手段の実測

- gh: 2026-10-22 に読み取りの API が通った。Claude Code の CLI の sandbox の中では TLS の検証が `x509: OSStatus -26276` で落ちるので、sandbox の外で走らせる。sandbox の中で `gh api repos/ikeyan/agent-devcontainer` が通るようになったら、この但し書きを消す。
- github-mcp-server.md・cc-web-github-mcp.md: 実測していない。

## 方針の差分

- **Codex Review**: 使っている (chatgpt-codex-connector が PR にレビューを付ける)。
- **PR の作成**: 頼まれなくても、作業の区切りで push したら作る。
- **PR コメントの watch**: PR を作った・push したら常に回す。cc-web では `subscribe_pr_activity`、それ以外は手段ファイルの「PR の watch」を使う。
