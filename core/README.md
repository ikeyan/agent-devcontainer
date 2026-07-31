# devcontainer: Claude Code + Socket Firewall + secrets-proxy

ホストの sandbox と sfw が共存できない問題への対応。隔離境界を「コマンド単位の sandbox」から「コンテナ」に移す:

- Claude Code はコンテナ内で sandbox なし (`--dangerously-skip-permissions`) で動かす
- ホストの保護はコンテナが担う (書けるのは bind mount したこのリポジトリだけ)
- ネットワークは iptables の egress allowlist (`init-firewall.sh`) で制限
- `pnpm` / `npm` / `npx` / `pip` / `uv` は PATH 先頭の shim (`sfw-shim.sh`) により常に sfw 経由。alias 不要で、非対話シェル (Claude の Bash ツール) でも確実に効く
- **秘密情報 (スコープを切れない Web パスワード等) は dev コンテナに置かず、別コンテナ `secrets-proxy` だけが保持する。** dev は普通のリクエストを送るだけで、proxy が宛先ホストごとに秘密を注入する。サプライチェーン攻撃で dev が侵害されても秘密は読めない (後述)

anthropics/claude-code の公式 `.devcontainer` がベース。2 コンテナ構成なので docker compose (`compose.yaml`) で起動する。

## 公式 devcontainer からのセキュリティ強化

- **capability 最小化**: `--cap-drop=ALL` した上で `NET_ADMIN` / `NET_RAW` / `SETUID` / `SETGID` の 4 つだけ付与
  (各 cap の要否は `compose.yaml` の per-cap コメントと canon (`facts/capabilities/`))
- **DNS をリゾルバ限定**: 公式は任意ホストへの udp/53 を全許可 (DNS トンネリングで持ち出し可能) だが、/etc/resolv.conf のリゾルバ宛てのみに限定
- **SSH 全許可を削除**: 公式は任意ホストへの tcp/22 を全許可だが削除。GitHub への git+ssh は ipset の GitHub IP レンジで引き続き通る
- **fail-open 修正**: 公式は DNS 解決失敗で DROP ポリシー適用前に abort する (= firewall なしで起動)。警告して続行に変更
- sudo は postStart で再実行する `init-firewall.sh` / `install-proxy-ca.sh` だけを sudoers で許可。スクリプト本体はイメージに COPY された root 所有のものを指すので、bind mount 側を書き換えても効かない

トレードオフ:

- CHOWN を落としたため、起動中のコンテナに `docker exec -u root` で入って `apt-get install` すると dpkg が失敗することがある。パッケージ追加は Dockerfile に書いて rebuild する
- KILL を落としたが、影響するのは「root が node のプロセスに kill」だけ。シグナルは UID が一致すれば capability 不要なので、node ユーザー同士の Ctrl+C / kill / kill -9 は普通に効く (実測済み)

## secrets-proxy (秘密注入プロキシ)

### 脅威モデル

dev コンテナは sandbox なしで未検証コード (Claude + npm/pip 依存) を動かすため、**侵害される前提**で扱う。スコープを切れない秘密 (Web サイトのパスワードなど。API トークンと違い、漏れるとアカウント全権を奪われる) を dev に置くと、サプライチェーン攻撃で env / ファイルから抜かれる。

そこで秘密を **別コンテナ `secrets-proxy` だけ** に置く。dev からは決して読めない。

### 信頼境界とトラフィックの流れ

```
┌─────────────── dev (侵害される前提・秘密ゼロ) ───────────────┐
│  Claude Code / curl / git / アプリコード                      │
│    │  HTTP(S)_PROXY=secrets-proxy:8080                        │
│    │  NO_PROXY=レジストリ/Anthropic/GitHub… → これらは直結    │
│    ▼                                                          │
└────┼─────────────────────────────────────────────────────────┘
     │ (dev の iptables は proxy:8080 と NO_PROXY 直結先しか許さない)
     ▼
┌─────────────── secrets-proxy (信頼境界・秘密はここだけ) ──────┐
│  mitmproxy + addon.py                                         │
│   - 宛先ホストを rules.yaml と照合し、合致したら秘密を注入     │
│   - 秘密は「設定された宛先ホスト宛」にしか注入しない          │
│   - rules に無いホストは 403 (block_unlisted)                 │
│  秘密は起動時に Vaultwarden(bw) から取得し、メモリ上にだけ保持 │
└────┼─────────────────────────────────────────────────────────┘
     ▼  本物の証明書を検証してインターネットへ
```

