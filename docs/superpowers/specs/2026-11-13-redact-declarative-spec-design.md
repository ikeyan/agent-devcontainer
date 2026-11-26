# redact を任意の関数から宣言データに置き換える — 設計

## 背景

redact サンドボックスでは、子 claude が `extract/<app>/redact_flow.py` に `redact(s: str) -> str` を書く。この関数は、JSON に直列化した 1 flow 1 行の文字列を受け取り、秘密を伏せた文字列を返す。関数は任意の Python なので、欲しい性質は外から観察するしか確かめられない。

- 出力が JSON として読めること、1 flow = 1 行であること
- 受け入れるドメインの flow だけが残ること

`core/bin/redact_selfcheck.py` は合成 flow を流してこれらを観察している。ホストで flow を絞るアプリのために `SELFCHECK_DOMAINS` 宣言を ast で読む処理を足してからは、次の手当てが積み重なった。

- 宣言の書き方を禁止する規則: 複数回の代入、展開代入、累積代入、入れ子の root
- 境界を破る filter を捕まえる近傍ホストの probe

ikeyan/agent-devcontainer#44 では、このやり方に Codex の指摘が 18 本続いた。任意のコードの性質を観察で確かめる限り、抜け道を 1 つずつ塞ぐ作業は終わらない。

もう 1 つ問題がある。脅威モデル (`core/redact/SECURITY-MODEL.md`) は、子 claude を prompt injection されうる敵対的なプロセスとみなしている。いまの方式では、その子が書いた Python をサンドボックスの外で実行している。CI の selfcheck と、consumer が本番の出力を作る実行がそれに当たる。

## 目的

- アプリごとの redact を、kit が解釈する**宣言データ** (`extract/<app>/redact.toml`) にする。サンドボックスの外で動くコードを kit のコードだけにする。
- 守りたい性質を、観察ではなく engine の構造で成り立たせる。性質の検査は、engine に対して kit 側で 1 回行えば済むようにする。
- 既定を fail-closed にする。値を見せる例外は宣言に明示させ、認証情報らしい名前を見せるときは理由を書かせる。

## 守る対象 (ユーザー確認済み, 2026-11-11)

守るのは、アクセス権や価値を奪える情報に限る。パスワード、セッショントークン、API キー、セッション Cookie、OAuth のコードとトークンなどである。

氏名・メール・アカウント UUID・GitHub ハンドルのような、個人を特定する情報 (PII) は伏せる対象に入れない。consumer リポジトリは private で、redact 済みの出力を公開する予定はない。公開するときは、出力をさらに確認して人が黒塗りする。

アプリが PII を伏せたいなら、宣言で伏せることはできる (既存の判断を残すのは任意)。

## 現状の棚卸し (`ikeyan/tools` の `extract/*/redact_flow.py`, 2026-11-11 時点)

| アプリ | 方式 |
|---|---|
| shiraseru-bus (33 行) | 値の形 (JWT、FCM トークン) を、直列化した行に正規表現で当てて置き換える |
| nestle-jp-nbapp (132 行) | 値の形 5 種 (64 桁 hex、UUID、FCM、メール、16 桁数字) ＋ 伏せるヘッダ名・本文のキー名 ＋ Set-Cookie の値だけを伏せる処理。すべて直列化した行への正規表現 |
| claude-code (236 行) | ドメイン絞り込み ＋ 本文の構造化 walk (キーは残し、値を `<str>` / `<num>` / `<bool>` に置き換える) ＋ 文字列層の置き換え ＋ 全 flow を先読みして本人の UUID・メール・ハンドル・セッション ID を見つけ、全体から消す 2 パス処理 |

shiraseru-bus と nestle-jp-nbapp の redact 済み出力は、`extract/<app>/client/*/sample.jsonl` として commit されている。これは API クライアントの再生テストのデータで、テストは次を確かめる。

- クライアントが送る URL・method・JSON の要求本文が、サンプルと完全に一致すること
- 応答本文がクライアントの valibot スキーマで読めること

スキーマが使う検証は次のとおり (2026-11-11、`ikeyan/tools` の `extract/*/client` で `grep -rhoE 'v\.(number|string|boolean|regex)\b'` を数えた)。

| 検証 | 箇所 |
|---|---|
| `v.number()` | 58 |
| `v.string()` | 92 |
| `v.boolean()` | 25 |
| 16 進カラーの `v.regex` | 2 |

