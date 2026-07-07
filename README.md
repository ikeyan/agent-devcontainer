# agent-devcontainer

agent CLI (Claude Code 等) を封じ込め付きで動かす devcontainer キット。
dev コンテナ + egress firewall (iptables/ipset) + secrets-proxy (平文 credential を dev に置かない) +
redact サンドボックス + 検証群 (`make check`) を、1 コマンドで任意のリポジトリへ導入・更新する。
アーキテクチャ詳細は [core/README.md](core/README.md)、redact の脅威モデルは
[core/redact/SECURITY-MODEL.md](core/redact/SECURITY-MODEL.md)、外部ツールの確定仕様は
[core/docs/verified-facts/](core/docs/verified-facts/)。

## インストール / 更新

```shell
curl -fsSL https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh | bash
```

導入先リポジトリの root で実行する。再実行 = 更新 (冪等)。`core/` は毎回全置換、`project/` ほか
scaffold 済みファイルは二度と触らない。

| option | 既定 | 意味 |
|---|---|---|
| `--ref` | 最新 release tag | 取得する kit の版 (branch/tag/SHA) |
| `--dir` | `.devcontainer` | 導入先ディレクトリ |
| `--src` | (なし) | ダウンロードせずローカル checkout を使う (CI/開発用) |

```shell
curl -fsSL https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh \
  | bash -s -- --ref v0.1.0 --dir .devcontainer
```

release が 1 つも無いと既定 ref の解決に使う `releases/latest` API 自体が 404 になり、`curl -f` が
失敗して `pipefail` で `REF=$(...)` の代入ごと fail-closed する (REF が空になる前にここで止まる)。
その場合は `--ref main` を明示する。

curl \| bash を避けたい場合:

```shell
gh api repos/ikeyan/agent-devcontainer/tarball/main | tar xz
bash ikeyan-agent-devcontainer-*/install.sh --src ikeyan-agent-devcontainer-*
```

(`gh api .../tarball/<ref>` が展開する top dir は `<owner>-<repo>-<short-sha>` — install.sh 自身が
使う `/archive/<ref>.tar.gz` 直叩きとは生成規則が別物。`core/docs/verified-facts/github-tarball.md` 参照)

**要件**: `bash` / `curl` / `tar`。git は installer 自体には不要だが、取り込み内容を `git diff` で
レビューしてから commit する運用を前提にしている。devcontainer の実行には docker または podman
(+ compose plugin) が要る。

実行し終えると次の手順が標準出力に案内される (`--dir` 既定の `.devcontainer` の場合):

```
done. 次の手順:
  1. git diff で取り込み内容をレビューして commit
  2. project/ を編集 (allow-domains.txt / .env / rules.yaml / Dockerfile.{top,dev})。
     Dockerfile fragment 変更後: make -C .devcontainer/core gen-dockerfile
  3. 検証: make -C .devcontainer/core check
  4. secrets-proxy 初回認証: make -C .devcontainer/core bootstrap
```

## 導入先のレイアウト

`--dir` (既定 `.devcontainer`) 配下:

| パス | 由来 | 手編集 |
|---|---|---|
| `core/` | installer が毎回全置換で同期 | 不可 (次回同期で消える。中身は [core/README.md](core/README.md)) |
| `project/` | install.sh が無ければ初回のみ scaffold。以後 installer は触らない | 可 (consumer 所有) |
| `devcontainer.json` | 同上。`dockerComposeFile: ["project/compose.yaml", "core/compose.yaml"]` の順で重ね、project 側 (先頭) が compose の project directory になる | 可 (薄い project 所有) |
| `Dockerfile` | `core/bin/gen-dockerfile.sh` が `core` の template と `project/Dockerfile.{top,dev}` から生成し、install.sh が毎回上書き | 不可 (生成物。直接編集は次回の再生成で消える) |