### なぜ漏れないか (アンチ持ち出し)

- 秘密の実体は dev に一切渡らない。dev は秘密名すら知らなくてよい (自動注入)。
- 注入は **宛先ホストをキーに静的定義** される。`github.com` 用の秘密は `github.com` 宛のリクエストにしか載らない。dev が侵害されて `evil.com` 宛に秘密を載せ替えようとしても、`evil.com` は `rules.yaml` に無いので注入されない = **秘密が別ホストへ出ていく経路が存在しない**。
- 秘密を解決できないときは forward せずブロックする (空パスワードのまま送らない)。
- CA は公開証明書だけを共有ボリュームで dev に渡す。CA 秘密鍵は proxy 専用 volume (`proxy-confdir`) に留まる。

### クッキー/トークンのトークン化 (capture)

多くのサイトは「パスワードでログイン → 名前付きクッキーを取得 → 以降はそのクッキーが秘密」という流れになる。パスワードだけ隠してもクッキーが dev に渡れば意味がない。そこで proxy が**応答からクッキー/トークンを捕獲して保管し、dev には代替トークン (プレースホルダ) に置換して返す**。

```
dev ── POST /login (pw は inject) ──▶ proxy ──▶ サイト
dev ◀── Set-Cookie: session=@@CRED_…@@ ◀── proxy ◀── Set-Cookie: session=<本物> ──┘
        (dev のクッキー壺には代替トークンだけが入る。本物は proxy だけが保持)

dev ── Cookie: session=@@CRED_…@@ ──▶ proxy ──[bind 先か検査]──▶ Cookie: session=<本物> ──▶ サイト
```

- 捕獲源は `set-cookie` / `header` / `json` (本文のドットパス) / `regex` (group 1)。トークンの形 (base64 / JWT 等) はサイト依存だが、**値をそのまま不透明に扱う**ので形式に依存しない。
- **リフレッシュトークン → アクセストークン** のような本文 JSON のやり取りも同じ仕組み。リクエスト本文のプレースホルダを本物に復元し、応答本文の `access_token` を捕獲して代替トークン化する (`inject` の `type: json` + `capture` の `from: json`)。
- **本物の値は応答内の全出現を置換してから dev に返す**ので、ヘッダにも本文にも本物は残らない。
- 捕獲した秘密は `bind` で指定した宛先ホスト宛にしか復元されない (アンチ持ち出し)。
- トークンが更新 (再ログイン/リフレッシュ) されると同じプレースホルダのまま中身だけ差し替わるので、dev 側の代替トークンは使い続けられる。

### 危険エンドポイントの遮断 (block)

`rules.yaml` の `block:` に「ホスト (省略可) / メソッド (省略可) / パス正規表現」を書くと、合致したリクエストを 403 で遮断する (注入・捕獲より先に評価)。アカウント削除・パスワード変更・API キー発行・管理画面などを塞ぐ用途。`passthrough_hosts` のホストは MITM しないので block も効かない点に注意。

### リーク検知 (detect_leaks)

dev は本物の捕獲済み秘密を持たないはずなので、**リクエストに本物の値が現れたら漏洩**とみなして即遮断する。`detect_leaks: true` (既定)。

### 設計の根本: 壁・tripwire・surface 削減

防御は3層あり、**load-bearing な「壁」は1つだけ**。残りは best-effort の補助で、壁の代わりにはならない。これを取り違えると「firewall に穴があるのに leak 検知に意味があるのか」式の混乱になる。

- **壁 (唯一)**: crown-jewel (漏れるとアカウント全権を奪われる静的秘密) を dev に**使える形で渡さない**。binding + tokenization で実現する (上の「なぜ漏れないか」)。資格情報の実体は dev に存在せず、被害は bind ホストに限定される。保証はここだけ。
- **surface 削減 (firewall allowlist)**: dev の宛先集合を粗く絞るだけ。粒度は **IP (ipset)・全ポート・名前解決は dev 任せ**で、共有 CDN の co-tenant や同 IP の同居サービスまで芋づるに開く。ホスト名単位の制御ではない。
- **tripwire (detect_leaks)**: proxy が MITM する経路でだけ働く。scan 対象は `cap_store` の値 (= 捕獲/置換した本物の秘密) だけで、実体は **proxy 自身の redaction バグ検知器**。exfil 防止壁ではない。

