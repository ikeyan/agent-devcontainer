# agent-devcontainer

agent CLI (Claude Code 等) を封じ込め付きで動かす devcontainer キット。
dev コンテナ + egress firewall (iptables/ipset) + secrets-proxy (平文 credential を dev に置かない) +
redact サンドボックス + 検証群 (`make check`) を、1 コマンドで workspace (`.devcontainer/` を置き、直下に 1 つ以上の
リポジトリを並べるディレクトリ。単一リポジトリの root でもよい) へ導入・更新する。
アーキテクチャ詳細は [core/README.md](core/README.md)、redact の脅威モデルは
[core/redact/SECURITY-MODEL.md](core/redact/SECURITY-MODEL.md)、外部ツールの確定仕様は
canon (`ikeyan/canon` の `facts/`)。

## インストール / 更新

```shell
curl -fsSL https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh | bash
```

**workspace root** (= `.devcontainer/` を置くディレクトリ。コンテナ内では `/workspace` になる) で実行する。単一リポジトリなら
その root、複数リポジトリを 1 つの devcontainer で扱うならそれらを並べる親ディレクトリ (git 管理推奨。後述「workspace と
複数リポジトリ」)。再実行 = 更新 (冪等)。`core/` は毎回全置換、`project/` ほか scaffold 済みファイルは二度と触らない。

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

(`gh api .../tarball/<ref>` が展開する top dir は `<owner>-<repo>-<short-sha>` (canon: facts/github-tarball/rest-api-tarball-topdir-naming) — install.sh 自身が使う `/archive/<ref>.tar.gz` 直叩き (canon: facts/github-tarball/archive-tar-gz-topdir-naming) とは生成規則が別物)

**要件**: `bash` / `curl` / `tar`。git は installer 自体には不要だが、workspace root を git 管理して取り込み内容を
`git diff` でレビューしてから commit する運用を前提にしている (`clone-repos` は git を使う)。devcontainer の実行には docker または podman
(+ compose plugin) が要る。

実行し終えると次の手順が標準出力に案内される (`--dir` 既定の `.devcontainer` の場合):