数値と正規表現の検証があるので、**この 2 本では本文の値を既定で伏せられない**。

いまの書き方には、規則が直列化の書式に依存している脆さがある。

- nestle は本文のキーを `\"key\":\"value\"` という文字列で探す。値が文字列でないときや、エスケープの層が変わったときには当たらない。
- nestle の Set-Cookie 処理は、mitmproxy が同名ヘッダを `", "` で畳む書式に依存している (`canon: facts/mitmproxy/headers-fold-and-cookie-accessors`)。
- 3 本とも、どの規則にも当たらない値はそのまま出る。例外は claude-code の本文だけ。

## 宣言の仕様 (v1)

宣言ファイルは `extract/<app>/redact.toml` に 1 つ置く。形式は TOML で、engine は標準ライブラリの `tomllib` (Python 3.11 以上) で読む。

```toml
version = 1

# 必須。残す flow の root ドメイン。各 root 自身とその subdomain だけを残す。
# 照合は実接続先 request.host に対して行う (Host ヘッダの偽装で騙されない)。
# ホストで絞らないときは domains = "all" と書く (報告に載る)。
domains = ["anthropic.com", "claude.com"]

[headers]
keep = ["x-request-id"]              # 値を見せるヘッダ名。kit の基本リストに足す

[cookies]
keep = []                            # 値を見せる Cookie 名

[query]
keep = ["lastSearchDateTime"]        # 値を見せる query のキー

[body]
mode = "values"                      # 必須。"values" か "structure"
mask = ["bpass", "installId"]        # mode = "values" のときだけ書ける。追加で伏せるキー
keep = []                            # mode = "structure" のときだけ書ける。値を見せるキー
text_body = "mask"                   # 文字の本文 (JSON・フォーム・SSE 以外) の扱い。"mask" (既定) か "keep"

[[pattern]]                          # 値の形。並びは結果に影響しない
label = "FCM_TOKEN_REDACTED"
regex = '[A-Za-z0-9_-]{8,}:APA91b[A-Za-z0-9_-]+'

[[expose]]                           # 認証情報らしい名前の値を見せる例外。理由は必須
where = "body"                       # "header" / "cookie" / "query" / "body"
name = "token_type"
reason = "enum (Bearer)。値に秘密を含まない"
```

### 宣言エラー

次はすべて宣言エラーとし、engine は何も出力せずに非 0 で終わる (fail-closed)。

- 未知のキー、型の誤り、列挙外の値 (`version` が 1 以外、`body.mode`・`body.text_body`・`[[expose]].where` が上の値以外)
- `domains` の欠落、空の配列、`"all"` 以外の文字列、重複・入れ子の root、小文字の dot 区切りでない root
- `body.mode` の欠落
- `mode = "values"` での `body.keep`、`mode = "structure"` での `body.mask`
- 正規表現のコンパイル失敗、空文字列に一致する正規表現 (`re.compile(regex).fullmatch("")` が一致する)
- `label` が `[A-Z][A-Z0-9_]*` に合わない、他の `[[pattern]]` か kit 共通の規則と重複する、場所のプレースホルダ (`masked`、`str`、`num`、`bool`) と同じ
- 認証情報らしい名前 (下の判定) を `*.keep` に書く
- 認証情報らしい名前でない名前を `[[expose]]` に書く (`*.keep` に書く)
- `[[expose]]` の `reason` の欠落か空文字列
- 同じ名前を、伏せる側 (`body.mask`) と見せる側 (`*.keep`・`[[expose]]`) の両方に書く

### 場所ごとの既定