**exfil を壁にできない根本理由**: dev と同一 uid の敵対プロセスは、開いている任意のチャネル (直結穴・passthrough・resolver への DNS すら) に、正規に見えるリクエストへ steganography やタイミングで情報を混ぜて持ち出せる。allowlist 内の `api.anthropic.com` 自体が exfil 経路になる。よって設計は exfil 防止を約束せず、「**crown-jewel を渡さない**」一点に賭ける。下の残存リスクはこの帰結。

**直結 vs proxy の選択もこの観点で決まる**:
- egress の厳密さだけなら **passthrough (proxy がホスト名を解決して :443 へ) の方が直結穴 (IP 粒度) より tight**。
- それでも生命線級 (Anthropic / 高ボリューム DL) を直結にするのは、**全秘密を集約した proxy = 最高価値の標的 (SPOF) への曝露を最小化**し、可用性を proxy から独立させるため。passthrough-everything は逆に全トラフィックをその標的へ集めてしまう。
- 単に通したいだけなら `allow_hosts` (MITM するが leak 検知は残る) が既定。MITM が壊れる cert-pin のホストだけ最小限 `passthrough` (無検査) にする。

### 残存リスク (設計上の限界)

- **侵害された dev は、注入対象ホストに対しては秘密を「使える」**。例えば `github.com` 用の秘密があれば、悪性コードが `github.com` 宛リクエストを出して注入させ、ユーザーとして操作できる。捕獲したクッキー/トークンも同様で、代替トークンを bind 先ホストに送れば認証済み操作ができてしまう (proxy が本物に戻すため)。これは「Claude にそのサイトを使わせる」目的と表裏一体で避けられない。緩和は「秘密/トークンの**実体**が攻撃者インフラへ持ち出されない (= 被害が bind ホストに限定され、資格情報そのものは盗まれない)」点と、`block` による危険操作の遮断。
- プレースホルダ (代替トークン) は秘匿前提ではない (bind ホスト束縛が効くので知られても別ホストには使えない)。ただし bind の範囲は最小にすること。
- Vaultwarden の API キー / セッションキー (`BW_SESSION`) はボルト全権 (セッションキーは vault 全体を復号できる)。可能なら注入に要るアイテムだけを置いた**専用アカウント**を使う (セットアップの注記参照)。マスターパスワードも API キーも保存しない (bootstrap で1度入力するだけ)。
- **JS の多い Web ログインや、ホスト側 browserless 経由のブラウザ操作はこの proxy では注入できない**。proxy が見るのは HTTP レイヤだけ。ブラウザを使う場合はブラウザ自身を proxy 経由にし CA を信頼させる必要があり、ホストで動く browserless は対象外。dev コンテナ内から出る HTTP(S) (curl / fetch / requests / git 等) が対象。

### セットアップ (初回 1 回)

認証情報は bootstrap で対話入力し、得られた `BW_SESSION` だけが proxy 専用ボリューム
(`proxy-bw` = `$BITWARDENCLI_APPDATA_DIR/session.key`) に保存される。

```shell
# bootstrap: BW_SERVER / API キー / マスターパスワードを対話入力する
make -C .devcontainer/core bootstrap
# 生 compose を直接叩く場合 (project 層を先頭にした 2 ファイル指定が必須。理由は下記「セットアップ」節):
docker compose -f .devcontainer/project/compose.yaml -f .devcontainer/core/compose.yaml run --rm -it secrets-proxy bootstrap
```

bootstrap が `bw login --apikey` → `bw unlock` を行い、`BW_SESSION` を `session.key` に保存する。
以後は `docker compose ... up` するだけで proxy がそれを読んで解錠する。

- **マスターパスワードも API キーも保存しない**。bootstrap で1度入力するだけ。保存するのは vault を
  復号するセッションキー `BW_SESSION` (= `session.key`, 600/mitm 所有, proxy-bw ボリューム内なので
  リビルドを跨いで永続)。`bw login --apikey` だけでは vault は locked のままで使えるセッションは
  得られない (= bootstrap の `bw unlock` が必須)。セッションはパスワード変更/ログアウトまで有効。
  失効したら再 bootstrap する。
