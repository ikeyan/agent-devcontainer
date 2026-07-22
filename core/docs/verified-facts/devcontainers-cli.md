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
- 含意: この kit は `project/compose.yaml` の `name:` (installer が consumer ごとの値へ置換) を
  明示宣言するので project 名はその値 (上記 3) に固定される。もし `name:` を消すと fallback
  (上記 5) が働き、最初の `-f` = `project/compose.yaml` のディレクトリ basename = `project` が
  project 名になり、全 named volume が `project_<vol>` の別 namespace に化けて既存データから
  切り離される (この不変条件は `bin/redact_invariants_check.sh` §10 が pin)。

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

## postStartCommand は起動時 env (compose `environment:`) を継承する

- lifecycle command (postStartCommand 等) は稼働中コンテナへの `docker exec` で走る。`docker exec` は
  コンテナ起動時 env (compose `environment:` が入れた Config.Env) を継承するので、compose の
  `environment: FOO: ...` は postStart の shell で `$FOO` として読める。init-firewall.sh の gh seed
  整合検査 (`$PROJECT_GH_USER` を sudo 経由で読む) はこの継承に依存する。
  検証: `compose config dev` に PROJECT_GH_USER が environment として現れ (build.args と 2 箇所)、
  `podman run -e PROBE=x … && podman exec … printenv PROBE` が値を返す (exec の env 継承)。
  sudo の env_reset を env_keep が貫通する最終 hop は core/Dockerfile の build 時 probe が実測。
  チェーン全体 (compose → Config.Env → CLI postStart docker exec → sudo env_keep → reader) の
  end-to-end pin は devcontainers CLI 実行を要するため issue #25 (check-devcontainer-up) が担う。 `[empirical]`

## compose 経路の既存コンテナ探索は project+service ラベルだけ (idLabel 非依存)

- compose 構成 (`dockerComposeFile` 指定) の `devcontainer up` は `openDockerComposeDevContainer`
  経由で、既存コンテナを `findComposeContainer(params, projectName, service)` で探す。これは
  ```ts
  listContainers(params, true, [
      `com.docker.compose.project=${projectName}`,
      `com.docker.compose.service=${serviceName}`,
  ])
  ```
  の 2 ラベルだけで照合し、`devcontainer.local_folder` / `devcontainer.config_file` (idLabel) は
  **探索に使わない** (idLabel は `startContainer` が新規コンテナへ *付与* するだけ)。よって一意な
  `COMPOSE_PROJECT_NAME` を与えれば同一 workspace/config の既存 devcontainer は project ラベル不一致で
  ヒットせず、CLI は fresh な `compose up` を新しい project 名前空間で建てる。`--id-label` は
  非 compose (image/Dockerfile) 経路の `findDevContainer` 探索にだけ効き、compose 経路の
  `findComposeContainer` には影響しない。 `[docs(source)]`

## `devcontainer up` が建てる image は 2 系統 (project 冠 compose / cwd 由来 folder) — cleanup 設計の要石

- **compose 系**: service に `image:` が無い (このリポジトリの `dev` / `secrets-proxy` は `build:` のみ) と、
  compose が建てる image 名は `getDefaultImageName = ${projectName}${sep}${service}` (`sep` は compose
  >=2.8 で `-`、未満で `_`)。→ 一意 project 名で up する度に `<project>-dev` / `<project>-secrets-proxy`
  が別 image として溜まる。 `[docs(source)]`
- **folder 系**: CLI が UID 調整 (`updateRemoteUserUID`) で建てる image は
  `getFolderImageName = vsc-${basename(cwd)}-${sha256(cwd)}` を基に `${folderImageName}-uid`
  (`containerFeatures.ts:435`; compose 経路は `imageName.startsWith(folderImageName)` が偽なので
  `vsc-…-uid`)。**cwd から決まる**ので project 名では隔離できず、`--workspace-folder` が同じなら実
  devcontainer と同名になる。→ probe が同じ folder で up すると `up` 中に user の共有
  `vsc-<folder>-<hash>-uid` を retag する副作用が起き、project scope の cleanup では防げない。 `[docs(source)]`
  - 注: `${folderImageName}-features` (features 拡張 image) が出るのは**単一コンテナ経路のみ**
    (`extendImage`, `containerFeatures.ts:59`)。compose 経路は `build:` service だと `overrideImageName`
    が undefined のまま (`dockerCompose.ts:197-202`) で `-features` image は作られない。 `[docs(source)]`
- **含意 (smoke `check_devcontainer_up.sh` の隔離設計)**: probe を **一意 basename (`dcup-smoke-<pid>`) の
  temp workspace へ複製**して up する。folder 系も `vsc-dcup-smoke-<pid>-<hash>[-uid]` になり、compose 系
  `dcup-smoke-<pid>-<svc>` と共に全 image が `dcup-smoke-<pid>` token を持つ = user の `vsc-<realfolder>-*`
  とは別名 (retag も衝突も無い)。cleanup はこの token を持つ資源だけを消せば完結する。 `[docs(source)]`
- `docker images --filter reference=<pat>` は `repository:tag` を glob 照合し `*` を許す
  (docs 例 `busy*:*libc` が `busybox:uclibc` に一致)。tag 省略時は該当 repo の全 tag に一致。複数
  `--filter reference=` は OR。ただし `reference=<proj>*` は右端 unanchored で `<proj>` が別 PID の prefix
  (`dcup-smoke-700*` が `dcup-smoke-7005-dev` に一致) だと取り違えるので、separator を anchor した
  `reference=<proj>-*` / `<proj>_*` / `vsc-<proj>-*` で消す。 `[docs]`

## 出典

- devcontainers/cli, `src/spec-node/dockerCompose.ts` (branch `main`)。
  `https://raw.githubusercontent.com/devcontainers/cli/main/src/spec-node/dockerCompose.ts`
  を取得して該当箇所 (`composeGlobalArgs` の生成、`getProjectName` の fallback) を確認 (2026-07-07)。
- 同 repo `src/spec-node/dockerCompose.ts` (`userEntrypoint`/`overrideCommand`)、
  `src/spec-node/containerFeatures.ts` (`getRemoteUserUIDUpdateDetails` の発動条件)、
  `scripts/updateUID.Dockerfile` (`chown -R`) を取得して確認 (2026-07-17)。
- 同 repo v0.88.0 を clone し `src/spec-node/dockerCompose.ts`
  (`openDockerComposeDevContainer`/`findComposeContainer`/`getDefaultImageName`)、
  `src/spec-node/containerFeatures.ts` (`updatedImageName`/`fixedImageName` = folder image 由来)、
  `src/spec-node/utils.ts` (`getFolderImageName`) を確認 (2026-07-22)。
- `docker image ls` の `--filter reference=` の glob 照合は Docker docs
  (`https://docs.docker.com/reference/cli/docker/image/ls/`) の例 `busy*:*libc` で確認 (2026-07-22)。
- 上記に対応する本リポジトリの帰結・実測 (project directory = 最初の `-f` のディレクトリ、`.env` の
  読まれ方、後勝ちマージ) は `docker.md`「compose 複数 -f / project directory / .env」。ホスト側パスの
  実在検証は `make check-compose-paths` (`bin/check_compose_paths.py`)。 `[docs(source)]`