| 場所 | 既定 | 見せる方法 |
|---|---|---|
| ヘッダ | 値を伏せる (`<masked>`) | kit の基本リスト ＋ `headers.keep` |
| Cookie (要求) / Set-Cookie (応答) | Cookie 名と Set-Cookie の属性は残し、値を伏せる | `cookies.keep` |
| Cookie・Set-Cookie の `=` のない片 | 片全体を `<masked>` にする | — |
| URL の query | キーは残し、値を伏せる | `query.keep` |
| URL の query の `=` のない片 | 片全体を `<masked>` にする | — |
| URL の scheme・host・port・path | 見せる (値の形の規則だけを当てる) | — |
| 本文 (`mode = "structure"`) | キーは残し、プリミティブ値を `<str>` / `<num>` / `<bool>` にする。`null` は残す | `body.keep` |
| 本文 (`mode = "values"`) | 値を見せる。認証情報らしい名前のキーと `body.mask` のキーは、型を問わず部分木ごと `<masked>` にする | — |
| フォーム形式の本文 | JSON 本文と同じ規則を、キーと値の組に当てる | 同上 |
| SSE の `data:` 行 | 行ごとに JSON 本文と同じ規則を当てる。JSON でない行は文字の本文と同じ扱い | 同上 |
| SSE の `event:`・`retry:` 行 | 値を見せる (値の形の規則は当てる) | — |
| SSE の `id:` 行、コメント行 (`:` 始まり)、その他のフィールド | 値を伏せる | — |
| 文字の本文 (上記以外) | 本文全体を `<masked text len=N>` にする | `body.text_body = "keep"` |
| バイナリの本文 | `<binary len=N>` | — |
| UTF-8 として復号できない値 (surrogate escape を含む文字列) | 値全体を `<masked>` にする | — |

- `body.text_body = "keep"` にした本文にも、値の形の規則は当てる。この宣言は報告に載る。
- 出力の `url` は mitmproxy の `pretty_url` で、URL の userinfo (`user:pass@`) を含まない (`canon: facts/mitmproxy/pretty-url-omits-userinfo`)。
- `=` のない片の扱いは、mitmproxy の解析に合わせた規則である。`=` のない片は、名前の位置に片全体が入る。要求の Cookie では、値が空の片 (`b=`) と同じ形になる。名前を残す規則のままだと、`=` なしで送られたセッショントークンがそのまま出る (`canon: facts/mitmproxy/headers-fold-and-cookie-accessors`)。

kit の基本リストは、値に認証情報が載らない標準ヘッダだけにする。候補は次のとおり。

- `content-type`、`content-length`、`content-encoding`
- `accept`、`accept-encoding`、`accept-language`
- `user-agent`、`host`、`date`、`server`、`connection`、`vary`、`priority`
- `cache-control`、`pragma`、`expires`、`etag`、`last-modified`
- `sec-fetch-*`

`location` と `referer` は URL を運び、OAuth のコードや query の token が載りうる。

### 名前の照合

| 照合 | 大小文字 |
|---|---|
| ヘッダ名 (どの規則でも) | 区別しない (HTTP の規則) |
| Cookie 名・query のキー・本文のキーを、伏せる側 (`body.mask`、認証情報らしい名前の判定) で照合する | 区別しない |
| Cookie 名・query のキー・本文のキーを、見せる側 (`*.keep`、`[[expose]]`) で照合する | 区別する |

照合の緩さを伏せる側にだけ寄せるのは、見せる範囲が意図より広がらないようにするためである。見せる側に書いたのに 1 件も一致しなかった名前は、`--report` の末尾に一覧で出す。

### 認証情報らしい名前の判定 (kit 共通)

キー名・ヘッダ名・Cookie 名・query のキーを、camelCase・`_`・`-`・`.` の境で単語に分ける。次のどちらかに当たれば、認証情報らしい名前とみなす。

- 単語のどれかが、次の集合に入る。
  - `password`、`passwd`、`secret`、`token`、`apikey`、`credential`、`credentials`
  - `auth`、`authorization`、`bearer`、`jwt`、`signature`
  - `session`、`sid`、`cookie`
  - `otp`、`pin`、`cvv`
- `api` と `key` が隣り合う (`api_key`、`X-Api-Key`)。

例: `idToken` は [id, token] なので当たる。`authorName` は [author, name] なので当たらない。

`code` は `statusCode` や `errorCode` に当たり、数値スキーマを壊す。OAuth のコードなどは、アプリの `body.mask` か値の形の規則で扱う。

### 値の形の規則 (`[[pattern]]`) と優先順位

engine が出力する**すべての文字列**に値の形の規則を当てる。対象は値と、次の名前である。

- ヘッダ名
- Cookie 名
- query のキー
- フォームのキー
- JSON のキー

kit 共通の規則の候補は、形の誤検出が少ない認証情報に限る。

- JWT
- `sk-ant-…`
- GitHub トークン (`ghp_` など、`github_pat_`)
- AWS のアクセスキー ID
- Slack トークン
- Google API キー
- PEM の秘密鍵ブロック

それぞれの形は、実装の前に一次情報で確かめて canon に記録する。kit 共通の規則の label は kit が固定する。JWT の label は、いまの shiraseru の出力と同じ `JWT_REDACTED` にする。