- **セキュリティ上の注意**: `session.key` は暗号化 vault (`data.json`) と同じ `proxy-bw` ボリュームに
  あり、両者が揃うと「解錠済み vault が at-rest で存在する」のと等価。マスターパスワード再入力を不要に
  する代償。ボリューム (= Docker ホスト) のアクセス権がこの proxy の秘密と等価になる。dev はこの
  ボリュームをマウントしないので dev からは見えない。
- 未 bootstrap (= `session.key` 不在) なら proxy が起動時に「未保存」と明示して exit、または
  `BW_SESSION` で解錠できなければ `status=...` を添えて exit → healthcheck が通らず dev も起動しない
  (=フェイルクローズ)。理由は proxy のログに出る (`docker compose -f .devcontainer/project/compose.yaml -f .devcontainer/core/compose.yaml logs secrets-proxy`)。
- `.devcontainer/project/rules.yaml` (**git 管理**): 宛先ホストごとの `inject` (静的注入) / `capture` (トークン化) と、トップレベルの `block` (危険エンドポイント遮断)。秘密は含まない (Vaultwarden のアイテム名と宛先ホストだけ) ので追跡する。全ルール型の構造・必須項目 (`secret.field` の許容値を含む) は `.devcontainer/core/secrets-proxy/rules.schema.json` (JSON Schema) が定義し、`make -C .devcontainer/core check-rules` で検証する (schema/検証は core、設定の中身は project)。`rules.yaml` が無いと secrets-proxy は起動しない。
- **再 bootstrap / 別アカウントに切替える**には、`docker compose ... run --rm secrets-proxy bash -lc 'rm -f $BITWARDENCLI_APPDATA_DIR/session.key'` で session を消すか、`docker volume rm <project>_proxy-bw` (`<project>` = project/compose.yaml の `name:`) で login 状態ごと破棄してから bootstrap し直す。

### 構成ファイルの検証

```shell
make -C .devcontainer/core check   # compose / shell / addon.py / rules / Dockerfile を一括検証
```

`make -C .devcontainer/core help` で個別ターゲット一覧。検査に要る道具 (shellcheck / hadolint /
PyYAML 等) は CI/devcontainer の両方に実在する前提で、skip ガードは無い (無い環境ではその場で
自然にエラーになる)。

### BW CLI / mitmproxy のバージョン

`secrets-proxy/Dockerfile` の `BW_CLI_VERSION` は実在するリリース (タグ `cli-v<VERSION>` / アセット `bw-linux-<VERSION>.zip` および `bw-linux-arm64-<VERSION>.zip`) に合わせること。Vaultwarden と互換のある版を選ぶ。バイナリは buildx の `TARGETARCH` でホストと同じアーキ (amd64/arm64) を選択する — **linux arm64 の単体バイナリは 2026.4 以降のみ**配布なので、Apple Silicon ではそれ以上に固定する (古い版だと x86-64 バイナリが Rosetta で `ld-linux` 不在により落ちる)。`MITMPROXY_VERSION` も固定済み (addon API がメジャー間で変わるため)。

### デバッグ: mitmweb で傍受フローを見る

secrets-proxy の通信をヘッダ/ボディまでブラウザで観察したいときは、opt-in override で
secrets-proxy だけを `mitmweb` に切替える (基底の secrets-proxy は従来どおり `mitmdump`)。
dev は VS Code が管理しているので **無停止のまま** proxy だけ差し替わる:

    make -C .devcontainer/core proxy-web        # 要 host python3
    # 実体: up -d --build --no-deps secrets-proxy → host 踏み台トンネルを foreground で開く
    # project 名は project/compose.yaml の `name:` で VS Code と共有。dev は touch しない。
    # --build: entrypoint.sh / tcp-bridge.py は image に COPY されるので変更反映に再ビルドが要る (通常一瞬)。

`make proxy-web` は起動ログの token を拾って URL を表示し、続けて **host 踏み台トンネル**
(`proxy-web-tunnel.py`) を foreground で開く。表示された `http://127.0.0.1:8081/?token=<token>` を
ブラウザで開く。トンネルは **Ctrl-C で閉じる** (secrets-proxy は動いたまま)。完全に mitmdump へ戻すには:

    make -C .devcontainer/core proxy-web-off

> `dockerComposeFile` に `compose.mitmweb.yaml` を足す形 (devcontainer.json 改変) でも Web UI は
> 出るが、常時 Web が立ちっぱなしになり差分も残るので非推奨。切替は上の make target で行う。

