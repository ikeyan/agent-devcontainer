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

- **守るのは、アクセス権や価値を奪える情報**: パスワード、セッショントークン、API キー、セッション Cookie、OAuth のコードとトークンなど。
- **個人を特定する情報 (PII) 一般は対象外**: 氏名、メール、アカウント UUID、GitHub ハンドルなど。consumer リポジトリは private で、redact 済みの出力を公開する予定はない。公開するときは、出力をさらに確認して人が黒塗りする。
- アプリが PII を伏せたいなら、宣言で伏せることはできる (既存の判断を残すのは任意)。

## 現状の棚卸し (`ikeyan/tools` の `extract/*/redact_flow.py`, 2026-11-11 時点)

| アプリ | 方式 |
|---|---|
| shiraseru-bus (33 行) | 値の形 (JWT、FCM トークン) を、直列化した行に正規表現で当てて置き換える |
| nestle-jp-nbapp (132 行) | 値の形 5 種 (64 桁 hex、UUID、FCM、メール、16 桁数字) ＋ 伏せるヘッダ名・本文のキー名 ＋ Set-Cookie の値だけを伏せる処理。すべて直列化した行への正規表現 |
| claude-code (236 行) | ドメイン絞り込み ＋ 本文の構造化 walk (キーは残し、値を `<str>` / `<num>` / `<bool>` に置き換える) ＋ 文字列層の置き換え ＋ 全 flow を先読みして本人の UUID・メール・ハンドル・セッション ID を見つけ、全体から消す 2 パス処理 |

shiraseru-bus と nestle-jp-nbapp の redact 済み出力は、`extract/<app>/client/*/sample.jsonl` として commit されている。これは API クライアントの再生テストのデータで、テストは次を確かめる。

- クライアントが送る URL・method・JSON の要求本文が、サンプルと完全に一致すること
- 応答本文がクライアントの valibot スキーマで読めること

スキーマは `v.number()` 58 箇所、`v.string()` 92 箇所、`v.boolean()` 25 箇所、16 進カラーの `v.regex` 2 箇所を含む。したがって、**この 2 本では本文の値を既定で伏せられない**。

いまの書き方には、規則が直列化の書式に依存している脆さがある。

- nestle は本文のキーを `\"key\":\"value\"` という文字列で探す。値が文字列でないときや、エスケープの層が変わったときには当たらない。
- nestle の Set-Cookie 処理は、mitmproxy が同名ヘッダを `", "` で畳む書式に依存している (`canon: facts/mitmproxy/headers-fold-and-cookie-accessors`)。
- 3 本とも、どの規則にも当たらない値はそのまま出る。例外は claude-code の本文だけ。

## 宣言の仕様 (v1)

宣言ファイルは `extract/<app>/redact.toml` に 1 つ置く。未知のキー、型の誤り、正規表現のコンパイル失敗は、すべて宣言エラーとして実行を止める (fail-closed)。

```toml
version = 1

# 省略時は全 flow を残す。書いた場合は、各 root 自身とその subdomain だけを残す。
# 照合は実接続先 request.host に対して行う (Host ヘッダの偽装で騙されない)。
domains = ["anthropic.com", "claude.com"]

[headers]
keep = ["x-request-id"]              # kit の基本リストに足す。名前は大小を区別しない

[cookies]
keep = []                            # 値を見せる Cookie 名

[query]
keep = ["lastSearchDateTime"]        # 値を見せる query のキー

[body]
mode = "values"                      # "values" か "structure" (必須)
mask = ["bpass", "installId"]        # values のときだけ: 追加で伏せるキー
keep = []                            # structure のときだけ: 値を見せるキー
text = "mask"                        # JSON・フォーム以外の文字の本文: "mask" (既定) か "keep"

[[pattern]]                          # 値の形。上から順に照合する
label = "FCM_TOKEN_REDACTED"         # 出力は <FCM_TOKEN_REDACTED>
regex = '[A-Za-z0-9_-]{8,}:APA91b[A-Za-z0-9_-]+'

# 認証情報らしい名前の値を見せる例外。理由を書かせ、報告に載せる。
[[expose]]
where = "body"                       # "header" / "cookie" / "query" / "body"
name = "token_type"
reason = "enum (Bearer)。値に秘密を含まない"
```

### 場所ごとの既定

