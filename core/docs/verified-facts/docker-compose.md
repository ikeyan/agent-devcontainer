# docker compose バイナリの供給元

confidence tag の凡例: [README](README.md)。

## `docker/compose-bin` image のバイナリ == GitHub Releases のバイナリ (byte 一致)

- `docker/compose-bin:<tag>` (Docker Hub の OCI image。中身は単一 static バイナリ `/docker-compose` のみ)
  のバイナリは、GitHub Releases の `docker-compose-linux-<arch>` と **byte 完全一致**する。よって供給元を
  GitHub Releases (curl + 手動 sha 固定 + arch case) から named FROM ステージ
  (`FROM docker/compose-bin:<tag>@sha256:<index-digest> AS compose` → `COPY --from=compose /docker-compose`)
  へ替えても機能差はゼロで、**Dependabot 追従 + multi-arch index digest 1 個で amd64/arm64 両対応**の利点
  だけ得られる (uv / hadolint と同形。理由は `dependabot.md`)。 `[empirical]`
- 検証 (2026-07-06, arm64): `podman pull docker.io/docker/compose-bin:v5.2.0` → `podman export` した
  `/docker-compose` (30448011 bytes) の sha256 =
  `739de570a0adf5eab12830db980f549fb5f44ad6b266e1e43e20f6f9df7cbcca` が、Dockerfile にピン済みの
  GitHub Release arm64 SHA (`DOCKER_COMPOSE_SHA256_arm64`) と一致。取り出したバイナリの
  `docker-compose version` = `Docker Compose version v5.2.0`。 `[empirical]`
- index digest の取得 (registry HTTP API v2, `dependabot.md`「index digest の取り方」と同手順): Docker Hub の
  `auth.docker.io/token?service=registry.docker.io&scope=repository:docker/compose-bin:pull` で token を取り、
  `registry-1.docker.io/v2/docker/compose-bin/manifests/v5.2.0` を
  `Accept: application/vnd.oci.image.index.v1+json` 付きで引いて `Docker-Content-Digest` を読む
  → `sha256:54c280c16d23289af63a9391626e3d09ddcd1253d4a5eef1f6ed52a531168e91` (index)。
  版が上がれば Dependabot が digest ごと差し替えるので人手で追う必要はない (値は取得手順の参照用)。 `[empirical]`
