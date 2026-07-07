# devcontainer 内の rootless Podman

dev コンテナ内で **rootless・daemonless** な Podman でコンテナを作れるようにする設定。
user namespace + subuid で動く。権限緩和は seccomp (Docker 既定 profile に rootless 用 syscall を足した
tailored profile。`unconfined` ではない) と、ネスト `/proc` mount を通すための dev の /proc マスク解除
(`systempaths=unconfined`) の 2 点で、Podman 用の cap 追加は無い (評価は下記 blast-radius)。
`newuidmap`/`newgidmap` が必要とする cap は file-cap で与える
(この環境では setuid→cap 付与が効かないため。docs/verified-facts/podman.md「rootless Podman / newuidmap」)。

## 構成

各設定ファイルに理由コメントがあり、深い根拠は docs/verified-facts/podman.md にある。ここは所在の地図:

- **Dockerfile (dev ステージ)**: `podman`/`uidmap`/`slirp4netns` を apt 追加 (uidmap・slirp4netns は
  Recommends なので明示) し `podman/*.conf` を `/etc/containers/` へ COPY。`newuidmap`/`newgidmap` は
  file-cap 化して setuid を外す (docs/verified-facts/podman.md「rootless Podman / newuidmap」)。
- **`podman/*.conf`**: storage=overlay (graphroot は podman volume 上に置き overlay-on-overlay を
  回避。vfs に戻す手順は下記) / cgroupfs + file logger / slirp4netns / 短縮名の docker.io 解決。各値の
  根拠は同ファイルのコメント参照。
- **compose (`dev`)**: `security_opt` に tailored seccomp + `systempaths=unconfined` (下記)、slirp4netns
  用の `/dev/net/tun`、Podman ストア volume。Podman 用の cap 追加は無い (newuidmap の cap は file-cap)。
- **seccomp profile** (`podman/seccomp.json`): `gen-seccomp.ts` が生成し `check-seccomp` が drift を検査。
  詳細は下記「seccomp profile (tightening)」。

rebuild 後の確認 (オフラインで通る):

```sh
podman info                     # storage=overlay / runtime=crun / network=slirp4netns
```

`podman run <image>` で実際にコンテナを作るには image が要る。registry は許可制で Docker Hub / GHCR は
既定で pull 可、他は要許可 → 下記「イメージの取得」を参照。

## イメージの取得 (registry)

dev コンテナの egress は secrets-proxy (`block_unlisted: true`) + `init-firewall.sh` でロックされており、
registry は secrets-proxy の `allow_hosts` に挙げたホストにだけ到達できる。**既定では Docker Hub と GHCR が
許可済み**で pull/run できる (実機確認済み)。それ以外の registry は到達できず失敗する。選択肢:

- **ローカルで完結 (pull 不要)**: `podman import` (tarball)、および base が `scratch`/取得済みの
  `podman build` は registry を要らない (`FROM <未取得>` の build は base image を pull する)。
- **registry を許可する**: 使う registry のホストを secrets-proxy の `allow_hosts` に足す。既定許可の
  registry ホスト (Docker Hub の manifest/token/blob-CDN と GHCR) は `.devcontainer/project/rules.yaml` の
  `allow_hosts` (per-host コメント付き) 参照。完全一致なので apex の `docker.io` でなく podman が実際に
  繋ぐホストを挙げる — blob CDN は manifest とは別ホストで `registry-1` が署名付き URL でリダイレクトする
  (新しい registry は最初の pull 失敗の proxy block ログ / blob の 307 `Location` で確認)。これは dev の
  到達先を広げる **egress ポリシー変更**。registry は NO_PROXY 外なので proxy 経由 → firewall (allowlist)
  側の追加は不要。proxy は MITM だが podman は system CA バンドル (= proxy CA) を信頼するので TLS は通る。

## なぜ seccomp 緩和が要るのか

rootless Podman は userns を作るが、dev の既定設定では弾かれる (kernel でなくランタイムの既定 seccomp
profile による制限。probe の確定値は docs/verified-facts/podman.md「コンテナ (Podman / userns)」)。これが seccomp
緩和を入れる理由。緩和は `unconfined` ではなく、必要な syscall だけを開いた tailored profile で行う
(下記「seccomp profile (tightening)」)。

## blast-radius (破壊半径) の検討

前提: mac では dev コンテナは **ランタイムの Linux VM** 内で動く。コンテナ脱出は mac ではなく
**その VM** に着地する。ただし **secrets-proxy・MITM CA 秘密鍵・Vaultwarden セッションは同じ VM 内の
別コンテナ**にあるので、「dev → VM」昇格が意味を持つ脅威。

