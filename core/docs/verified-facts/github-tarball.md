# GitHub archive tarball (`/archive/<ref>.tar.gz` および `gh api .../tarball/<ref>`)

confidence tag の凡例: [README](README.md)。

`install.sh` は `https://github.com/<slug>/archive/<ref>.tar.gz` を curl して展開し、展開先の
唯一の top dir を glob (`"$tmp"/*/`) で拾って `SRC` にする (ref 文字列を dirname 生成に自前で
使わない)。理由: このエンドポイントは branch/tag/SHA のどの ref 表記も受けるが、**tarball 内の
top dir 名は ref の種類によって生成規則が違う** ため。

## 実測 (2026-07-07, `hadolint/hadolint` — public repo。kit repo 本体は private のため代替に使用)

コマンド (3 パターンとも同一の URL 形。`--noproxy ""` はこの devcontainer 環境の secrets-proxy 経由
egress を強制するための local な事情で、install.sh 自体には不要):

```
curl -fsSL "https://github.com/hadolint/hadolint/archive/master.tar.gz"    -o branch-master.tar.gz
curl -fsSL "https://github.com/hadolint/hadolint/archive/v2.12.0.tar.gz"   -o tag-v2.12.0.tar.gz
curl -fsSL "https://github.com/hadolint/hadolint/archive/<full-sha>.tar.gz" -o sha-ref.tar.gz
```

`tar tzf <file> | head -1` (先頭行 = top dir) の観測結果:

| ref 表記                          | 渡した ref               | top dir                                    |
|-----------------------------------|---------------------------|---------------------------------------------|
| branch                            | `master`                  | `hadolint-master/`                           |
| tag (`v` prefix 付き)              | `v2.12.0`                 | `hadolint-2.12.0/` (**`v` が落ちる**)         |
| SHA (40桁 full)                    | `84c6da50b6431ba5d5e8b390253773878d628c19` | `hadolint-84c6da50b6431ba5d5e8b390253773878d628c19/` |

`[empirical]`

## 結論・install.sh への反映

- branch / SHA は `<repo>-<ref>` そのまま。tag は `v` prefix が (存在すれば) 剥がされた版が
  dirname に使われる — `<repo>-<ref を v-strip したもの>`。この非対称性を ref 文字列の加工で
  先読みするのは (v の有無だけでなく将来の GitHub 側規則変更にも) 脆い。
- したがって `install.sh` は dirname を文字列演算で組み立てず、展開先を `set -- "$tmp"/*/` の
  glob で「唯一存在するディレクトリ」として拾う (ref 表記に非依存)。tarball 直下に top dir が
  ちょうど 1 個だけ存在する前提は GitHub archive エンドポイントの一般的な出力形なので、この
  リポジトリ限定の話ではない。
- HTTP 404 (release 未作成時の既定 ref 解決失敗、typo した ref 等) は `curl -fsSL` が非 0 で
  fail-closed になり、`set -euo pipefail` で installer 全体が止まる (部分展開のまま次工程に
  進まない)。

## GitHub REST API tarball エンドポイント (`gh api repos/<owner>/<repo>/tarball/<ref>`)

root README の「curl \| bash を避けたい場合」の代替手順で使う。上の `/archive/<ref>.tar.gz` (codeload
直叩き) とは **top dir の生成規則が別物**。

### 実測 (2026-07-08, `hadolint/hadolint` と `octokit/octokit.js` — ともに public repo)

`https://api.github.com/repos/<owner>/<repo>/tarball/<ref>` は 302 で
`https://codeload.github.com/<owner>/<repo>/legacy.tar.gz/refs/heads/<ref>` (または `refs/tags/<ref>`) に
リダイレクトする (`legacy.tar.gz` エンドポイント。`/archive/<ref>.tar.gz` とは別パス)。`tar tzf | head -1`
で top dir を確認:

| repo | ref 種別 | ref | top dir |
|---|---|---|---|
| hadolint/hadolint | branch | `master` | `hadolint-hadolint-84c6da5/` |
| hadolint/hadolint | tag | `v2.12.0` | `hadolint-hadolint-b30b8b5/` |
| octokit/octokit.js | branch | `main` | `octokit-octokit.js-f2d3153/` |

`[empirical]`

### 結論

- top dir は常に `<owner>-<repo>-<short-sha>` (owner 込み、branch/tag で規則の差は無い —
  `/archive/<ref>.tar.gz` と違いここは対称。tag の `v` prefix は short SHA に置き換わるため
  そもそも残らない)。
- したがって `ikeyan/agent-devcontainer` を `gh api repos/ikeyan/agent-devcontainer/tarball/<ref> | tar xz`
  で展開すると top dir は `ikeyan-agent-devcontainer-<short-sha>/` になる。**`agent-devcontainer-*` という
  glob は owner prefix (`ikeyan-`) が無いためマッチしない** — root README の代替手順は
  `ikeyan-agent-devcontainer-*` を使うこと。