| 場所 | 既定 | 見せる方法 |
|---|---|---|
| ヘッダ | 値を伏せる (`<masked>`) | kit の基本リスト ＋ `headers.keep` |
| Cookie (要求) / Set-Cookie (応答) | Cookie 名と Set-Cookie の属性は残し、値を伏せる | `cookies.keep` |
| URL の query | キーは残し、値を伏せる | `query.keep` |
| URL の scheme・host・port・path | 見せる (値の形の規則だけを当てる) | — |
| 本文 (`mode = "structure"`) | キーは残し、プリミティブ値を `<str>` / `<num>` / `<bool>` にする。`null` は残す | `body.keep` |
| 本文 (`mode = "values"`) | 値を見せる。認証情報らしいキー名と `body.mask` のキーは値を伏せる (型を問わず部分木ごと `<masked>`) | — |
| フォーム形式の本文 | JSON 本文と同じ規則を、キーと値の組に当てる | 同上 |
| SSE の本文 | `data:` 行ごとに JSON 本文と同じ規則を当てる。JSON でない `data:` 行は文字の本文と同じ扱い | 同上 |
| 文字の本文 (上記以外) | 本文全体を `<masked text len=N>` にする | `body.text = "keep"` (値の形の規則だけを当てる。報告に載る) |
| バイナリの本文 | `<binary len=N>` | — |

kit の基本リストは、値に認証情報が載らない標準ヘッダだけにする。候補は `content-type`、`content-length`、`content-encoding`、`accept`、`accept-encoding`、`accept-language`、`user-agent`、`host`、`date`、`cache-control`、`pragma`、`expires`、`etag`、`last-modified`、`connection`、`vary`、`server`、`sec-fetch-*`、`priority`。`location` と `referer` は URL を運び、OAuth のコードや query の token が載りうるので入れない。

### 認証情報らしい名前の判定 (kit 共通)

キー名・ヘッダ名・Cookie 名・query のキーを単語に分けて判定する。区切りは camelCase、`_`、`-`、`.`。単語のどれかが次の集合に入れば、認証情報らしい名前とみなす。

`password`, `passwd`, `secret`, `token`, `apikey`, `auth`, `authorization`, `credential`, `credentials`, `session`, `sid`, `cookie`, `otp`, `pin`, `cvv`, `signature`, `jwt`, `bearer`

加えて、`api` と `key` が隣り合う並び (`api_key`、`X-Api-Key`) も該当する。

- 部分文字列ではなく単語で判定する。`authorName` は [author, name] なので当たらない。`idToken` は [id, token] なので当たる。
- `code` は入れない。`statusCode` や `errorCode` に当たり、数値スキーマを壊すため。OAuth のコードなどは、アプリの `body.mask` か値の形の規則で扱う。
- この判定に当たる名前は、`headers.keep` / `cookies.keep` / `query.keep` / `body.keep` に書くと**宣言エラー**になる。見せるには `[[expose]]` に理由つきで書く。

### 値の形の規則 (`[[pattern]]`) と優先順位

engine が出力する文字列には、すべて値の形の規則を当てる。対象は、ヘッダ値、Cookie 値、query 値、path、JSON のキーと文字列値、フォームの値、`text = "keep"` の本文。規則はアプリの `[[pattern]]` を上から順に当て、その後に kit 共通の規則を当てる。

kit 共通の規則の候補は、形の誤検出が少ない認証情報に限る。

- JWT
- `sk-ant-…`
- GitHub トークン (`ghp_` など、`github_pat_`)
- AWS のアクセスキー ID
- Slack トークン
- Google API キー
- PEM の秘密鍵ブロック

それぞれの形は、実装の前に一次情報で確かめて canon に記録する。

1 つの値に対する処理は次の順に決める。

1. その場所の規則が「伏せる」なら、値の形の規則に当たったときはその label (例: `<JWT_REDACTED>`) を、当たらなければその場所のプレースホルダ (`<masked>`、`<str>` など) を出す。
2. その場所の規則が「見せる」なら、値の中で規則に当たった部分を label に置き換えて出す。

いまの出力のプレースホルダ (`<JWT_REDACTED>`、`<UUID_REDACTED>` など) は label で再現できる。commit 済みの `sample.jsonl` と表記がずれない。

### 実行時の事後条件

engine は出力する各行について、次を assert する。破れたら、その行を出さずに非 0 で終了する。

- 行は 1 つの JSON object で、キーはいまの出力と同じ集合 (`method`, `url`, `request_headers`, `request_body`, `status_code`, `response_headers`, `response_body`) に収まる。
- 出力したどの文字列にも、値の形の規則がもう当たらない。

## engine の振る舞い

新しい engine は `core/redact/redact.py`。いまの `flows_to_ndjson.py` の読み取りループ (`iter_flow_dicts`) とドメイン判定 (`host_in_domains`) を引き継ぐ。

```
python3 .devcontainer/core/redact/redact.py extract/<app>/redact.toml < raw.flows > out.jsonl
python3 .devcontainer/core/redact/redact.py --check  extract/<app>/redact.toml   # 宣言の検証だけ
python3 .devcontainer/core/redact/redact.py --report extract/<app>/redact.toml   # 値を見せる箇所の一覧
```

