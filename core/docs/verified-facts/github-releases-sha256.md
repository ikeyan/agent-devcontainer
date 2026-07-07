# GitHub Releases 成果物の sha256

confidence tag の凡例: [README](README.md)。

Dockerfile が GitHub Releases から取る成果物は version と sha256 を ARG で対固定し、取得後に
`sha256sum -c` で検証する (CDN 改竄・取り違えを fail-closed で弾く)。version 更新時は sha256 も
更新する。各成果物の sha256 の一次情報源 (上流が出すものを優先、無い物だけ自分で算出):

- **docker compose** `v5.2.0`: 上流 `releases/download/v5.2.0/checksums.txt`。
  `docker-compose-linux-x86_64` / `-aarch64` の行を引く。 `[docs]`
- **bitwarden cli** `cli-v2026.5.0`: GitHub API の release asset `.digest` (`sha256:…`)。
  `api.github.com/repos/bitwarden/clients/releases/tags/cli-v2026.5.0` の `.assets[].digest`。 `[docs]`
- **git-delta** `0.18.2`: 上流は checksum を出さず API の `.digest` も `null`。成果物 `.deb` を
  取得して `sha256sum` で算出 (取得元が壊れた/差し替わった時の検知が目的の TOFU 固定)。 `[empirical]`
- **zsh-in-docker** `v1.2.0`: スクリプトを `sh` に直結せず temp に落として `sha256sum -c` してから実行。
  上流は checksum を出さないので成果物を算出。 `[empirical]`

arch キーは `dpkg --print-architecture` (`amd64`/`arm64`) を正規にし、上流の資産名 (compose は
`x86_64`/`aarch64`) へ case で写す。secrets-proxy の bw は buildx の `TARGETARCH` を使う
(クロスビルドで実行アーキ一致の資産を選ぶため。意図的)。 `[empirical]`

hadolint は Release バイナリ取得をやめ、`hadolint/hadolint` image から `COPY --from` で焼く方式に
移行した (tag@digest 固定・Dependabot 追従。`docs/verified-facts/dependabot.md`)。CI
(`.github/workflows/check.yml`) は自前で Release バイナリを curl する (devcontainer と版を合わせる)。