**なぜ dev から見えないか**: mitmweb のフロー画面には proxy が substitute した平文 cred が on-wire で
映るので、敵対的 `dev` (同一ネットワーク・NET_ADMIN 持ち) や LAN に晒せない。**経路と token の 2 段**で守る:

- **経路 = container loopback bind + publish 無し**。mitmweb の Web UI は `web_host=127.0.0.1`
  (container の loopback) にだけ bind し、host publish は張らない。dev は別コンテナなので、
  `route_localnet=0` の下では他コンテナの loopback へ到達できない (martian drop。NET_ADMIN でも
  weak host model で迂回不可)。LAN も publish が無いので届かない。host は `docker exec` で container 内に
  入って loopback へ中継する踏み台 (`proxy-web-tunnel.py` → `/app/tcp-bridge.py`) 経由でだけ届く。
  dev は docker socket を持たず `docker exec` できないので、この経路も使えない。
- **token = defense-in-depth**。mitmweb 常時 ON の token (128bit/起動毎、entrypoint が `web_password` に
  固定) が二重目の壁。dev は別コンテナで token を取得できない。

以前検討した「admin IP に bind + admin ネット隔離」は NET_ADMIN dev に weak host model で破られ (経路隔離に
ならず)、加えて Docker Desktop の publish 先 container IP が admin IP と一致せず Empty reply になったため
撤去した (経緯は canon: facts/mitmproxy/weak-host-model-bypasses-admin-ip-bind と canon: facts/mitmproxy/admin-ip-bind-breaks-docker-desktop-publish)。loopback bind は dev を経路で確実に締め出しつつ、
publish に依存しないので Empty reply も起きない。踏み台トンネルは接続ごとに `docker exec` を起こすので
多少もたつくが、debug 用途には十分。

(`redact/SECURITY-MODEL.md` 不変条件 1/4)

## 使い方

VS Code: コマンドパレットから "Dev Containers: Reopen in Container"。

CLI:

```shell
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . zsh
```

コンテナ内で:

```shell
claude --dangerously-skip-permissions
```

初回はログインが必要。認証情報は named volume (`claude-config`) に永続化されるので 2 回目以降は不要。

起動時は compose が `secrets-proxy` を先に立ち上げ、healthcheck (CA 発行 + :8080 listen) が通ってから dev が起動する。未 bootstrap (`session.key` 不在) や `BW_SESSION` 失効だと proxy が healthy にならず、dev も起動しない (=フェイルクローズ)。失敗理由は proxy の起動ログに出る (`docker compose -f .devcontainer/project/compose.yaml -f .devcontainer/core/compose.yaml logs secrets-proxy`)。

## 初回セットアップ

`node_modules` はコンテナ専用の named volume (ホストの darwin-arm64 バイナリ入り node_modules を Linux 用バイナリで壊さないため)。初回は空なので:

```shell
pnpm install   # 自動的に sfw 経由になる
```

Python の venv も同様: `/workspace/.devcontainer/.venv` (Dockerfile の `UV_PROJECT_ENVIRONMENT` で固定。pyproject.toml/uv.lock 自体も同じ `.devcontainer/core/` 配下) を `node_modules` と同じく named volume (`venv`) で覆い、ホストの mac 用 venv を隠したコンテナ専用にする。

初回 (や `uv.lock` 更新時) だけ env を作る:

```shell
.devcontainer/core/uv-sync.sh   # Socket Firewall の CA を結合して uv sync (詳細はスクリプト冒頭)
```

以後 `uv run` は既存 env を使うのでネット不要。

## allowlist の編集

許可先には 2 系統ある:

1. **直結する (proxy をバイパスする) インフラ系ドメイン** — このリポジトリ固有の依存先は `.devcontainer/project/allow-domains.txt` (iptables 直結許可。1 行 1 ドメイン) と `.devcontainer/project/.env` の `PROJECT_NO_PROXY` (proxy バイパス。カンマ区切り) の**両方に対で**追記する。前者は image へ COPY され `init-firewall.sh` が `ALLOWED_DOMAINS` に合流させ、後者は `compose.yaml` の `NO_PROXY` アンカー末尾に補間される。**編集後はイメージの rebuild が必要** (allow-domains.txt は COPY されるため)。kit 本体 (Claude Code / VS Code / GitHub 系等) 用の直結許可は `core/init-firewall.sh` の `ALLOWED_DOMAINS` と `core/compose.yaml` の `NO_PROXY` にあるが、通常はリポジトリ側で編集しない。**注意: NO_PROXY は suffix 一致**なので、proxy 経由にしたいサブドメインを持つ親 (apex) を NO_PROXY に置かないこと — 例えば `github.com` を置くと gh の `api.github.com` (proxy が token を substitute) まで直結し substitute が無効化される。git 用の `github.com` は NO_PROXY でなく `passthrough_hosts` で素通しする (canon: facts/network/no-proxy-suffix-match-and-trailing-comma)。
2. **proxy 経由で出す Web/API ホスト** — `.devcontainer/project/rules.yaml` に書く。秘密注入が要るなら `hosts:` に (Vaultwarden 側に対応アイテムがあること)、要らないが通したいだけなら `allow_hosts:` に。proxy 再起動だけで反映され、dev の rebuild も `init-firewall.sh` の編集も不要。

