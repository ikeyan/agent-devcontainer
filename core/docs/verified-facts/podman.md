# rootless Podman

confidence tag の凡例: [README](README.md)。

## rootless Podman / newuidmap

- この環境 (Docker Desktop / LinuxKit VM, kernel `6.12.76-linuxkit`) では **setuid-root の
  `newuidmap`/`newgidmap` が setuid→cap 付与で実効 `CAP_SETUID`/`CAP_SETGID` を得られない**。
  結果、rootless Podman の userns セットアップが `newuidmap: write to uid_map failed: Operation
  not permitted` で失敗する。**カーネル自体は特権 uid_map write を許可している**: コンテナ内
  real root (`docker compose exec -u 0`、`CapEff` に cap_setuid) が `newuidmap` を介さず
  `printf '0 1000 1\n1 100000 65536\n' > /proc/<pid>/uid_map` を直接書くと**成功**する。
  失敗するのは「node が setuid バイナリ経由で cap を得る」経路だけ。切り分け根拠:
  非特権の単一自己マップ (自 euid への 1 行) は成功 / `newuidmap` の `/etc/subuid` policy も通過
  (範囲内なら policy OK→write で EPERM) / `unshare -rU` も成功 (userns 作成は seccomp=unconfined で通る)。
  → 対処は `newuidmap`/`newgidmap` に**明示 file capability** を付与 (`setcap cap_setuid+ep` /
  `cap_setgid+ep`) し **setuid ビットを外す** (`chmod u-s`)。file-cap は setuid 昇格と別系統で
  この環境でも実効になる (検証: setcap 後に node 実行の `newuidmap <pid> 0 1000 1 1 100000 65536`
  が rc=0、2 行マップ成立)。Dockerfile (dev ステージ) で焼き込む。`setcap` には `libcap2-bin`。
  `[empirical]`
- DAC_OVERRIDE は当初「setuid `newuidmap` が node 所有 uid_map を開くのに要る」として足したが、
  file-cap 化で `newuidmap` は **node のまま** (setuid 昇格しない) 動き node 所有ファイルを開けるため
  **不要**。検証: cap_add から DAC_OVERRIDE を外して rebuild → `podman info`/`podman unshare` が通る
  (`podman run <image>` は egress 許可リスト次第なので別途)。→ cap_add から除去済み。 `[empirical]`

## コンテナ (Podman / userns)

- dev コンテナの既定設定では **unprivileged user namespace の作成が弾かれる** (`unshare -Ur` が
  `Operation not permitted`)。一方 `sysctl user.max_user_namespaces` は非ゼロ (= kernel は許可) なので、
  これは **kernel ではなく container 層 (ランタイムの既定 seccomp profile)** の制限。rootless Podman を
  dev 内で動かすには seccomp 緩和が要る (本リポジトリは userns/mount 系 syscall だけ許可した tailored
  profile を採用。`seccomp=unconfined` でも動くが攻撃面が広い)。**追加 capability・`--privileged`・
  `/dev/fuse` は不要** (storage=overlay も kernel native なので fuse 不要。下記「rootless Podman / overlay」)。
  `/etc/subuid`/`/etc/subgid` には base image が `node:100000:65536` を設定済み。
  検証 (devcontainer, kernel 6.12 linuxkit): `unshare -Ur` = EPERM、`user.max_user_namespaces=31337`。
  詳細は `.devcontainer/core/PODMAN.md`。 `[empirical]`

## rootless Podman / overlay

- storage driver は **overlay** (graphroot = named volume podman = VM の ext4 上)。kernel 6.12 は
  userns 内の **native overlay** に対応し、`/dev/fuse` も fuse-overlayfs もカーネル特権も要らずに動く。
  CoW で layer の全コピーを避けるため vfs より速く容量も食わない (vfs は base+redact の実ビルドで
  graphroot を食い潰し disk full で落ちた)。overlay-on-overlay を避けるため graphroot は必ず実 fs
  (= volume) 上に置くのが要点。
  検証 (devcontainer, kernel 6.12 linuxkit, ext4): 使い捨て graphroot で
  `podman --storage-driver overlay run hello-world` 成功、`graphDriverName=overlay` /
  `Backing Filesystem: extfs` / `Native Overlay Diff: "true"` / `Using metacopy: "false"`。 `[empirical]`
- vfs → overlay の切替時は既存ストア (volume) のドライバ不一致を避けるため一度リセットが要る
  (`podman system reset -f`。ストアは pull 可能なキャッシュなので破棄可)。 `[empirical]`
- rootless podman は `XDG_RUNTIME_DIR` 未設定・`/run/user/$UID` 不在でも **storage/network 初期化が通る**
  (devcontainer には systemd/PAM ログインが無いので `/run/user/1000` は作られない)。podman 5.4.2 は runRoot
  を `/tmp/storage-run-$UID/containers` へ**警告なしでフォールバック**する (runtime state は本来 ephemeral
  なので /tmp で問題ない)。検証 (devcontainer, node uid=1000, XDG_RUNTIME_DIR unset, /run/user 不在):
  `podman info` = exit 0・stderr 無警告 (`runRoot=/tmp/storage-run-1000/containers`、overlay 各値は上記)。
  `podman run` も storage/network を初期化しレジストリ ping まで到達 (停止は registry allowlist の 403 で
  runtime dir 起因ではない)。 `[empirical]`

