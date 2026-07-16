# gh CLI

confidence tag の凡例: [README](README.md)。

## gh CLI / 認証 (secrets-proxy 経由)

- gh 2.46 は `~/.config/gh/hosts.yml` の `oauth_token` を **`Authorization: token <oauth_token>` で
  verbatim 送出**する (scheme は `token`、Bearer でない)。**placeholder 文字列も受理**する。検証:
  使い捨て `GH_CONFIG_DIR` に `oauth_token: "@@SECRET_github_token@@"` を置いて `gh api user` →
  api.github.com が **401 Bad credentials** (= placeholder をトークンとしてそのまま送っている)。 `[empirical]`
- gh は Go 製なので `HTTPS_PROXY`/`NO_PROXY` を既定で honor し、TLS は system CA pool (Linux は
  `/etc/ssl/certs`) を使う。proxy CA は install-proxy-ca.sh で system バンドルへ導入済みなので gh も
  信頼する (podman も Go で同経路の MITM が通っている前例)。api.github.com が proxy 経由になれば proxy が
  MITM して Authorization 内の placeholder を実トークンへ substitute する。**ただし NO_PROXY は suffix
  一致なので、api.github.com を NO_PROXY から外すだけでは不十分** — bare `github.com` が残ると
  api.github.com まで捕まえて直結させる (docs/verified-facts/network.md「proxy bypass / NO_PROXY」)。検証: NO_PROXY から
  github.com を外すと `gh api user` が proxy 経由で 200 を返し substitute が効く。 `[empirical]`
- hosts.yml format (gh 2.46): `github.com` 配下に `users.<login>.oauth_token` と top-level `oauth_token`
  の両方へ同じ値を書く。seed (gh/hosts.yml) は両方を placeholder にする。 `[empirical]`
- init-firewall.sh の `curl https://api.github.com/meta` は `sudo` 実行で env が剥がれる (env_reset、
  sudoers に env_keep 無し) ため proxy env を見ず直結する。よって api.github.com を NO_PROXY から外しても
  firewall 構築の meta 取得には影響しない (meta は IP レンジ算出用で、DROP 適用前の開放状態で取る)。 `[empirical]`

## gh CLI / config マイグレーション (hosts.yml の ro bind)

- gh は起動時に config マイグレーションを走らせ、必要なら **hosts.yml を書き換える** (gh 2.46 で観測:
  ダブルクォート→シングルクォートの再シリアライズ)。マイグレーション済みかは **config.yml の `version`** で
  判定し、ledger は hosts.yml でなく config.yml 側にある。よって hosts.yml を **ro bind** して `gh auth login`
  を構造的に封じる (書込が EROFS) には、`config.yml` に現行版 (`version: "1"`) を seed してマイグレーションを
  スキップさせる必要がある。検証 (gh 2.46.0): ① ro hosts.yml + config.yml 無し → `gh api user` が
  `failed to write config after migration` で失敗。② ro hosts.yml + `config.yml: version "1"` → `gh api user`
  が 200 (`ikeyan`)・hosts.yml は mtime 不変 (書換なし)。gh を上げてマイグレーション版が変わったら
  `make -C .devcontainer/core gh-seed` で追従する: committed の config.yml (= 現在の版) + hosts.yml を gh に渡して
  **差分 migration を実適用**させ、結果 (新 version + migrate 済み hosts.yml) を書き戻す (version を pin する
  のでなく gh 自身に schema 変換させるので hosts.yml の構造変更も反映)。追従漏れは `make check` の
  check-gh-seed が「installed gh が seed を書き換える」を検出して fail-closed。どちらもネットワーク不要
  (user: 設定済みで migration はローカル。`gh config get version` を unreachable proxy 付きでも exit 0 で確認)。 `[empirical]`

## gh CLI / config dir の読取と書込 (root 所有 seed)

- gh の config/hosts 読取は owner・permission を検査しない: go-gh v2.6.0 (gh 2.46.0 の go.mod が
  vendor) の `pkg/config/config.go` `readFile` は plain な `os.Open`+read で、`Stat` による所有者/mode
  検証は無い (ssh の strict permission check のような読取拒否は起きない)。 `[docs(source)]`
- 実機 (gh 2.46.0 Debian, proxy substitute 環境): `GH_CONFIG_DIR` を「書込不可 dir (555) + 読取専用
  file (444)」にしても読取系は成功する — `gh config get version` exit 0、`gh api user` は substitute
  経由で 200。書込系は clean に失敗する — `gh config set` が `failed to write config to disk: ...
  permission denied` exit 1、`gh auth logout` が hosts.yml への `open ... permission denied` (seed は
  byte 不変のまま)。 `[empirical]`
- **owner 書込可の file (0644 node 所有) のままでは封じにならない**: dir を 555 にしても file への
  in-place 書込 (`gh config set`) は成功してしまう (実測)。`gh auth login` の実トークン焼込を ro bind
  無しで封じるには file と dir の両方を node 非所有 (root 所有) にする — file 側が in-place 書込を、
  dir 側が unlink+再作成を塞ぐ。 `[empirical]`
  - 範囲の注意: これは既定パスへの「事故」封じ。同一 uid の意図的迂回 — 親 dir (`~/.config`,
    node 所有) ごと rename して書込可能な gh/ を作り直す、または `GH_CONFIG_DIR` を書込可能な場所へ
    付け替える — は塞げない (旧 ro bind も `GH_CONFIG_DIR` 迂回は塞いでいなかった。mount 上の rename は
    EBUSY で塞がっていた分だけ旧構成が強い)。実トークンは通常 dev に存在しない (proxy が egress で
    substitute) ため、防ぐべきは貼り付け事故の durable spill で、そこには足りる。
  - 範囲の注意 2: uid≠1000 の Linux ホストでは devcontainers CLI の `updateRemoteUserUID` が
    home を丸ごと `chown -R` するため root 所有自体が剥がれる
    (`devcontainers-cli.md`「updateRemoteUserUID」)。そのホスト群ではこの封じは効かない —
    accident-grade の割切りに含める。
- update-notifier 等の state は `GH_CONFIG_DIR` でなく StateDir (`XDG_STATE_HOME` 既定
  `~/.local/state/gh`) に書く (go-gh v2.6.0 `config.go` `StateDir`)。config dir を書込不可にしても
  state 書込とは干渉しない (実測: 上記読取系の実行で config dir への書込・エラーは発生しない)。 `[docs(source)][empirical]`
