# mitmproxy / mitmweb (11.1.3 固定)

secrets-proxy は `mitmproxy==11.1.3` を使う (`.devcontainer/core/secrets-proxy/Dockerfile`)。
flag/オプションはこの版のソースで確認した。

## `http.Request.path` は origin-form で path+query を含む (query 除去は最初の `?` で切る)

- `Request.path` は request-target をそのまま返し、**path と query の両方**を含む (docstring:
  "This attribute includes both path and query parts of the target URI"; 例 `/index.html?a=b`)。
  OPTIONS は `*` になりうる。→ path から query を落とすには最初の `?` で切る (`split("?",1)[0]`)。
- fragment (`#…`) は本来 request target に載らない (client が除く) が、防御的に `#` でも切る。
- 実測 (venv の mitmproxy 12.2.3。proxy 固定は 11.1.3 だが docstring/挙動は同じ):
  `req.path` を `/a/b?x=SEK#frag` にすると `split("?",1)[0]` = `/a/b`。
- 用途: secrets-proxy の観測/ブロックログが path 埋め込み秘密を漏らさない sanitize
  (`addon.py:_safe_path` — query/fragment 除去 + token 状 segment 伏せ)。 `[docs][empirical]`

## mitmweb の Web UI オプション

出典: `mitmproxy/tools/web/webaddons.py` (tag `v11.1.3`, class `WebAuth`/`WebAddon`)
- `web_host` (str, 既定 `127.0.0.1`) — Web UI の bind host。
- `web_port` (int, 既定 `8081`) — Web UI の port。
- `web_open_browser` (bool, 既定 `True`) — 起動時にブラウザを開く。ヘッドレスでは `--set web_open_browser=false`。
- `web_debug` (bool, 既定 `False`)。
- `web_password` (str, 既定 空) — 空でも起動時に `secrets.token_hex(16)` を生成し **認証は常時 ON**。
  平文値はそのまま bearer token になり `?token=<web_password>` / `Authorization: Bearer <web_password>` で
  認証できる (`WebAuth.is_valid_password` = `hmac.compare_digest`)。`$` 始まりは argon2 hash 扱い。

## token は web_open_browser=false だとログに出ない (重要・要訂正だった点)

以前ここに「token は起動ログの web_url に出る」と書いていたが **誤り**。`WebAddon.running()`
(v11.1.3 `mitmproxy/tools/web/webaddons.py`) が token 入り web_url を出すのは **`web_open_browser=true`
かつ `open_browser()` が失敗したパスだけ**:
```python
def running(self):
    if hasattr(ctx.options, "web_open_browser") and ctx.options.web_open_browser:
        success = open_browser(master.web_url)
        if not success:
            logger.info(f"No web browser found. ... point it to {master.web_url}")
```
ヘッドレス運用 (`web_open_browser=false`) では `running()` 本体が丸ごとスキップされ、**token は生成
されるがどこにも表示されない**。加えて `web_url` property は `web_password` を設定すると token を URL に
出さない (`auth = ""`; 平文 password を晒さない方針) ので、mitmweb 自身に出させる手は無い。
→ よって secrets-proxy の entrypoint は **token を自前生成 → `--set web_password=<token>` で固定 →
`http://127.0.0.1:<port>/?token=<token>` を echo** して surface する (`make proxy-web` がその行を拾う)。
実測 (mitmweb 12.2.3): `?token=<web_password>`→200 / token 無し・誤 token→403、起動ログに token 行は
出ない。11.1.3 ソースも同一ロジック (上記 GitHub tag v11.1.3 で確認)。

## mitmweb と mitmdump のオプション差

- `flow_detail` は `mitmproxy/addons/dumper.py` (mitmdump 専用 Dumper) のオプション。
  `mitmproxy/addons/__init__.py` の `default_addons()` に Dumper は無く、mitmweb (WebMaster は
  `default_addons()` のみ) には **登録されない**。
  - ただし **未登録の `--set` はクラッシュしない**。CLI は `mitmproxy/tools/main.py` が
    `opts.set(*args.setoptions, defer=True)` で処理し、`optmanager.set(defer=True)` は未知キーを
    即エラーにせず `self.deferred` に退避する。`process_deferred()` は登録済みキーだけ適用し、
    登録されないまま残った deferred は **黙って無視** される (v11.1.3, `optmanager.py`)。
    実測でも mitmproxy 12.2.3 で未知 `--set` は起動継続した (`.venv`)。
  - よって mitmweb に `--set flow_detail=...` を渡してもクラッシュはしないが **無意味**。
    mitmweb 分岐には渡さない — クラッシュ回避ではなく、mitmdump 専用オプションを混ぜず
    invocation を正直に保つため。mitmdump 分岐には `--set flow_detail=0` を置く (登録済みで有効)。