## rootless Podman / 共有 netns の resolv.conf に Google Public DNS が混ざる (`KeepHostServers`)

- ネスト build (slirp4netns) の `/etc/resolv.conf` に Google Public DNS (`8.8.8.8`/`8.8.4.4` と v6 版) が
  混ざる原因は **podman/containers-common に無条件でハードコードされた挙動**であり、CLI flag も
  `containers.conf` も関与しない。出典 (containers/common, `main` HEAD 時点):
  - `libnetwork/resolvconf/resolvconf.go` の `filterResolvDNS()`: netns 使用時、host の
    `/etc/resolv.conf` からループバック nameserver (`127.*`/`::1`) を除去した結果 nameserver が
    0 件になると、`defaultIPv4Dns = ["nameserver 8.8.8.8", "nameserver 8.8.4.4"]` (+ IPv6 有効なら
    `defaultIPv6Dns` の Google v6) を**黙って**追加する。
  - `libnetwork/internal/rootlessnetns/netns_linux.go` の `setupSlirp4netns()`/`setupPasta()` (rootless
    Podman が全コンテナ/build で共有する user-level netns の初期化。ここで slirp4netns の DNS 転送先
    (`10.0.2.3` 相当) を `resolvconf.New()` の `Nameservers` に渡すと同時に **`KeepHostServers: true` を
    無条件で渡す** → host 由来の (= 上記フォールバックで捏造された) nameserver も併記される。
  `[empirical]`
- `KeepHostServers` は `containers/common` 全体でこの 2 箇所以外に設定箇所が無く (`gh search code
  KeepHostServers --owner containers` で確認)、`containers.conf`/podman CLI から false にする経路は
  無い。`podman build --dns 10.0.2.3` を実際に試しても (devcontainer, podman 5.4.2) ネスト build の
  `/etc/resolv.conf` は変化しなかった (Google DNS 行が残ったまま) — 共有 rootless netns の
  resolv.conf は起動時に一度だけ作られ再利用されるため、build 単位の `--dns` はそこに効かない。
  `[empirical]`
- host 側 (`dev` コンテナ) の `/etc/resolv.conf` を非ループバック値にできれば `filterResolvDNS` の
  フォールバック自体は避けられるが、それでも解決しない: この devcontainer の `init-firewall.sh` は
  DNS 宛先を「`/etc/resolv.conf` に書かれた resolver 自身」+ loopback にしか許可しない allowlist
  firewall であり、`8.8.8.8` 含め任意の外部 resolver は元々到達不能という**設計上の制約**。podman 側の
  挙動を変えられたとしても、host の resolver をそのまま追加 nameserver として持ち込む限り「sandbox から
  到達不能な nameserver が resolv.conf に混ざる」こと自体は避けられない。 `[empirical]`
- 結論: podman/slirp4netns 側の設定でこれを止める手段は (2026-07 時点の containers/common /
  インストール済み podman 5.4.2 で) 存在しない。対処は呼び出し側 (`.devcontainer/core/Dockerfile` redact
  ステージ) で `uv pip install` 直前に `/etc/resolv.conf` を先頭の nameserver 1 行に削る
  (`docs/verified-facts/network.md`「musl (uv) resolver / 複数 nameserver」)。 `[empirical]`

## podman / short-name 解決 (registry 無し image 参照)

- registry を含まない image 参照 (例: `alpine:3`) は podman では **short-name reference** として扱われ、
  `short-name-aliases.conf` に alias が無ければ「どの registry から pull するか」をユーザーに**対話プロンプト**で
  尋ねる。出典: podman-pull(1) の DESCRIPTION と FILES (short-name-aliases.conf)。 `[man]`
- つまり非対話 (スクリプト/CI) 実行では alias 未定義の short-name は失敗し得る。ホスト側で走る
  スクリプト (`core/bin/migrate-volumes.sh` の podman fallback) は `docker.io/library/...` に完全修飾する。
  (PR #13 Codex review 指摘 P2 由来。)

## podman build / SHELL (OCI vs docker フォーマット)

- podman/buildah は **OCI 出力フォーマット (既定) だと Dockerfile の `SHELL` を無視**する。redact 実ビルド
  (`make check-net`) を podman で回した際に観測。 `[empirical]`
- 原因: OCI image-spec の config object に `Shell` フィールドが無く焼き込めない (docker image config 拡張
  にはある)。出典: opencontainers/image-spec `config.md` の config プロパティ一覧 (User / Env / Entrypoint /
  Cmd / Volumes / WorkingDir / Labels / StopSignal / Healthcheck … に Shell は無い)。 `[docs]`
- 対処: `SHELL ["/bin/bash","-o","pipefail","-c"]` を効かせて sha256 検証 pipe (`… | sha256sum -c -`) の
  pipefail を honor させるには `podman build --format docker` が要る。docker engine は `--format` を
  取らないので podman のときだけ付ける (`.devcontainer/core/Makefile` の `BUILD_FORMAT`)。