- **出力の形は変えない。** ヘッダは「名前 → 値」の dict のまま出す。同名ヘッダは、値を 1 つずつ伏せてから、いまと同じ `", "` で畳む。`ikeyan/tools` の `bin/flows.ts` と `client/test-helpers.ts` はそのまま動く。
- **ヘッダ・Cookie・query は 1 値ずつ扱う。** `headers.fields` / `get_all`、`request.cookies` / `response.cookies` を使い、畳んだ文字列には正規表現を当てない (`canon: facts/mitmproxy/headers-fold-and-cookie-accessors`)。
- **query は生の文字列のまま扱う。** `&` と `=` で区切り、キーは percent-decode して照合する。値は伏せる部分だけを差し替え、伏せない部分は元のバイト列のまま残す。何も伏せない URL は、元と 1 文字も変わらない。再生テストは URL を文字列として完全一致で比べるので、再エンコードで表記が変わってはならない。
- **JSON 本文は解析して再直列化する。** 区切りは `(",", ":")`、`ensure_ascii=False`。数値は Python の `json` が往復させる値で出す。JavaScript の `JSON.parse` で読んで同じ値になり、表記は変わりうる。再生テストは JSON として比べるので影響しない。
  - キーの重複がある JSON は、文字の本文として扱う (`object_pairs_hook` で検出する)。`json.loads` は後の値だけを残すので、解析すると重複した値が黙って消える。
- **JSON・フォーム・SSE かどうかの判定:**
  - JSON: content-type に依らず解析を試みる (claude-code のいまの挙動と同じ)。
  - フォーム: content-type が `application/x-www-form-urlencoded` のとき。
  - SSE: content-type が `text/event-stream` のとき。
- **ドメイン絞り込みの検証規則は、いまの `declared_domains` と同じ。** 小文字の dot 区切りの tuple で、重複・入れ子を拒否する。違いは、ast からではなく TOML のデータから読むこと。

## 安全性 (`core/redact/SECURITY-MODEL.md` との照合)

前提は「子 claude は node と同一 uid の敵対的プロセス」。

- **外で動くコードが kit のコードだけになる。** 宣言は TOML のデータで、engine は読むだけで実行しない。子が書いたものが外で実行される経路は、redact については消える。
- **サンドボックスの出力を宣言ファイル 1 つに限る (新設)。** launcher (`core/bin/redact-flow`) は、サンドボックスの終了後に `extract/<app>/` の変更を調べる。`redact.toml` 以外が増えたり変わったりしていたら、警告して非 0 で終える。
  - `extract/<app>/` には、外で書いた API クライアントやテスト (`client.ts`、`*.test.ts`) も置かれ、外の vitest が実行する。いまは、サンドボックスがここに書いた TS が外で実行されうる。この経路も塞ぐ。
- **値を見せる例外は宣言に現れる。** `[[expose]]`、`text = "keep"`、`mode = "values"` は `--report` に一覧で出る。レビューで見るのは「どこを見せているか」の一覧だけになる。
- **残る危険:**
  - `mode = "values"` の本文に、名前も形も未知の認証情報が載ると素通しする。shiraseru と nestle は、再生テストのためにこの方式を選ぶ。この方式であることは宣言に明示され、報告にも出る。
  - URL の path に載る認証情報 (署名付き URL、パスワード再設定のリンクなど) は、値の形の規則に当たらない限り素通しする。
  - アプリの正規表現は、サンドボックスの外で生の flow に当てる。破滅的なバックトラックを起こす正規表現は止まらなくなりうるが、影響は DoS に限られ、宣言のレビューで見える。

## 検査の作り替え

| いま | 作り替え後 |
|---|---|
| `redact_selfcheck.py` が各アプリの `redact_flow.py` を実行し、合成 flow で観察する | engine の性質を kit 側で 1 回検査する。各アプリについては宣言の検証と、合成 flow での実行 (smoke) だけを行う |
| `SELFCHECK_DOMAINS` を ast で読み、書き方の禁止規則を積む | 宣言はデータなので読み方は 1 つ。不正は TOML の検証で止まる |
| 近傍ホストの probe をアプリごとに流す | 固定のドメインで engine の単体テストにする |

engine の性質は、hypothesis (すでに `core/pyproject.toml` の依存にある) で無作為な flow と宣言に対して検査する。

