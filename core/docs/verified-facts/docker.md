# Docker / Docker compose / Docker Desktop

confidence tag の凡例: [README](README.md)。

## Docker compose

- top-level `name:` / `external:` の無い named volume は **project スコープ** (`<project>_<vol>`) に
  なり、リテラル名 (例 `agent-devcontainer-redact-auth`) と一致しない。固定するなら `name:` を付ける。
  `[docs][review]`
- `security_opt: seccomp=<path>` の `<path>` は **project ディレクトリ基準** (= compose ファイルの
  あるディレクトリ。`-f` 指定時はその親) で解決され、compose が**ファイル本文を読んで inline 展開**
  してから engine に渡す (`unconfined`/`builtin` はそのまま透過)。moby 拡張の `includes`/`excludes`/`args`
  は **engine (moby daemon)** がコンテナの caps/arch で解決して OCI seccomp config に落とす (OCI runtime
  自体は解さない) ので、compose 経由の custom profile でも拡張が効く。
  出典: docker/compose `pkg/compose/create.go` `parseSecurityOpts` (`os.ReadFile(p.RelativePath(con[1]))`)。
  本リポジトリでは `context: ..` (compose ファイルは `.devcontainer/core/compose.yaml`) が `.devcontainer/`
  基準でビルドできている事実とも整合する。 `[docs(source)][empirical]`
- 上記「caps で解決」を実機確認: dev は `cap_add` に CAP_SYS_CHROOT を持たないため、custom profile 中の
  `chroot` group (`includes: {caps:[CAP_SYS_CHROOT]}`, action ALLOW) は engine の解決で**落ち**、`chroot`
  が defaultAction(ERRNO) になる。`podman unshare`(userns 内で CAP_SYS_CHROOT を満たす)から `chroot()`
  を叩いても EPERM = cap でなく seccomp が原因と確定 (直接実行も EPERM だがそれは cap チェック由来)。
  実害: `podman build` の COPY (buildah copier が context へ chroot) が `operation not permitted` で失敗。
  対処: `gen-seccomp.ts` が chroot group の `includes` を外して常時許可 (実権限は kernel の cap チェック =
  userns 内のみ有効なので同 uid プロセスは依然 chroot 不可)。 `[empirical]`
- **`docker compose -f a.yml -f b.yml config` のマージは各ファイルを個別に canonical 形へ正規化してから
  合成する** (先に生の YAML 同士をマージしてから正規化、ではない)。よって base 側 service が
  `networks:` を短縮形 (list, 例 `- devnet`) で書き、override 側が同じキーを長縮形 (mapping, 例
  `devnet: {}` / `proxy-admin: {ipv4_address: ...}`) で書いても、両者とも合成前に長縮形へ正規化
  済みなのでキー単位の mapping マージ (欠けているキーを追加、重複キーは値をマージ) が効き、
  型不一致エラーにはならない。`.devcontainer/core/compose.mitmweb.yaml` が base の list 形 `devnet`/
  `proxy-upstream` に `proxy-admin: {ipv4_address: ...}` を mapping 形で足す override はこの挙動が前提。
  出典: compose-go `loader/loader.go` の `Load()` — 各 `ConfigFile` ごとに `parseConfig` → `schema.Validate`
  → `loadSections` (`transformServiceNetworkMap` 等の canonical 変換を含む) を通してから、全ファイル分
  集まった後で `merge(configs)` を呼ぶ順序。マージ規則自体 (mapping は欠落キー追加+衝突キーはマージ、
  sequence は追記) は compose-spec `13-merge.md`。Docker 公式の networking ガイドも
  「base が `networks: - frontend`(list) の時、override は `networks: { frontend: { ipv4_address: ... } }`
  (mapping) を書く」構成を明示している (`docs.docker.com/compose/how-tos/networking/` の static IP 例)。
  compose エンジン (docker/podman compose) を実行して `config` の実合成結果までは本セッションでは
  未検証 — 一次情報 (ソース順序 + 公式ドキュメント例) からの結論であり、実機確認は別途要る。 `[docs(source)]`

## compose 複数 -f / project directory / .env

- **devcontainer 環境の compose エンジンは `podman compose`** (`podman compose version` が
  `>>>> Executing external compose provider "/usr/local/lib/docker/cli-plugins/docker-compose" <<<<`
  と表示する通り、provider の実体は docker compose plugin v5.2.0)。`docker` バイナリ自体はこの環境に
  無い。以降の検証はすべて `podman compose` で実施した (`docker compose` 相当の挙動として扱ってよいのは
  provider が同一バイナリのため)。 `[empirical]`