全規則 (アプリの `[[pattern]]` と kit 共通の規則) を**元の文字列**に対して照合し、当たった範囲の和集合を置き換える。重なる範囲は 1 つにまとめ、label は次の順で決める。

1. 範囲の始まりが最も早い規則
2. 始まりが同じなら、範囲が長い規則
3. それも同じなら、アプリの規則 (kit の規則より先)

規則を 1 つずつ順に当てると、先の規則が後の規則の対象の一部だけを書き換え、残りが出る。例えば nestle の 16 桁数字の規則が JWT や PEM ブロックの途中に当たると、kit の JWT の規則はもう当たらない。残りの部分がそのまま出て、事後条件も通ってしまう。

1 つの文字列に対する処理は次のとおり。

1. その場所の規則が「伏せる」なら、規則に当たったときはその label (例: `<JWT_REDACTED>`) だけを、当たらなければその場所のプレースホルダ (`<masked>`、`<str>` など) を出す。
2. その場所の規則が「見せる」なら、当たった範囲を label に置き換えて出す。

いまの出力のプレースホルダのうち、値の形の規則から出たもの (`<JWT_REDACTED>`、`<UUID_REDACTED>` など) は label で再現できる。名前の規則から出たものは `<masked>` に変わる。nestle の `sample.jsonl` には `<REDACTED>` が 22 箇所あり、次の位置に出る。

- `x-barista-id`
- `cookie`
- Set-Cookie の値
- `buser`
- `bpass`

再生テストは、サンプルの値をそのまま送り返したかを比べるだけなので通る。新旧の出力を比べると、ここに差分が出る。

### 実行時の事後条件

engine は出力する各行について、次を assert する。破れたら、その行を出さずに非 0 で終了する。

- 行は 1 つの JSON object で、キーはいまの出力と同じ集合に収まる: `method`、`url`、`request_headers`、`request_body`、`status_code`、`response_headers`、`response_body`。
- 行は `.encode("utf-8")` が通る。
- 出力したどの文字列 (名前を含む) にも、値の形の規則がもう当たらない。

規則はすべての文字列に当てているので、3 つ目が破れるのは engine の不具合のときだけである。この assert は、不具合で出力が漏れないための防御である。

## engine の振る舞い

新しい engine は `core/redact/redact.py`。いまの `flows_to_ndjson.py` の読み取りループ (`iter_flow_dicts`) とドメイン判定 (`host_in_domains`) を引き継ぐ。

```
python3 .devcontainer/core/redact/redact.py extract/<app>/redact.toml < raw.flows > out.jsonl
python3 .devcontainer/core/redact/redact.py --check  extract/<app>/redact.toml   # 宣言の検証だけ
python3 .devcontainer/core/redact/redact.py --report extract/<app>/redact.toml   # 値を見せる箇所の一覧
```

- **stdout には NDJSON の行だけを書く。** 診断・警告・`--report` の一覧は stderr に書く。
- **出力の形は変えない。** ヘッダは「名前 → 値」の dict のまま出す。同名ヘッダは、値を 1 つずつ伏せてから、いまと同じ `", "` で畳む。`ikeyan/tools` の `bin/flows.ts` と `client/test-helpers.ts` はそのまま動く。
- **ヘッダ・Cookie・query は 1 値ずつ扱う** (`canon: facts/mitmproxy/headers-fold-and-cookie-accessors`)。
  - ヘッダは `headers.fields` / `get_all` で 1 値ずつ取る。
  - 要求の Cookie は、`headers.get_all("cookie")` の各値を `;` で分け、片ごとに生の文字列で `=` の有無を見る。`request.cookies` は `=` のない片と値が空の片を同じ形で返すので使わない。
  - Set-Cookie は `response.cookies` を使う。`=` のない片は値が `None` になり区別できる。
