# redact サンドボックスの脅威モデルと不変条件

## 何から守るのか

redact サンドボックスは、生の秘密を含む mitmproxy フロー (`/input/raw.flows`) を子 claude に見せ、
ユーザーと対話して redact スクリプト (`extract/<app>/redact_flow.py`) を作らせる。子は
`--dangerously-skip-permissions` で動き、任意の Bash をユーザー確認なしに実行できる。したがって:

> **前提: 子 claude は node と同一 uid で動く敵対的プロセスであり、任意の Bash を回せる。**
> prompt injection された生フロー、または悪意あるアプリ由来の指示で、そう振る舞いうる。

守るべきは「生の秘密と認証情報を、レビュー対象の出力 (`extract/<app>/`) と Anthropic への正規 API
以外へ持ち出させない」こと。「子が善良に動く」前提は置かない。

## 不変条件 (変更ごとに上から照合する)

1. **node が読める平文の秘密をディスクに置かない。** 認証情報を子 home にもファイルにも置かず、
   env 経由かつ短命 access token のみにする。生 cred ファイルが存在しなければ tool が `cat` できない。
2. **同一 uid のプロセス env も読める。** `/proc/<pid>/environ` は同一 uid から読めるので、env scrub
   (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`) はツール子プロセスにしか効かない。main claude プロセスの
   認証情報はこれだけでは隠れない → 真の分離は別 uid / proxy (進行中の secrets-proxy 設計)。
   なお本サンドボックスでは scrub=1 が bubblewrap を要求し `cap_drop: ALL` (CAP_SYS_ADMIN 無し) では
   claude が起動不能になるため scrub は `0`。どのみち同一 uid 子には無効なので、この層に依存しない。
3. **durable な書込先を `extract/<app>/` 以外に作らない。** rw マウントや永続 volume を子 home に
   与えない (子 home は ephemeral)。生秘密を逃がす永続経路を断つ。
4. **egress は「転用できない宛先」だけに開ける。** 単に `api.anthropic.com` を許すと、子が自前の
   API キーで生フローを攻撃者の Anthropic アカウントへ upload できる。宛先ホストの allowlist だけでは
   防げない → proxy が host+path+auth を gate し、注入した正規 credential に縛る設計へ。
5. **マウント volume から実行可能な設定を継承しない。** auth volume の `~/.claude`
   (hooks/plugins/CLAUDE.md/skills/commands) を子に持ち込ませない (`--safe-mode`)。
6. **DNS も持ち出し経路。** dnsmasq は転送せず厳密名だけ応答する (`no-resolv` + `hostsdir`)。
   `server=/host/` の suffix 一致や、Docker embedded DNS の DNAT を filter が取りこぼす抜けに注意。
7. **外部値→パスは封じ込める。** app 名等は `.`/`..`/symlink を弾き `realpath` で領域内を確認する。
8. **fail-closed。** 必要な前提 (全許可名の解決 / placeholder 展開 / 必要 capability) が欠けたら
   起動を中止する。沈黙の部分成功を作らない。

## 静的に pin している不変条件

上のうち静的に検査できるものは `make check` が回す:
- `.devcontainer/core/bin/redact_invariants_check.sh`: 7 (app 名検証)、6 (dnsmasq 無転送)、settings の絶対パス、
  `__APP__` 展開の一貫性 — いずれも negative probe つき。
- `.devcontainer/core/Makefile`: 構文・compose・dnsmasq `--test`・JSON Schema 等。

「秘密の網羅性 (実際に伏せられているか)」は静的検査の対象外 — 本物のフローを見ながら子 claude が
対話的に確かめる領分であり、それがこのサンドボックスの存在意義 (`.devcontainer/core/bin/redact_selfcheck.py` 参照)。