- **複数 `-f` は後勝ちでスカラーを上書きする**。`-f p.yaml -f c.yaml` で両方が同じ `services.a.image` を
  持つとき、`config` の結果は `c.yaml` (後方) の値になる。
  検証: `p.yaml` に `image: "busybox:${PROBE_TAG:-none}"`、`c.yaml` に `image: busybox:override` を置き
  `podman compose -f p.yaml -f c.yaml config` → `image: busybox:override`。 `[empirical]`
- **project directory の既定は最初の `-f` のディレクトリ**であり、`.env` はそこから読まれて変数補間に
  使われる。検証: 上記 `p.yaml` と同じ dir に `.env` (`PROBE_TAG=fromenv`) を置き、`c.yaml` 側の
  `image` 上書きを外す (`services.a: {}` のみ) と `config` の結果は `image: busybox:fromenv` になった
  (= `p.yaml` の interpolation が `p.yaml` の dir の `.env` から解決された)。`c.yaml` は別 dir に置いても
  `.env` 探索には影響しない。 `[empirical]`
- **project directory の決定と `.env` 読込は cwd に依存しない**。上と同じ `-f p.yaml -f c.yaml` 呼び出しを
  `cd /` してから実行しても同じ `image: busybox:fromenv` になった。 `[empirical]`
- **後のファイルに `name:` が無ければ先の `name:` がそのまま残る**。`p.yaml` に `name: probe` を書き
  `c.yaml` に `name:` を書かない場合、上記いずれの `config` 出力も `name: probe` を保持した (mapping
  マージで欠けているトップレベルキーは前方から引き継がれる。上の「`docker compose -f a.yml -f b.yml
  config` のマージ」の canonical 化 + merge の一般則と整合)。 `[empirical]`
- 上記 4 点により、本リポジトリで `-f ../project/compose.yaml -f compose.yaml` の順に固定すると:
  `project/compose.yaml` のディレクトリ (`.devcontainer/project/`) が project directory になり
  `project/.env` が補間に効く。`project/compose.yaml` の `name: tools` は `core/compose.yaml` に
  `name:` を書かない限り生き残る。
- **「後勝ちで core 優先」が効くのは、core が同じキーを明示宣言したスカラーに限る**。マージ規則は
  mapping=欠落キー追加+衝突キーはマージ / sequence=追記 (compose-spec `13-merge.md`) なので、(a) 追加系
  (`cap_add`/`volumes`/`networks` 等の list) は project 値が**追記されて残り**、(b) core が宣言していない
  キーは project 値が**そのまま通る**。つまり「project の編集で core の安全設定を弱められない」保証は、
  core が実際に上書き宣言しているスカラー (例: `security_opt: [systempaths=unconfined]` を core が持つ等)
  についてだけ成り立ち、list/未宣言キーには**及ばない**。そこで dev の cap 集合・privileged 不在・
  network 所属・secrets-proxy 専用 volume の非漏洩・project の `name:` 宣言といった「マージ結果そのもの」の
  セキュリティ姿勢は、後勝ちの一般則に頼らず `bin/redact_invariants_check.sh` §10 がマージ済み config を
  PyYAML で parse して assert し pin する。 `[empirical]`
- **先頭 `-f` が `name:` と `services: {<svc>: {}}` (空 mapping) だけの最小ファイルでも、`compose config` は
  valid で後続ファイルと正しく merge される**。kit 化した `project/compose.yaml` テンプレート (プレースホルダ
  scaffold 直後、project 固有の上乗せが何も無い状態) はまさにこの形なので、これが壊れると
  「project 層を汎用化した瞬間に compose が死ぬ」という kit 最大のリグレッションになる。
  検証: `podman compose -f project/compose.yaml -f core/compose.yaml config` (project/compose.yaml は
  `name: "kitlocal"` と `services: { dev: {} }` のみ) → exit 0、出力の `name:` は `kitlocal`、
  `services.dev` は core/compose.yaml が宣言する内容 (build/cap_add/volumes/networks 等) がそのまま
  現れる (= 空の `dev: {}` は「このキーは存在するが値を追加しない」という merge の単位オブジェクトとして
  働き、後続ファイルの `dev:` 定義を丸ごと採用する。mapping merge の一般則と整合)。 `[empirical]`
  この形は `make -C core check` の `check-compose` / `check-compose-paths` が毎回 `-f project/compose.yaml
  -f compose.yaml config` を実行するため、kit ローカル検証パイプライン (Step 7: scaffold → gen →
  `make -C core check`) が壊れれば即座に検出できる (常時 pin)。

## internal network の DNS