- `--listen-host` / `--listen-port` / `-s <script>` / `--set confdir=` / `--set block_global=` /
  `--set stream_large_bodies=` は core で mitmweb でも有効。
- `--set <key>=<value>` 形式は全オプション共通 (dashed flag 名に依存しない)。

## Host/Origin allowlist は無い

`mitmproxy/tools/web/app.py` (v11.1.3) に check_origin / Host allowlist の実装は無い。
認証は token/password (`_require_auth` が `token` 引数/cookie を検査) のみ。したがって web_host を
別 IP に bind しても、token さえ合えば `127.0.0.1` (host publish) 経由でアクセスできる。WebSocket は
同一 origin (ブラウザも WS も `127.0.0.1:8081`) なので Tornado 既定の check_origin を通る。

## dev(NET_ADMIN) は admin IP bind でも :8081 に到達し得る (weak host model)

secrets-proxy は devnet と proxy-admin にマルチホームする。mitmweb を admin IP (172.31.242.2) に
bind し dev を proxy-admin に載せなくても、**NET_ADMIN を持つ dev は経路隔離を迂回できる**: devnet 上で
見える secrets-proxy の IP を next-hop に `ip route add 172.31.242.2/32 via <secrets-proxy の devnet IP>`
すると、パケットは secrets-proxy の devnet インターフェースに届き、Linux の **weak host model** (宛先が
自分のローカルアドレスなら別 IF 着でも受理。rp_filter も source が正当なら通す) で `172.31.242.2:8081` の
listener に配送される。mitmweb は IP bind であって device bind ではない (`SO_BINDTODEVICE` を使わない) ので
着信 IF を絞れない。addon の allowlist/403 は proxy(:8080) にしか効かず :8081 には効かない。
→ 経路隔離は best-effort であり、**主防御は mitmweb 常時 ON の token** (entrypoint が生成し `web_password` に
固定。dev は別コンテナで取得不能 → 到達しても 403)。根本遮断するには secrets-proxy 側で netfilter ingress を
絞る (NET_ADMIN が要る) しかない。(Codex PR #8 P2 指摘を検証。2026-07-05)

## admin IP 限定 bind は Docker Desktop の publish と食い違い Empty reply になる (実測)

上記に加え運用上の問題として、mitmweb を admin IP (172.31.242.2) だけに bind すると **host からも届かない**。
Docker Desktop (Mac) の `127.0.0.1:8081:8081` publish は container の別 IP (eth0 = devnet 側 172.22.0.2)
へ転送し、そこには listener が無いため docker-proxy が受理後に上流接続失敗 → `curl: (52) Empty reply from
server`。実測: dev から `--noproxy` 直叩きで `172.22.0.2:8081` は connection refused (mitmweb は admin IP に
しか bind していない) を確認。→ admin ネット (proxy-admin/静的 IP) は隔離にも寄与せず publish も壊すだけ。

## 最終形: container loopback bind + publish 無し + docker exec 踏み台 (経路で dev を締め出す)

`web_host=127.0.0.1` (container の loopback) に bind し、host publish は張らない。理由と保証:
- **dev 遮断は経路で確定**。`net.ipv4.conf.all.route_localnet=0` (既定。実測で確認) の下では、実 IF から
  入ってきた 127/8 宛は **martian として破棄**される。dev は別コンテナ (別 netns) なので secrets-proxy の
  loopback には原理的に到達できない。weak host model はローカルの *非* loopback アドレスにしか効かないので、
  admin IP 方式と違い NET_ADMIN でも迂回できない。publish が無いので LAN も届かない。
- **host アクセスは docker exec 踏み台**。host の `proxy-web-tunnel.py` が 127.0.0.1:8081 で待ち受け、接続毎に
  `docker compose exec -T secrets-proxy python /app/tcp-bridge.py 127.0.0.1 8081` を起こして socket↔loopback を
  中継する。host は docker socket を持つのでこれが打てるが、dev は docker socket を持たず `docker exec` できない。
  接続毎に exec を起こすので多少もたつくが debug 用途には十分。
- token (web_password 固定) は defense-in-depth として残す。
- 参考: `SO_BINDTODEVICE` で着信 IF を絞る案は、Docker Desktop では publish も dev も同じ devnet IF から
  入るため区別できず不成立 (かつ CAP_NET_RAW が要る)。loopback bind はこの問題を回避する。(2026-07-05)
