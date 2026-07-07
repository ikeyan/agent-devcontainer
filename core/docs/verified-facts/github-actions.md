# GitHub Actions (hosted runner)

confidence tag の凡例: [README](README.md)。

## ubuntu-24.04 runner 同梱ツール

- GitHub-hosted runner `ubuntu-24.04` (= `ubuntu-latest` 現行) は **Docker 28.0.4 / Docker Compose
  2.38.2 を標準同梱**する (docker/docker compose CLI とも追加インストール不要)。出典:
  actions/runner-images の `images/ubuntu/Ubuntu2404-Readme.md`
  (https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Readme.md)。
  取得日 2026-07-07、取得者: コントローラ。`[docs]`
  - この事実により `.devcontainer/core/Makefile` の `check-compose` 系 (`COMPOSE` 変数) や
    `check-redact-image` (`ENGINE` 変数) は CI で「docker/podman が無い」を skip する分岐を持つ必要が
    ない (Task 9b「Makefile 検査基盤の整理」)。CI で compose エンジンが無い状態は本来あり得ないので、
    無ければ `command -v docker`/`podman compose` 相当の呼び出しがそのまま自然にエラーになる設計にする。
