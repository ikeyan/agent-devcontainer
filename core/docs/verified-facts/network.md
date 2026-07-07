# ネットワーク / DNS / proxy bypass

confidence tag の凡例: [README](README.md)。

## dnsmasq

- `log-queries` は bare flag か `=extra|proto|auth|only_failed`。`=no` は**不正値で起動失敗**する
  (設定全体が読めず、firewall も子 claude も起動しない)。ログ無効化は「その行を書かない」で行う。
  `[man][review]`
- `server=/host/upstream` は host **およびその全サブドメイン** (suffix 一致) を転送する。
  → `<secret>.api.anthropic.com` で DNS 持ち出しが成立する。厳密名だけ応答させるには
  `no-resolv` + `hostsdir` (転送そのものを止める)。 `[man][review]`

## iptables / netfilter (Docker)

- Docker 既定の embedded DNS (`127.0.0.11:53`) は NAT テーブルで高位ポートへ DNAT される。filter
  `OUTPUT` の `--dport 53` DROP は**変換後**に評価されるため一致せず、loopback の一律 ACCEPT で DNS が
  漏れうる。 `[review]`
- 帰結 (dev コンテナ `init-firewall.sh`): loopback の一律 ACCEPT (`-o lo`) は sfw のローカルプロキシに
  必須なので `127.0.0.11:53` へは絞れず、embedded DNS が upstream へ forward する DNS 経路は残る。ただし
  dev の脅威モデルは exfil を壁にしない (秘密は dev に無く secrets-proxy 側。`api.anthropic.com` 自体が
  exfil 経路になりうる。README「設計の根本」) ので、この DNS 経路も設計上許容。DNS 持ち出しを実際に塞ぐのは
  redact サンドボックス側で、そこは forward しない dnsmasq (`no-resolv` + `hostsdir`) を使う
  (`SECURITY-MODEL.md` 不変条件 6)。 `[review]`

## proxy bypass / NO_PROXY (curl / Go の suffix 一致)

- `NO_PROXY`/`no_proxy` のエントリは **ドメイン suffix 一致**で、bare `github.com` は `api.github.com` /
  `uploads.github.com` 等のサブドメインまで「proxy バイパス (直結)」にする。curl と Go (gh / podman 等) で
  共通。→ 「apex だけ直結・サブドメインは proxy 経由」を NO_PROXY では表現できない (否定エントリも無い)。
  proxy 経由にしたいサブドメインがあるなら、その親 (apex) を NO_PROXY に置かない。検証 (dev コンテナ):
    - `curl -v https://api.github.com/...`: NO_PROXY に `github.com` があると、api.github.com が NO_PROXY に
      無くても `Connected to api.github.com … port 443` で**直結** (proxy へ CONNECT を出さない)。
    - `NO_PROXY=github.com gh api user` → 401 (placeholder 直送) / `NO_PROXY=codeload.github.com gh api user`
      → 200 (api.github.com は proxy 経由で substitute 成功)。= Go も `github.com`→`api.github.com` を
      suffix 一致し、`codeload.github.com` は一致しない。 `[empirical]`
- 帰結 (devcontainer): gh の api.github.com / uploads.github.com を proxy で substitute するため、
  github.com は **NO_PROXY から外し passthrough_hosts で素通し**する (git は無検査 tunnel で proxy 経由)。
  codeload.github.com は API ホストを suffix 一致しないので NO_PROXY 直結のまま。
  (dnsmasq `server=/host/` の suffix 一致と同根の「親に効かせると子まで巻き込む」罠。冒頭 dnsmasq 項参照。)
  この不変条件 (NO_PROXY が proxy 経由ホストを suffix 一致しない) は `.devcontainer/core/bin/redact_invariants_check.sh` #5。
- **末尾カンマ (空エントリ) は無視される** — `compose.yaml` の `x-no-proxy` は末尾に
  `,${PROJECT_NO_PROXY:-}` を付けており、`PROJECT_NO_PROXY` が空だと `...,codeload.github.com,` の
  ように末尾カンマが残る。これが解析を壊さないことを検証した:
    - curl (この devcontainer で実測): `env no_proxy='registry.npmjs.org,' NO_PROXY='registry.npmjs.org,'
      curl -sv --max-time 3 -x http://127.0.0.1:9 https://registry.npmjs.org/` は
      `Uses proxy env variable no_proxy == 'registry.npmjs.org,'` と表示した上で **直結を試みる**
      (IPv6 宛先へ `Trying` → `Network is unreachable`。bogus proxy `127.0.0.1:9` へは CONNECT しない)。
      対照実験として `no_proxy=''` にすると同じ宛先で `Trying 127.0.0.1:9...` → `Connection refused` になり、
      「NO_PROXY 一致時は直結・不一致時は proxy 経由」という判定方法自体も併せて確認できる。 `[empirical]`
    - Go (`golang.org/x/net/http/httpproxy`, `net/http` が internal vendor するのと同一ロジック):
      `proxy.go` の `config.init()` が `strings.Split(c.NoProxy, ",")` の各要素を
      `strings.TrimSpace` した後 `if len(p) == 0 { continue }` で空要素を明示的に skip する。
      末尾カンマによる空エントリが「全ホスト一致」や parse error になることはない。 `[docs]`
      (`golang.org/x/net/http/httpproxy/proxy.go`)
  帰結: `PROJECT_NO_PROXY` 未設定 (空文字) でも `x-no-proxy` の末尾カンマは無害。