allowlist は起動時に DNS 解決した IP を ipset に入れる方式なので、CDN の IP ローテーションで時間が経つと繋がらなくなることがある。その場合はコンテナ内で再実行:

```shell
sudo /usr/local/bin/init-firewall.sh
```

## HTTP_PROXY を尊重しないコマンド (proxychains)

`HTTP_PROXY`/`HTTPS_PROXY` を見ないツールを secrets-proxy 経由で通したいときは `pc <cmd>` を使う:

```shell
pc curl https://api.example.com/...
pc some-tool ...
```

- 実体は proxychains4。libc の `connect()` を LD_PRELOAD で捕まえ、secrets-proxy:8080 (HTTP proxy) へ流す。
  `proxy_dns` によりホスト名は `CONNECT <host>:443` として proxy に届くので、redsocks のような
  IP 落ち (`SO_ORIGINAL_DST`) が無く、`rules.yaml` のホスト照合・秘密注入・block がそのまま効く。
- **localhost / RFC1918 (host.docker.internal のローカルサービス等) は proxy を通さず直結**する
  (`proxychains4.conf` の `localnet`)。
- proxychains の `[ProxyList]` は数値 IP しか受けないため、`pc` が起動時に `secrets-proxy` を解決して
  テンプレート `/etc/proxychains4.conf` の placeholder を実 IP に差し替えた一時 conf で走る。**直接
  `proxychains4` ではなく `pc` を使う** (直接だと placeholder が未展開)。
- **限界**: 証明書ピンニングをするクライアントは proxy が MITM するので失敗する (その宛先は
  `passthrough_hosts` へ)。静的リンクのバイナリ (Go 等) は LD_PRELOAD が効かず捕まえられない
  (その場合は宛先を `rules.yaml`/`ALLOWED_DOMAINS` 側で許可して直結させる)。

## 注意点

- **localhost のサービス**: minio / browserless / MySQL は compose.yaml のサービスではなく、ホスト側で動く外部サービス (このリポジトリのアプリコードが使う)。コンテナ内からは `localhost` ではなく `host.docker.internal` で参照する必要がある (アプリのルート `.env` — compose 変数展開用の `.devcontainer/project/.env` とは別、gitignore 対象 — の `MINIO_ENDPOINT` / `BROWSERLESS_ENDPOINT` / `MYSQL_HOST` / `DATABASE_URL` は localhost 前提なので、コンテナ内で使うスクリプトは読み替えが必要)
- **ホストパス**: `OBSIDIAN_DIR` などホストの絶対パスはコンテナから見えない
- **WebFetch / 任意の外部通信**: allowlist 外のホストはすべてブロックされる。インフラ系は `ALLOWED_DOMAINS`+`NO_PROXY`、それ以外の Web/API ホストは `.devcontainer/project/rules.yaml` で許可する
- **sfw の自動更新**: バイナリはビルド時に取得済み。GitHub は allowlist 内なので日次の自動更新もそのまま動く。sfw のパッケージ取得はレジストリ直結 (`NO_PROXY`) なので proxy の MITM とは干渉しない
- **TLS の MITM**: proxy 経由で出る HTTPS は secrets-proxy が終端する (dev は proxy CA を信頼)。証明書ピンニングをするクライアントは proxy 経由だと失敗するので、その宛先は `passthrough_hosts` で素通しにする
- **proxy の CA 信頼**: dev は postStart の `install-proxy-ca.sh` で proxy の公開 CA をシステム信頼ストアに追記する。`NODE_EXTRA_CA_CERTS` / `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE` も設定済み
