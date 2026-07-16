# devcontainers CLI — compose の -f 渡しと project 名 fallback

confidence tag の凡例: [README](README.md)。

このリポジトリは `devcontainer.json` の `dockerComposeFile` に
`["project/compose.yaml", "core/compose.yaml"]` を並べる 2 層 compose 構成。core/compose.yaml 内の
相対ホストパス (bind source / build context / seccomp ファイル) がどのディレクトリ基準で解決されるかは、
devcontainers CLI が compose をどう起動するかで決まる。以下は一次情報 (CLI ソース) で確定した契約。

## `dockerComposeFile` の各要素は列挙順で `-f` に渡る / `--project-directory` は付けない

- CLI は `dockerComposeFile` 配列の各要素をそのまま順番に `-f` へ展開する。順序は保存され、間に
  `--project-directory` は挿入されない。よって compose の project directory は **compose 側の既定 =
  最初の `-f` のディレクトリ**になる (このリポジトリでは `.devcontainer/project/`)。
  ```ts
  // startContainer / readDockerComposeConfig 双方に同一パターン:
  const composeGlobalArgs = ([] as string[]).concat(
      ...composeFiles.map(composeFile => ['-f', composeFile]),
  );
  ```
  `composeGlobalArgs` は `docker compose` 呼び出し (config / up / build) の共通引数として使われ、
  この中に `--project-directory` は現れない。 `[docs(source)]`

## project 名の fallback は `basename(dirname(composeFiles[0]))`

- `getProjectName()` は次の優先順で project 名を決める。明示指定が無いときの最終 fallback が
  **最初の compose ファイルのあるディレクトリの basename**である:
  1. 環境変数 `COMPOSE_PROJECT_NAME`
  2. `.env` の `^COMPOSE_PROJECT_NAME=(.+)$`
  3. compose config の `name:` フィールド
  4. workspace 構成フォルダ名 (`<basename>_devcontainer` 由来)
  5. fallback:
     ```ts
     const workingDir = composeFiles[0] ? cliHost.path.dirname(composeFiles[0]) : cliHost.cwd;
     // ... toProjectName(cliHost.path.basename(workingDir), ...)
     ```
     出典コメントは docker の `compose/config/config.py` の同ロジックを参照している。 `[docs(source)]`
- 含意: このリポジトリは `project/compose.yaml` の `name: tools` (上記 3) を明示宣言しているので
  project 名は `tools` に固定される。もし `name:` を消すと fallback (上記 5) が働き、最初の `-f` =
  `project/compose.yaml` のディレクトリ basename = `project` が project 名になり、全 named volume が
  `project_<vol>` の別 namespace に化けて既存データから切り離される (この不変条件は
  `bin/redact_invariants_check.sh` §10 が pin)。

## `overrideCommand: true` は compose 宣言の entrypoint/command を捨てる

- CLI は起動時に生成 override ファイルを **-f 連鎖の最後**に足し、そこで entrypoint を自前の
  keep-alive (`/bin/sh -c 'echo Container started ...'`) に置換する。compose ファイル側で宣言した
  entrypoint/command は `overrideCommand: true` だと引き継がれない:
  `const userEntrypoint = overrideCommand ? [] : composeEntrypoint` (`dockerCompose.ts`)。
  → **compose 宣言の entrypoint を devcontainer 起動時の検査 gate に使うことはできない**
  (last-file-wins で丸ごと差し替わる)。`overrideCommand: false` でだけ compose 宣言が生き残る。 `[docs(source)]`
- 補足 (compose 一般): service に `entrypoint:` を宣言すると image の CMD は無効化される
  (compose spec: entrypoint declared → image の default command は使われない)。entrypoint 内で
  `exec "$0" "$@"` しても image CMD は届かない。検証: image CMD=`[sh]` に compose で
  entrypoint を宣言して create → container の `Config.Cmd` が `[]` (podman 5.4.2 +
  compose v5.3.1)。 `[docs][empirical]`

## `updateRemoteUserUID` は home を丸ごと chown -R する

- Linux ホスト (既定 on。macOS は `updateRemoteUserUIDOnMacOS` 指定時のみ) で remoteUser が
  非 root・非数値のとき、CLI は `updateUID.Dockerfile` で派生イメージ (`<image>-uid`) を作る。
  ホスト uid/gid が image の値 (このリポジトリでは node=1000) と異なると
  `chown -R $NEW_UID:$NEW_GID $HOME_FOLDER` が走る (`scripts/updateUID.Dockerfile`)。
  → **イメージ内で root 所有にした home 配下のファイル (gh seed 等) は uid≠1000 の Linux ホスト
  では node 所有へ戻る**。所有者を前提にした防御・検査はそのホスト群では成立しない。 `[docs(source)]`

## 出典

- devcontainers/cli, `src/spec-node/dockerCompose.ts` (branch `main`)。
  `https://raw.githubusercontent.com/devcontainers/cli/main/src/spec-node/dockerCompose.ts`
  を取得して該当箇所 (`composeGlobalArgs` の生成、`getProjectName` の fallback) を確認 (2026-07-07)。
- 同 repo `src/spec-node/dockerCompose.ts` (`userEntrypoint`/`overrideCommand`)、
  `src/spec-node/containerFeatures.ts` (`getRemoteUserUIDUpdateDetails` の発動条件)、
  `scripts/updateUID.Dockerfile` (`chown -R`) を取得して確認 (2026-07-17)。
- 上記に対応する本リポジトリの帰結・実測 (project directory = 最初の `-f` のディレクトリ、`.env` の
  読まれ方、後勝ちマージ) は `docker.md`「compose 複数 -f / project directory / .env」。ホスト側パスの
  実在検証は `make check-compose-paths` (`bin/check_compose_paths.py`)。 `[docs(source)]`