```
done. 次の手順:
  1. git diff で取り込み内容をレビューして commit
  2. project/ を編集 (allow-domains.txt / .env / rules.yaml / repos.txt / Dockerfile.{top,dev})。
     Dockerfile fragment 変更後: make -C .devcontainer/core gen-dockerfile
  3. 検証: make -C .devcontainer/core check
  4. secrets-proxy 初回認証: make -C .devcontainer/core bootstrap
  5. workspace 直下へ repo を clone (project/repos.txt の宣言): make -C .devcontainer/core clone-repos
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
次の root glue を scaffold する: `CLAUDE.md` (workspace レイアウトの説明。編集可),
`.github/workflows/devcontainer-check.yml`, `.github/workflows/devcontainer-kit-update.yml`,
`.github/dependabot.yml`, `.claude/settings.json` (`CLAUDE.md`/workflows は既存なら黙ってスキップ、
`dependabot.yml`/`.claude/settings.json` は既存なら取り込み候補を stdout に表示する)。更新実行 (core が既にある) は glue を一切触らないので、不要な glue は削除すれば
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
  `make -C .devcontainer/core check-rules` で検証する。template は `github.com` を `passthrough_hosts` で
  素通しする (コンテナ内の git clone/push 用)。
- **`project/repos.txt`** — workspace 直下に置く repo の宣言 (1 行 1 repo: `<owner>/<repo> [<dir>]` または
  `<git URL> <dir>`)。`make -C .devcontainer/core clone-repos` が揃える (詳細は下記「workspace と複数リポジトリ」)。
- **`project/compose.yaml`** — project 固有の compose 層。追加 volume / build args / 環境変数は
  `services.dev` 以下に足す。`name:` (compose project 名) は scaffold 時にカレントディレクトリ名から
  自動設定される。
- **`project/.env`** — compose の変数補間用 (`PROJECT_NO_PROXY`, secrets-proxy bootstrap 用の
  `PROJECT_BW_SERVER`, gh seed 用の `PROJECT_GH_USER`)。原則 dev コンテナのプロセス環境には渡らない —
  例外は `PROJECT_GH_USER` で、rebuild 漏れ検査 (init-firewall.sh) のため environment 経由でも渡る
  (GitHub user 名であり秘密ではない。秘密はそもそも .env に置かない設計)。
  `PROJECT_GH_USER` (認証に使う GitHub user 名) は必須 — 未設定・不正だと dev イメージ build 時に
  Dockerfile の ARG assert が fail-closed に落とす (compose 補間は `:-` なので非 dev の compose 操作は通す)。

## workspace と複数リポジトリ

installer を実行したディレクトリ (= `.devcontainer/` の親) を **workspace root** と呼ぶ。dev コンテナはこれを
`/workspace` に bind するので、直下に置いた git repo はすべてコンテナから見え、逆にコンテナ内で `/workspace`
直下に clone した repo はホストの workspace root にそのまま現れる (VS Code の Explorer / Source Control にも出る —
VS Code の git 拡張は開いたフォルダ直下 1 階層の repo を既定で検出する: `git.autoRepositoryDetection: true`,
`git.repositoryScanMaxDepth: 1`)。

```
<workspace root>/            ← コンテナ内では /workspace。git 管理推奨 (kit 設定を版管理し、kit-update PR と CI を受ける)
├── .devcontainer/           ← kit (core/ + project/ + devcontainer.json + Dockerfile)
├── .claude/settings.json    ← 初回 scaffold (SessionStart で make check)
├── CLAUDE.md                ← 初回 scaffold (このレイアウトの説明。編集可)
├── repo-a/                  ← project/repos.txt に宣言 → make -C .devcontainer/core clone-repos
└── repo-b/
```

- **宣言と clone**: 直下に置く repo は `.devcontainer/project/repos.txt` に宣言し (`<owner>/<repo> [<dir>]` または
  `<git URL> <dir>`。形式と封じ込めは `core/bin/workspace-repos` 冒頭)、`make -C .devcontainer/core clone-repos` で
  揃える。冪等で、clone 済みは origin が宣言と一致することだけ検査する (不一致・非 git dir・symlink は止める。URL は
  `.git` 接尾辞と末尾 `/` 以外そのまま比較するので、宣言は origin と同じ形 — transport・login・port を含めて — で書く)。
  host でもコンテナ内でも同じ dir に clone される。`make -C .devcontainer/core check` (`check-repos`) は宣言の
  構文とパス封じ込め、clone 済み repo の origin 整合を検査する — 未 clone は正常 (CI は workspace repo 単体を checkout する)。
- **workspace root を git 管理する場合**: clone した repo は workspace repo の追跡対象にしない。`clone-repos` が
  `.git/info/exclude` に `/<dir>/` を登録するので `git status` は汚れない (手で clone した repo は宣言してから
  `clone-repos` を再実行すれば同じく登録される)。
- **認証**: コンテナ内の git は `github.com` を secrets-proxy の `passthrough_hosts` (template の `project/rules.yaml`
  に同梱) で素通しする。private repo の clone/push に要る資格情報はコンテナ内に置かず、VS Code Dev Containers の
  credential helper / ssh-agent 転送 (host に credential manager があれば追加設定なし) に任せる。`gh` は
  `api.github.com` を proxy が substitute する既存経路のまま。
- **claude をどこで起動するか** (Claude Code の解決規則: project 設定 `.claude/settings.json` は起動ディレクトリのもの、
  `CLAUDE.md` は起動ディレクトリとその祖先を起動時に、配下は参照時に遅延ロード):
  - `/workspace` で起動 — 全 repo を横断して扱える。各 repo の `CLAUDE.md` はその repo のファイルを読んだ時に
    ロードされ、各 repo の `.claude/settings.json` (hooks / plugins) は読まれない。
  - `/workspace/<repo>` で起動 — その repo の設定・hooks が効き、親 `/workspace/CLAUDE.md` も読み込まれる。他 repo は
    `--add-dir ../<other>` で対象に足す (managed の `bypassPermissions` 下では権限プロンプトは元々出ない)。
- **bind 配下の named volume**: `/workspace/<repo>/node_modules` のように bind mount 配下へ named volume を当てるなら、
  image 側に同じパスを node 所有で作っておく (`project/Dockerfile.dev`)。無いと volume が root 所有で初期化され node が
  書けない (`core/README.md`「初回セットアップ」)。

### 単一リポジトリ運用から workspace へ移す

既存 consumer (`<repo>/.devcontainer`) はそのまま動く (workspace root = repo root の特殊形)。複数 repo を 1 つの
devcontainer で扱うときは、新しい workspace ディレクトリを作って installer を実行し、旧 `project/` を移す:

1. `mkdir <ws> && cd <ws>` (git 管理するなら `git init`) → installer を実行。
2. 旧 `<repo>/.devcontainer/project/` の `.env` / `rules.yaml` / `allow-domains.txt` / `Dockerfile.*` を
   `<ws>/.devcontainer/project/` へ写す。`compose.yaml` の `name:` を旧値にすると named volume (`<name>_proxy-bw` の
   BW session、`<name>_claude-config` のログイン等) をそのまま引き継げる (volume は compose project 名で namespace
   される: canon `facts/docker/compose-named-volume-project-scope`)。ただし旧 devcontainer と同時には起動しない
   (同名 project の stack が衝突する)。
3. repo 直下を指していた mount (`node-modules:/workspace/node_modules` 等) は `/workspace/<repo>/...` に付け替え、
   mount 先を `project/Dockerfile.dev` で node 所有に作る (上記)。
4. `project/repos.txt` に repo を宣言 → `make -C .devcontainer/core clone-repos`。
5. 旧 `<repo>/.devcontainer` は残してもよい (単体でも開ける) が kit-update の PR が両方に来る。不要なら削除し、
   その repo の CI / Makefile が `.devcontainer/core` を参照していれば併せて直す。

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

### 更新に伴う既知の移行 (既存 consumer 向け)

installer は consumer 所有の `project/` を触らないため、core が新しい必須 project 変数を要求する
更新では、既存 consumer は次の compose 呼び出し/rebuild で fail-closed に止まる (エラーメッセージが
設定すべき変数を指す)。現時点の移行点:

- **`PROJECT_GH_USER`** (gh seed の GitHub user 名) を `project/.env` に設定する。build 時に image へ
  焼き込むため、設定・変更の反映には dev イメージの rebuild が要る (未設定なら dev イメージ build 時に
  エラー文言で設定+rebuild を要求する)。設定「変更」後の rebuild 漏れは postStartCommand (init-firewall.sh) の
  整合検査が起動時に止める。**kit 更新直後に旧イメージのまま再作成した場合だけは検査が旧版のため
  検出されない** (VS Code は compose 宣言の entrypoint を使わないため、ホスト側から強制する手段が
  無い — canon: facts/devcontainer/override-command-discards-compose-entrypoint)。更新後は必ず rebuild すること。
- **consumer 固有の直結ドメイン** (旧 core 直書き分、例: `ikeyan.github.io`) は core の許可リストから
  外れた。必要なら `project/allow-domains.txt` と `PROJECT_NO_PROXY` の対に追加して rebuild する
  (こちらは config エラーにならず、実行時の接続失敗として現れる点に注意)。
- **`project/repos.txt`** (workspace 直下に置く repo の宣言) が無いと `make check` の `check-repos` が止める。
  installer が欠落分を補完する (kit-update の PR に乗る。宣言 0 件のままでよい)。
- **`rules.yaml` の `passthrough_hosts: [github.com]`** は template にだけ入る。既存 consumer がコンテナ内で
  `clone-repos` (git over https) を使うなら自分の `project/rules.yaml` に足す (無いと secrets-proxy が 403 で止め、
  `clone-repos` がその旨を hint する)。

## 既知の汎用化 TODO

- `core/claude/` (`managed-settings.json` / `user-settings.json` / `local-mask.json`) は Claude Code
  固有の seed。他 agent CLI (Codex 等) への対応は今後。

consumer 固有値 (gh user / 直結ドメイン / compose project 名) は project 層へ抽出済みで、再混入は
`make check-contamination` が防ぐ (kit issue #10)。