- **query は生の文字列のまま扱う。** `&` で区切り、片ごとに最初の `=` でキーと値に分ける。キーは percent-decode して照合する。値は伏せる部分だけを差し替え、伏せない部分は元のバイト列のまま残す。再生テストは URL を文字列として完全一致で比べるので、何も伏せない URL は元と 1 文字も変わらないようにする。
- **JSON 本文は解析して再直列化する。** 区切りは `(",", ":")`、`ensure_ascii=False`、`allow_nan=False`。数値は Python の `json` が往復させる値で出す。JavaScript の `JSON.parse` で読んで同じ値になり、表記は変わりうる。再生テストは JSON として比べるので影響しない。
  - キーの重複がある JSON は、文字の本文として扱う (`object_pairs_hook` で検出する)。`json.loads` は後の値だけを残すので、解析すると重複した値が黙って消える。
  - `NaN`・`Infinity`・範囲外の数 (`1e999`) を含む JSON は、文字の本文として扱う (`parse_constant` で拒否する)。`json.dumps` は既定でこれらを非標準の `NaN` / `Infinity` として書き戻し、`JSON.parse` で読めない行になる (`canon: facts/python/json-loads-accepts-nan-infinity`)。
- **本文の種類の判定 (上から順に):**
  1. バイナリ: content-type が文字の型でないか、`get_content()` のバイト列が UTF-8 として読めないか、NUL を含む。文字の型は `text/*`、`application/json`、`*+json`、`application/javascript`、`application/xml`、`*+xml`、`application/x-www-form-urlencoded`。`get_text(strict=False)` の戻り値では判定できない。content-type が無ければ latin-1 でどんなバイト列も読み、`None` も surrogate escape も返さないためである (`canon: facts/mitmproxy/get-text-never-signals-binary`)。
  2. SSE: content-type が `text/event-stream`。
  3. フォーム: content-type が `application/x-www-form-urlencoded`。
  4. JSON: content-type に依らず解析を試み、最上位が object か array のときだけ JSON 本文とする。最上位がスカラーの本文 (例: `123456` という OTP、`"opaqueToken"`) はキーを持たないので、名前の規則が働かない。
  5. それ以外は文字の本文。
- **ドメイン絞り込みの検証規則は、いまの `declared_domains` と同じにする。** 宣言は TOML のデータから読む。

## 安全性 (`core/redact/SECURITY-MODEL.md` との照合)

前提は「子 claude は node と同一 uid の敵対的プロセス」。

- **外で動くコードが kit のコードだけになる。** 宣言は TOML のデータで、engine は読むだけで実行しない。子が書いたものが外で実行される経路は、redact については消える。
- **サンドボックスはリポジトリに書かない (新設)。**
  - いまは `extract/<app>/` を rw でマウントしている (`core/redact/compose.yaml`)。ここには、外で書いた API クライアントやテスト (`client.ts`、`*.test.ts`) も置かれ、外の vitest が実行する。サンドボックスがここに TS を書けば、外で実行されうる。
  - launcher (`core/bin/redact-flow`) は、`mktemp -d` で作業ディレクトリを作り、既存の `extract/<app>/redact.toml` をそこへ写す。サンドボックスには、この作業ディレクトリだけを rw でマウントする。`extract/<app>/` は ro にする。
  - サンドボックスの終了後、launcher は作業ディレクトリの `redact.toml` だけを扱う。
    1. `redact.toml` が通常ファイルである (symlink でない) ことを lstat で確かめ、`O_NOFOLLOW` で開いて読む。
    2. 読んだ内容を `redact.py --check` に通す。
    3. 写し先 `extract/<app>/redact.toml` を、`core/bin/redact-flow` の app 名検証を通った `<app>` から作り、`realpath` で `extract/` の配下にあることを確かめる。
    4. 1〜3 が通ったときだけ、読んだ内容を写し先に書く。通らなければ写さず、作業ディレクトリのパスと理由を表示して非 0 で終える (作業ディレクトリは残す)。
  - 作業ディレクトリの他のファイルは、どこにも写さない。写した後は作業ディレクトリを消す。
- **値を見せる例外は宣言に現れる。** 次の宣言は `--report` に一覧で出る。レビューで見るのは「どこを見せているか」の一覧だけになる。
  - `[[expose]]`
  - `domains = "all"`
  - `body.text_body = "keep"`
  - `body.mode = "values"`
- **残る危険:**
  - `mode = "values"` の本文に、名前も形も未知の認証情報が載ると素通しする。shiraseru と nestle は、再生テストが本文の値を要するのでこの方式を選ぶ。
  - URL の path に載る認証情報 (署名付き URL、パスワード再設定のリンクなど) は、値の形の規則に当たらない限り素通しする。
  - アプリの正規表現は、サンドボックスの外で生の flow に当てる。破滅的なバックトラックを起こす正規表現は止まらなくなりうるが、影響は DoS に限られ、宣言のレビューで見える。