- P1: 残した flow 1 本につき、JSON object の行がちょうど 1 行出る。
- P2: ドメイン絞り込みが dot 境界を守る。`not<root>`、`<root>.x.invalid`、Host ヘッダの偽装を落とす。
- P3: 見せるリストにないヘッダの値に入れた目印は、出力に現れない。
- P4: Cookie の名前は残り、値は `cookies.keep` にない限り現れない。
- P5: query の値は `query.keep` にない限り現れない。何も伏せない URL は元と一致する。
- P6: `structure` では、本文のプリミティブ値は `body.keep` と `[[expose]]` 以外に現れない。
- P7: `values` では、認証情報らしい名前のキーの値が、深さと型に依らず現れない。`[[expose]]` の名前だけは現れる。
- P8: 出力のどこにも、値の形の規則がもう当たらない。
- 宣言エラーの negative probe: 未知のキー、型の誤り、不正な正規表現、入れ子の root、認証情報らしい名前を keep に書くこと、`version` の欠落。

consumer 側の `check-redact-selfcheck` は、各 `extract/*/redact.toml` について `--check` と smoke を回し、`--report` を出す。移行が終わったら、`extract/*/redact_flow.py` が存在すること自体をエラーにする。任意のコードへの後戻りを防ぐためである。

## 移行

1. **kit: engine と検査を足す。** `redact.py`、engine の性質の検査、宣言のテンプレート (`redact.toml.template`) を追加する。selfcheck は、`redact.toml` を持つアプリには新しい検査を、`redact_flow.py` を持つアプリには従来の検査を当てる (並存期間)。
2. **kit: サンドボックスの手順を宣言に切り替える。** `core/redact/interview.md`、launcher の scaffold、`entrypoint.sh` の存在確認、`SECURITY-MODEL.md` の記述、終了後の差分確認を更新する。
3. **tools: 3 アプリを移す。** 下の宣言の素案から始める。手元の生 flow に新旧を両方当てて差分を確かめる手順は、Makefile のターゲット (例: `redact-compare APP=… RAW=…`) として用意する。生 flow はリポジトリに置かない前提なので、この比較はユーザーの手元で行う。
4. **kit: 任意の関数の経路を消す。** `redact_flow.template.py`、`flows_to_ndjson.py` の `redact` 引数、`redact_selfcheck.py` の旧検査を削除する。`redact_flow.py` の存在を検査でエラーにする。

### 3 アプリの宣言の素案

shiraseru-bus: JWT は kit 共通の規則で、`authorization` ヘッダは既定で伏せる。`idToken`・`refreshToken`・`deviceToken` は認証情報らしい名前の判定 (単語 `token`) に当たる。アプリ固有に残るのは FCM の形と query の 1 キーだけになる。

```toml
version = 1
[query]
keep = ["lastSearchDateTime"]
[body]
mode = "values"
[[pattern]]
label = "FCM_TOKEN_REDACTED"
regex = '[A-Za-z0-9_-]{8,}:APA91b[A-Za-z0-9_-]+'
```

nestle-jp-nbapp: 既存の判断をそのまま写す (PII の規則を減らすのは任意)。`x-barista-id`・`x-barista-auth` は既定で伏せる。要求の `cookie` は、いまは値全体を伏せているが、新しい既定では Cookie 名が残り値だけが伏せられる。Set-Cookie は、名前と属性を残して値を伏せる既定がいまの挙動と同じになる。

```toml
version = 1
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

ログイン Web (ndgapp-web) の HTML・JS・CSS の本文は、既定の `text = "mask"` で伏せる。中身を見たいなら `text = "keep"` にする (報告に載る)。

claude-code: 先読みで値を消す処理はなくす (2026-11-11 の決定: 本人の UUID・メール・ハンドル・セッション ID は、単体でアクセス権を与えないので伏せない)。

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

- redact イメージの `python3` の版。宣言の読み込みに標準ライブラリの `tomllib` (Python 3.11 以上) を使う。版が足りなければ JSON に切り替える。確かめた結果は canon に記録する。
- `get_text(strict=False)` がバイナリの本文に何を返すか (12.3.1)。バイナリと文字の判定はこれに依る。
- SSE の応答が flow ファイルにどう記録されるか (`canon: facts/mitmproxy/response-streaming`)。
- kit 共通の値の形の規則に入れる各トークンの形式。一次情報で確かめて canon に記録する。

## 却下した案

- **Python の関数を組み合わせる eDSL:** 仕様を得るにはモジュールを実行する必要があり、同じファイルに任意のコードを書ける。外で子のコードを実行する経路が残る。
- **本文を全アプリ既定で伏せる:** shiraseru と nestle の再生テストとスキーマ検証が成り立たない (棚卸しの節)。
- **先読みで見つけた値を全体から消す操作:** 使い道が PII だけで、守る対象から外れた。
- **直列化した行に正規表現を当てる (いまの方式):** 規則が JSON のエスケープと mitmproxy の畳み方に依存し、当たらないときは素通しする。
- **任意コードへの逃げ道:** 足りない表現は、このリポジトリで engine に基本操作として足し、レビューを通す。
