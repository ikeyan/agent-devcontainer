# Dependabot (docker ecosystem) / OCI image の digest 固定

confidence tag の凡例: [README](README.md)。

devcontainer の image を `image:tag@sha256:<digest>` で固定しつつ Dependabot に自動更新させるための
確定仕様。過去に「Dependabot が uv (COPY --from) を追従している」と誤って信じていた (実際は未追従) の
再発防止。

## docker ecosystem は FROM 行だけを解析する — `COPY --from=` は追従しない

- Dependabot の docker ecosystem は Dockerfile を**行単位で走査し、`^FROM\s+` にマッチする行だけ**を
  依存として拾う。`COPY --from=<image>` の image は**拾わない** (tag も digest も更新されない)。
  出典: dependabot-core `docker/lib/dependabot/docker/file_parser.rb` の `parse` が
  `next unless FROM_LINE.match?(line)`、`FROM_LINE = %r{^#{FROM}\s+...}` (`FROM = /FROM/i`)。
  既知 issue: dependabot/dependabot-core#5103「Dependabot ignores image references in COPY」。 `[docs(source)]`
- 対策: COPY で焼くだけの image も **`FROM <image>:<tag>@sha256:<digest> AS <stage>` の名前付き
  ステージ**にし、`COPY --from=<stage>` で参照する。こうすると image は FROM 行に載るので追従対象に
  入る (本リポジトリの uv / hadolint がこの形)。名前付きステージ末尾の ` AS <name>` は parser の
  `NAME = /\s+AS\s+(?<name>[\w-]+)/` が食う。 `[docs(source)]`

## FROM の tag+digest は両方まとめて更新される

- `FROM_LINE` は `#{IMAGE}#{TAG}?(?:@sha256:#{DIGEST})?#{NAME}?` で **tag と digest の両方を捕捉**する
  (`TAG = /:(?<tag>...)/`、`DIGEST = /(?<digest>[0-9a-f]{64})/`)。 `[docs(source)]`
- updater は tag と digest を**同時に**書き換える。出典: dependabot-core
  `docker/lib/dependabot/shared/shared_file_updater.rb` `update_digest_and_tag` —
  `@sha256:<old>` → `@sha256:<new>` に gsub し、続けて `:<old_tag>` → `:<new_tag>` に gsub する
  (digest 空の tag-only image には新 digest を付す分岐もある = digest pinning)。
  よって `node:24.17.0-trixie@sha256:...` は新版で `node:<newtag>@sha256:<newdigest>` に更新される。 `[docs(source)]`

## multi-arch image は index (manifest list) の digest 1個で全 arch を指す

- OCI image index / docker manifest list を持つ image は、**index の digest を 1 個**固定すれば
  amd64/arm64 双方に効く (client が index を引いてから実行 arch の sub-manifest を選ぶ)。
  GitHub Releases 成果物 (arch ごとに別 sha256) とは異なり arch 分岐は不要。 `[docs][empirical]`
- index digest の取り方 (registry HTTP API v2): token を取り、manifest を
  `Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json`
  付きで引いて応答ヘッダ `Docker-Content-Digest` を読む。`content-type` が index/manifest.list なら
  それが index digest。例 (2026-07 時点):
  - `node:24.17.0-trixie` → `sha256:61db8992b5c481488fe236ea69fe94035ba73df76a474051ed2e9713f3a15e5a`
    (linux/amd64, arm64/v8, ppc64le, s390x)。Docker Hub は `auth.docker.io/token?service=registry.docker.io&scope=repository:library/node:pull`。
  - `ghcr.io/astral-sh/uv:0.11.21` → `sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946`
    (linux/amd64, arm64)。ghcr は `ghcr.io/token?service=ghcr.io&scope=repository:astral-sh/uv:pull`。
  - `hadolint/hadolint:v2.14.0-alpine` → `sha256:7aba693c1442eb31c0b015c129697cb3b6cb7da589d85c7562f9deb435a6657c`
    (linux/amd64, arm64)。 `[empirical]`
- 版更新は Dependabot が digest ごと差し替えるので、上記 digest を人手で追う必要はない (記録は取得手順の
  参照用。値は版が上がれば変わる)。 `[empirical]`

## hadolint バイナリを image から焼くときの前提

- `hadolint/hadolint` 公式 image の `/bin/hadolint` は **static バイナリ** (公式 `docker/Dockerfile`
  に `FROM scratch` 版があり libc 無しで動く = 完全 static が根拠)。よって musl (alpine) 版を
  glibc/trixie の image へ `COPY --from` してもそのまま動く。上流での配置は 0777 なので、焼く側で
  `--chmod=0755` に締める (同 uid の上書きを防ぐ)。 `[docs(source)]`

## `ignore:` で base image の版ポリシー (node=最新LTS / python=bugfix終了済み最新) を止める

- `ignore.versions` は Dependabot 全 ecosystem 共通で **Bundler の Gemfile 風 semver 範囲文字列**
  (`">= 25"` 等) を取る。docker ecosystem も例外ではなく、`.github/dependabot.yml` の docker block に
  `ignore: [{dependency-name, versions}]` を書けば該当 image の版上げ PR を止められる。出典: GitHub
  Docs `dependabot-options-reference` の `ignore` / `versions` フィールド記述
  (https://docs.github.com/en/code-security/dependabot/dependabot-options-reference)。 `[docs]`
- 一方、`FROM node:24.17.0-trixie` のような **suffix 付き tag (`-trixie` / `-slim` 等) が具体的にどう
  semver 範囲へマッピングされるか** は同ドキュメントに記載がない (`update-types` の説明も
  major/minor/patch の一般論のみで、tag の非数値 suffix をどう剥がして比較するかには触れない)。
  `[docs-silent]`
- 実機で確認した挙動 (本 kit): Dependabot は `node:24.17.0-trixie` → `node:26.4.0-trixie`
  (ikeyan/agent-devcontainer PR #6) および `python:3.12-slim` → `python:3.14-slim` (同 PR #8) を
  「バージョン更新」として自律的に提案した。つまり suffix (`-trixie`, `-slim`) は固定文字列として
  保持したまま、tag 先頭の数値部分 (`24.17.0` → `26.4.0` / `3.12` → `3.14`) だけを semver として
  数値的に比較している。 `[empirical]`
- house choice: suffix 付き tag の semver 意味論が非公開 (`[docs-silent]`) である以上、
  `update-types: ["version-update:semver-major"]` のような **update-types による粒度指定には
  依拠しない**。代わりに `ignore.versions` の **明示的な数値範囲** (`">= 25"` / `">= 3.13"`) で
  止める方式を採る。範囲文字列の構文自体は `[docs]` の保証内だが、それが「tag 先頭の数値部分」に
  効くという前提は `[empirical]` (PR #6/#8 の実測) にのみ依存していることを忘れないこと。