## 検査の作り替え

| いま | 作り替え後 |
|---|---|
| `redact_selfcheck.py` が各アプリの `redact_flow.py` を実行し、合成 flow で観察する | engine の性質を kit 側で 1 回検査する。各アプリについては宣言の検証と、合成 flow での実行 (smoke) だけを行う |
| `SELFCHECK_DOMAINS` を ast で読み、書き方の禁止規則を積む | 宣言はデータなので読み方は 1 つ。不正は宣言エラーで止まる |
| 近傍ホストの probe をアプリごとに流す | 固定のドメインで engine の単体テストにする |

engine の性質は、hypothesis (すでに `core/pyproject.toml` の依存にある) で無作為な flow と宣言に対して検査する。生成器は、上の「宣言エラー」「場所ごとの既定」「本文の種類の判定」の各行を次元として持つ。

- P1: 残した flow 1 本につき、JSON object の行がちょうど 1 行出る。
- P2: ドメイン絞り込みが dot 境界を守る。`not<root>`、`<root>.x.invalid`、Host ヘッダの偽装を落とす。
- P3: 見せるリストにないヘッダの値に入れた目印は、出力に現れない。
- P4: 「場所ごとの既定」の Cookie と query の行を満たす。生成器は `name=value` の片、`=` のない片、値が空の片を作る。
- P5: 何も伏せない URL は元と一致する。
- P6: `structure` では、本文のプリミティブ値は `body.keep` と `[[expose]]` 以外に現れない。
- P7: `values` では、認証情報らしい名前のキーの値が、深さと型と大小文字に依らず現れない。`[[expose]]` の名前だけは現れる。最上位がスカラーの本文は現れない。
- P8: 秘密の目印を、他の規則にも当たる文字列の内側や隣に埋めても (例: 16 桁の数字を含む JWT)、目印のどの部分も出力に現れない。
- P9: どの行も厳密な JSON として読め (`NaN`・`Infinity` を含まない)、UTF-8 に符号化できる。
- 宣言エラーの negative probe: 「宣言エラー」の節の各項目。

consumer 側の `check-redact-selfcheck` は、各 `extract/*/redact.toml` について次を行う。

- `--check` を回す
- smoke を回す
- `--report` を出す

## 移行

1. **kit: engine と検査を足す。** 次を追加する。selfcheck は、`redact.toml` を持つアプリには新しい検査を、`redact_flow.py` を持つアプリには従来の検査を当てる (並存期間)。
   - `core/redact/redact.py`
   - engine の性質の検査
   - 宣言のテンプレート `core/redact/redact.toml.template`
2. **kit: サンドボックスの手順を宣言に切り替える。** 次を更新する。
   - `core/redact/interview.md`
   - launcher の scaffold と、作業ディレクトリへの写しと写し戻し
   - `core/redact/entrypoint.sh` の存在確認
   - `core/redact/compose.yaml` のマウント (`extract/<app>/` を ro に、作業ディレクトリを rw に)
   - `core/redact/SECURITY-MODEL.md` の記述
3. **tools: 3 アプリを移す。** 下の宣言の素案から始める。新旧の出力の差分は、Makefile のターゲット `redact-compare APP=<app> RAW=<生 flow のパス>` で確かめる。`RAW` は、いま `redact-flow` に渡しているのと同じ mitmproxy の flow ファイル (または HAR) である。生 flow はリポジトリに置かないので、比較は生 flow を持っているユーザーの手元で行う。commit 済みの `sample.jsonl` は作り直さない。
4. **kit: 任意の関数の経路を消す。** 3 アプリすべての `redact.toml` が `--check`・smoke・`redact-compare` を通り、ユーザーが差分を承認したら行う。次を削除し、`extract/*/redact_flow.py` が存在すること自体を検査でエラーにする (任意のコードへの後戻りを防ぐ)。
   - `core/redact/redact_flow.template.py`
   - `core/redact/flows_to_ndjson.py` の `redact` 引数
   - `core/bin/redact_selfcheck.py` の旧検査

### 3 アプリの宣言の素案

shiraseru-bus: JWT は kit 共通の規則で、`authorization` ヘッダは既定で伏せる。次の名前は、認証情報らしい名前の判定 (単語 `token`) に当たる。

- `idToken`
- `refreshToken`
- `deviceToken`