緩和は **seccomp (tailored)** と **/proc マスク解除 (`systempaths=unconfined`)** の 2 点 (Podman 用の
cap 追加は無い。既存の NET_ADMIN/NET_RAW/SETUID/SETGID は firewall/sudo 用):

- **seccomp (tailored)**: 既定 profile が CAP_SYS_ADMIN / CAP_SYS_CHROOT で gate する syscall のうち
  rootless に要る分だけを開き、危険系 (`bpf`/`perf_event_open`/`kexec` 等) は gate のまま。`unconfined`
  (~44 の危険 syscall を全開放) と違い **VM kernel への攻撃面を最小限に保つ**。開く/開かない syscall の
  確定一覧と生成・追従は下記「seccomp profile (tightening)」(= `gen-seccomp.ts`)。
- **/proc マスク解除 (`systempaths=unconfined`)**: ネスト rootless Podman の fresh `/proc` mount を
  Docker 既定の locked /proc マスク (ro `/proc/sys`・masked `/proc/kcore` 等) が EPERM で弾くため外す
  (機構は docs/verified-facts/docker.md「Docker /proc マスク (systempaths)」)。防御は下がるが、dev は uid 1000 +
  `cap_drop: ALL` なので解除後も kcore 読取 (CAP_SYS_RAWIO 要) も root 所有 /proc/sys 書込 (CAP_SYS_ADMIN
  要) もできず、ネスト先 mapped-root も自 userns の fresh /proc しか触れない。低下するのは情報露出系の
  defense-in-depth のみ。

**ネットワーク封じ込めは維持される**: ネストした Podman コンテナの egress は slirp4netns 経由で
dev コンテナの netns を通る → `init-firewall.sh` の iptables (proxy + allowlist のみ) がそのまま効く。
(ネスト先で HTTPS を proxy 経由にするには `HTTP_PROXY`/`NO_PROXY` と CA を伝播。直結は firewall が弾く。)

## seccomp profile (tightening)

`seccomp=unconfined` ではなく、Docker 既定 profile に rootless Podman 用の syscall だけを足した
tailored profile (`podman/seccomp.json`) を使う。再生成・upstream 追従・drift 検査をできるようにしてある:

- **生成**: `make gen-seccomp` (`gen-seccomp.ts` が既定 profile を stdout に変換 → `seccomp.json` へ書く)。
  既定 profile (moby/profiles `seccomp/default.json`) を基に、`clone` の `CLONE_NEW*` 制限解除・`clone3`
  許可・namespace/mount 系 (`unshare`/`setns`/`mount`/`umount`/`umount2`/`pivot_root`/`mount_setattr`/
  新 mount API/`sethostname`/`setdomainname`/`keyctl`) の許可を追加する (すべて `excludes: CAP_SYS_ADMIN`)。
  加えて `chroot` の cap gate を外す: 既定は `includes: CAP_SYS_CHROOT` で gate されるが、engine は custom
  profile の includes を**コンテナの cap セット**で解決するため、この cap を持たない dev では rule が落ち
  `chroot` が defaultAction(ERRNO) になる (buildah copier 等が EPERM)。gate を外して常時許可しても、実権限は
  kernel の cap チェック = userns 内でのみ有効なので同 uid プロセスへの昇格にはならない。
- **開かない (gate 維持)**: `bpf`/`perf_event_open`/`lookup_dcookie`/`quotactl(_fd)`/`fanotify_init`/
  `lsm_*`/`syslog` — `unconfined` だと開いてしまう危険系。
- **出典の pin と追従**: 既定 profile は `podman/http-cache/raw.githubusercontent.com/moby/profiles/main/seccomp/
  default.json` に commit 済み。upstream 更新に追従するときは `HTTP_CACHE_REFRESH=1 make gen-seccomp` で
  再取得+再生成し、`podman/http-cache/` と `seccomp.json` の diff をレビューして commit する。
- **drift 検査**: `make check` の `check-seccomp` が「snapshot + 変換 == commit 済み seccomp.json」を
  offline で検証する (ずれていれば exit 1)。
- compose は `security_opt: [seccomp=./podman/seccomp.json]`。path は project ディレクトリ基準で compose
  が本文を inline 展開する (docs/verified-facts/docker.md「Docker compose」)。

## vfs に戻す / 切替時の注意

封じ込め最優先 (storage 層の追加挙動ゼロ) にしたいなら `storage.conf` の driver を `"vfs"` に戻せる。
vfs ⇄ overlay を切り替えたら既存ストア (podman volume) のドライバが食い違うので、一度
`podman system reset -f` でリセットする (ストアは pull 可能なキャッシュなので破棄可)。native overlay が
万一使えない環境では fuse-overlayfs を apt で足し `mount_program = /usr/bin/fuse-overlayfs` + compose に
`devices: [/dev/fuse]` を入れる手もある。