- `internal: true` の network 上のコンテナは、埋め込み DNS (`127.0.0.11`) で **コンテナ/サービス名は
  解決できる** が、**外部名は転送されず `SERVFAIL`** を返す (通常 bridge net では同じ名前が解決できる)。
  resolv.conf 側には `ExtServers: [host(8.8.8.8)]` が載るが、internal net では実際には転送しない。
  → redact を proxy 中継 + `internal: true` net へ移すと、**internal net 自体が DNS exfil 経路を塞ぐ**
  ので、子に `iptables`/`dnsmasq` (NET_ADMIN) を持たせずに DNS 持ち出しを断てる。子は proxy を
  サービス名で引けるまま (proxy だけが外向き net で upstream を解決する)。 `[empirical]`
  - 確認 (docker 29.3.1, iptables firewall backend):
    `docker run --rm --network <internal> busybox nslookup example.com` → SERVFAIL、
    同 net で peer コンテナ名 → 解決、通常 net で `example.com` → 解決。
  - 回帰 pin: `redact/dns_egress_test.sh` (`make -C .devcontainer/core check-redact-dns-egress`;
    kit CI が毎 PR で runner 同梱の docker に対し実行 = 版は runner image 依存)。二重コントロール (通常 net で外部名が解決する
    有意性 + internal net で peer 名が解決する機能性) が成立した時だけ「internal net で外部名が
    解決しない」を assert する fail-closed 設計。転送が復活したら FAIL。
  - podman は DNS 実装が別物 (aardvark-dns) のためこの pin の対象外。internal net 設計を podman 実行に
    載せる場合は別途実機 pin が要る。
  - 罠 (テスト実装上): host の resolv.conf に search domain があると (例: kit CI の Azure runner
    `*.bx.internal.cloudapp.net`) docker が container へ継承させ、busybox nslookup は `ndots:0` でも
    bare の container 名にそれを付けて引き `<name>.<search>` が SERVFAIL になる (peer 名が引けず
    control B が空振り)。`docker run --dns-search=.` で継承 search を消すと bare 名を absolute で
    引いて解決する。負プローブ側でも search 付きは外部名を search domain 経由で解決して転送有無を
    覆い隠しうるので、`--dns-search=.` は健全性・本命の両方に効く。 `[empirical]` (kit CI docker 28.0.4)

## Docker /proc マスク (systempaths)

- Docker は既定で `/proc` 上に **locked なオーバーレイ**を張る: readonly-paths (`/proc/sys`・`/proc/bus`・
  `/proc/fs`・`/proc/irq`・`/proc/sysrq-trigger` を ro 再 mount) と masked-paths (`/proc/kcore`・`/proc/keys`・
  `/proc/interrupts`・`/proc/scsi`・`/proc/timer_list` 等を tmpfs/空で隠す)。これは HostConfig の
  `MaskedPaths`/`ReadonlyPaths` が **nil の時だけ**既定値が入る (非 nil の空スライスなら入らない)。
  `security_opt: [systempaths=unconfined]` がこの 2 つを空スライスにして既定マスクを丸ごと外す。
  出典: moby `daemon/create_unix.go:27-32` (nil → `oci.DefaultSpec().Linux.{Masked,Readonly}Paths`)、
  既定リスト `daemon/pkg/oci/defaults.go`。 `[docs(source)]`
- 上記マスクがあると、**ネストした rootless Podman が張る fresh `/proc` mount が
  `mount proc to proc: Operation not permitted` で弾かれる** (kernel は、既存の locked mount が隠している
  領域を新しい procfs が露出させる mount を拒否する)。`default_sysctls` や network mode とは無関係
  (空/既定 × `--network=none`/slirp4netns の 4 通り全てで同一エラー)。`--pid=host` (host の `/proc` を
  流用し fresh mount しない) なら通る。`systempaths=unconfined` でマスクを外すと
  (`grep /proc /proc/mounts` でオーバーレイ消失を確認) `podman run` が通る。解除後も dev は uid 1000 +
  `cap_drop: ALL` なので `/proc/sys` への書込は不可 (`[ -w /proc/sys/kernel/hostname ]` が ro)。
  封じ込め評価は PODMAN.md「blast-radius」。 `[empirical]`

## Docker Desktop (macOS) / runc

- workspace bind (virtiofs 共有) の**配下**にあるパスへ**個別ファイルを bind** する時、ターゲットが
  ホストに**未作成**だと runc が mountpoint を作れず起動失敗する
  (`create mountpoint for ... mount: mountpoint "/run/host_virtiofs/.../X" is outside of rootfs ...`)。
  ターゲットが既存なら成功する (上被せされる)。→ 個別ファイル bind は **bind 先をホストに事前生成**して
  使う (devcontainer.json の `initializeCommand`)。ディレクトリ/named volume の bind は影響を受けない。
  検証 (Docker Desktop, desktop-linux context, virtiofs): 同一 mount をターゲット既存=成功 /
  不在=同一エラーで再現。未作成 bind は失敗後にホストへ phantom (空ファイル/ディレクトリ) を残す
  (compose の secrets-proxy rules.yaml で既出の罠と同根)。 `[empirical]`