アプリ固有に残るのは、FCM の形と query の 1 キーである。`domains` は生 flow のホストの一覧を見て決める。決まるまでは `"all"` にする (報告に載る)。

```toml
version = 1
domains = "all"
[query]
keep = ["lastSearchDateTime"]
[body]
mode = "values"
[[pattern]]
label = "FCM_TOKEN_REDACTED"
regex = '[A-Za-z0-9_-]{8,}:APA91b[A-Za-z0-9_-]+'
```

nestle-jp-nbapp: 既存の判断をそのまま写す (PII の規則を減らすのは任意)。いまの capture の filter が `~d \.nestle\.jp$` なので、`domains` は `nestle.jp` にする。

- `x-barista-id`・`x-barista-auth` は、既定で伏せる。
- 要求の `cookie` は、いまは値全体を伏せている。新しい既定では Cookie 名が残り、値だけが伏せられる。
- Set-Cookie は、名前と属性を残して値を伏せる既定が、いまの挙動と同じになる。
- ログイン Web (ndgapp-web) の HTML・JS・CSS の本文は、既定の `text_body = "mask"` で伏せる。中身を見たいなら `text_body = "keep"` にする (報告に載る)。

FCM の規則は、いまの nestle の正規表現 (`{6,}`) をそのまま写す。shiraseru のもの (`{8,}`) とは別の規則である。

```toml
version = 1
domains = ["nestle.jp"]
[body]
mode = "values"
mask = ["buser", "bpass", "emailAddress", "installId", "nickname"]
[[pattern]]
label = "TOKEN_REDACTED"
regex = '(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-f])'
[[pattern]]
label = "UUID_REDACTED"
regex = '(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
[[pattern]]
label = "FCM_TOKEN_REDACTED"
regex = '[A-Za-z0-9_-]{6,}:APA91b[A-Za-z0-9_-]+'
[[pattern]]
label = "EMAIL_REDACTED"
regex = '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
[[pattern]]
label = "NUMERIC_ID_REDACTED"
regex = '(?<!\d)\d{16}(?!\d)'
```

claude-code: 先読みで値を消す処理はなくす (伏せる対象は「守る対象」の節のとおり)。

```toml
version = 1
domains = ["anthropic.com", "claude.com"]
[body]
mode = "structure"
keep = ["grant_type", "client_id", "scope"]
[[expose]]
where = "body"
name = "token_type"
reason = "enum (Bearer)。値に秘密を含まない"
[[expose]]
where = "body"
name = "token_type_hint"
reason = "enum (access_token / refresh_token)。値に秘密を含まない"
```

## 実装の前に確かめること

- redact イメージの `python3` の版。3.11 以上であることを確かめて canon に記録する。足りなければ、イメージの Python を上げるか形式を変えるかを、その時点で決め直す。
- SSE の応答が flow ファイルにどう記録されるか (`canon: facts/mitmproxy/response-streaming`)。
- kit 共通の値の形の規則に入れる各トークンの形式。一次情報で確かめて canon に記録する。

## 却下した案

物差しは次の 3 つ: 外で子のコードが実行されうるか、守る性質を構造で保証できるか、既存の再生テストが通るか。

- **Python の関数を組み合わせる eDSL:** 仕様を得るにはモジュールを実行する必要があり、同じファイルに任意のコードを書ける。外で子のコードが実行されうる。
- **本文を全アプリ既定で伏せる:** 外のコードは kit だけで、性質も構造で保証できるが、shiraseru と nestle の再生テストとスキーマ検証が通らない (棚卸しの節)。
- **先読みで見つけた値を全体から消す操作:** 使い道が PII だけで、守る対象から外れた。
- **直列化した行に正規表現を当てる (いまの方式):** 外で子のコードが実行され、規則が JSON のエスケープと mitmproxy の畳み方に依存し、当たらないときは素通しする。
- **`extract/<app>/redact/` だけを rw でマウントし、終了後に `redact.toml` 以外を削除する:** サンドボックスがリポジトリの中に書き続けるので、削除の分岐 (ファイル・ディレクトリ・symlink)、consumer 側でのはみ出しの検出、vitest・tsc の対象からの除外が要る。作業ディレクトリから写し戻す方式は、写し戻しの 1 経路だけで済む。
- **任意コードへの逃げ道:** 足りない表現は、このリポジトリで engine に基本操作として足し、レビューを通す。