`--dir` の外にも、install.sh は**初回インストール時のみ** (= 同期前に `core/` が無かった実行でのみ)
次の root glue を scaffold する: `.github/workflows/devcontainer-check.yml`,
`.github/workflows/devcontainer-kit-update.yml`, `.github/dependabot.yml`, `.claude/settings.json`
(workflows は既存なら黙ってスキップ、`dependabot.yml`/`.claude/settings.json` は既存なら取り込み候補を
stdout に表示する)。更新実行 (core が既にある) は glue を一切触らないので、不要な glue は削除すれば
復活しない。既存の `.devcontainer` 運用から移行するなど「core は有るが glue が欲しい」場合は、
リポジトリの `templates/github/` / `templates/claude/` から手動でコピーする。

### project 層の編集ポイント

- **`project/Dockerfile.top` / `project/Dockerfile.dev`** — `core/Dockerfile` テンプレートの
  `@@PROJECT:top@@` (トップレベル。追加 `ARG` や追加 `FROM` ステージ) / `@@PROJECT:dev@@` (dev ステージ
  内・`USER root` 区間。追加パッケージや `COPY`) に挿入される fragment。変更後は
  `make -C .devcontainer/core gen-dockerfile` で `Dockerfile` を再生成する。
- **`project/allow-domains.txt`** (iptables 直結許可。1 行 1 ドメイン) と **`project/.env`** の
  `PROJECT_NO_PROXY` (proxy バイパス。カンマ区切り) は対で維持する。前者は image に `COPY` されるため
  編集後は dev イメージの rebuild が必要。
- **`project/rules.yaml`** — secrets-proxy の `inject`/`capture`/`block` ルール (秘密そのものは含まず、
  Vaultwarden のアイテム名と宛先ホストだけ)。構造は `core/secrets-proxy/rules.schema.json` が定義し
  `make -C .devcontainer/core check-rules` で検証する。
- **`project/compose.yaml`** — project 固有の compose 層。追加 volume / build args / 環境変数は
  `services.dev` 以下に足す。`name:` (compose project 名) は scaffold 時にカレントディレクトリ名から
  自動設定される。
- **`project/.env`** — compose の変数補間専用 (`PROJECT_NO_PROXY`, secrets-proxy bootstrap 用の
  `PROJECT_BW_SERVER`)。dev コンテナのプロセス環境には渡らない。

## 検証

```shell
make -C .devcontainer/core check
```

CI 例: `templates/github/workflows/devcontainer-check.yml` (scaffold 元として導入先に配布され、
push/PR ごとに上記を実行する)。`check-seccomp` は node>=23.6 を要する (`.ts` を型ストリッピングで
直接実行。tsx 等の追加依存は不要)。compose 系の check は docker または podman (+ compose) が必須。

## 更新フロー

- **kit**: Dependabot (`.github/dependabot.yml`: docker エコシステムが `core/`・`core/secrets-proxy/` の
  base image を tag+digest ごと、github-actions エコシステムが workflow 内の action バージョンを追従) が
  更新 PR を開く → CI (`.github/workflows/check.yml`: scaffold して `make check` / install.sh の冪等性 /
  dev・redact・secrets-proxy の実ビルド、の 3 jobs) が緑 → merge → release tag。
- **consumer**: installer 再実行 (`--ref` 省略で最新 release を取得)、または scaffold される
  `devcontainer-kit-update.yml` (毎週月曜 3:00 UTC の cron で installer を再実行し、差分があれば PR を
  作る。`GITHUB_TOKEN` で作った PR は他の workflow を trigger しない GitHub の仕様があるため、PR 上で
  CI を回したい場合は close/reopen するか PAT に差し替える)。

## 既知の ikeyan/tools 前提 (汎用化 TODO)

- `core/gh/hosts.yml` に GitHub user 名 `ikeyan` が直書きされている。
- `core/init-firewall.sh` の直結許可ドメインと `core/compose.yaml` の `NO_PROXY` に `ikeyan.github.io`
  が含まれる。
- `core/claude/` (`managed-settings.json` / `user-settings.json` / `local-mask.json`) は Claude Code
  固有の seed。他 agent CLI (Codex 等) への対応は今後。
- `core/redact/compose.yaml` の `name: tools-redact` — 複数 consumer で compose namespace が衝突する。
- `core/pyproject.toml` の `name = "tools"`。