## musl (uv) resolver / 複数 nameserver

- `uv` の GitHub Releases バイナリは **musl 静的リンク** (`uv --version` → `aarch64-unknown-linux-musl` 等。
  `ldd` は `not a dynamic executable`)。同じ環境の `dig`/`getent`/`curl` は glibc 動的リンクなので、両者は
  **別の resolver 実装**で DNS を引く。 `[empirical]`
- musl の resolver は `/etc/resolv.conf` に列挙された nameserver の**いずれか 1 つでも無応答だと**、
  他の nameserver が正常応答していても `getaddrinfo` 全体を `EAI_AGAIN` (musl の `gai_strerror` 文言は
  `Try again`) で失敗させる。glibc はこの状況を許容し先着の正常応答を使う。
  検証 (nested rootless Podman = slirp4netns、`.devcontainer/core/Dockerfile` redact ステージを podman build):
  `/etc/resolv.conf` を 1 行 (`nameserver 10.0.2.3` のみ) に絞ると `uv pip install --no-cache` が 0.3s で
  成功、動く nameserver はそのまま先頭に残し IPv4 のみの他 nameserver (`8.8.8.8`/`8.8.4.4`) を足すだけでも
  25s のリトライ後に同じ `dns error: failed to lookup address information: Try again` で失敗 → 原因は
  IPv6 到達性ではなく「複数 nameserver 併記」自体。 `[empirical]`
- 発生源 (詳細と出典は `docs/verified-facts/podman.md`「rootless Podman / 共有 netns の resolv.conf に
  Google Public DNS が混ざる」): podman/containers-common がハードコードで Google Public DNS を
  ネスト build の resolv.conf に混ぜ込み、sandbox の allowlist firewall からは到達不能。
  実機で観測した実物 (devcontainer, nested podman build):
  ```
  nameserver 10.0.2.3
  nameserver 8.8.8.8
  nameserver 8.8.4.4
  nameserver 2001:4860:4860::8888
  nameserver 2001:4860:4860::8844
  options ndots:0
  ```
  `[empirical]`
- 対処: `uv pip install` の直前で `/etc/resolv.conf` を「先頭の `nameserver` 1 行 + `nameserver` 以外の行」
  だけに削ってから呼ぶ (`.devcontainer/core/Dockerfile` redact ステージ、`awk '!/^nameserver/ || !n++'`)。
  docker 環境の resolv.conf は通常 nameserver 1 行 (`127.0.0.11` 等) なので無害。行の相対順序も保持する
  (`nameserver` 以外の行を元の位置のまま残し、2 個目以降の `nameserver` 行だけ落とす)。
  実装注意 (grep 2 本の初期案を awk 1 本に置き換えた理由): `grep -v '^nameserver' resolv.conf` は
  「`nameserver` 以外の行が 1 行も無い」(= 上記の `options` のような行を持たない resolv.conf) だと
  **マッチ 0 件で exit 1** になり、`resolv=$(grep -m1 ...; grep -v ...)` という command substitution の
  代入文自体の exit status (= 最後に実行したコマンドの exit status) が 1 になって、`&& printf ...` を含む
  Dockerfile RUN の `&&` 連鎖ごと中断し `uv pip install` に到達せずビルドが落ちる
  (`grep -v ... || true` でも無害化できるが、awk はマッチ 0 件でも exit 0 なのでこの罠自体が無い)。
  `[empirical]`
- runtime への波及なし: build 時のこの変更は redact ステージの RUN 内限定 (docker/podman はレイヤーごとに
  `/etc/resolv.conf` を都度渡すため後続 RUN には残らない上、image に焼き込まれたとしても)
  `redact/entrypoint.sh` がコンテナ起動直後 (dnsmasq 起動直後・firewall 適用前) に
  `printf 'nameserver 127.0.0.1\noptions ndots:0\n' > /etc/resolv.conf` で無条件に上書きするため、
  DNS 持ち出し経路の脅威モデル (`SECURITY-MODEL.md` 不変条件 6) には影響しない。 `[empirical]`

## uv / sfw の CA 結合 (TLS)

- Socket Firewall (sfw) は package manager の TLS を MITM 検査し、実行ごとに自分の CA を各ツールへ
  `SSL_CERT_FILE` / `PIP_CERT` / `CARGO_HTTP_CAINFO` 等で渡す。**`uv` は system trust store か
  `SSL_CERT_FILE` の「片方」しか見ない**ため、sfw CA 単体を `SSL_CERT_FILE` に渡すと実物の上流 CA を
  検証できず `UnknownIssuer` で失敗する。system バンドル (`/etc/ssl/certs/ca-certificates.crt`) と
  sfw CA を結合して `SSL_CERT_FILE` に渡す (+ `UV_SYSTEM_CERTS=1`) と通る。 `[empirical]`
- sfw 配下では実体 uv (`/usr/local/bin/uv`) を直接呼ぶ (shim 経由にしない): shim だと sfw が入れ子に
  なり、内側 sfw が `SSL_CERT_FILE` を自分の CA 単体へ上書きして結合バンドルを潰す。外側 sfw が subtree を
  検査するので supply-chain スキャンは効いたまま。実装は `.devcontainer/core/uv-sync.sh`。 `[empirical]`
