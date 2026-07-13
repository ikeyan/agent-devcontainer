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

## `run:` の plain scalar で先頭の bare `!` は消える (YAML tag indicator)

- YAML の plain scalar 先頭の `!` は **non-specific tag の指定子として解釈され、値から消える**。
  `run: ! grep ...` と書くと step には `grep ...` だけが渡り、否定 (`!`) が silent に脱落する。
  実測: `yaml.safe_load` で `!` が消失し `grep ...` だけが残ることを確認済み。`[empirical]`
- 対処: `!` 先頭のコマンドは block scalar (`run: |`) にするか、引用する。より良いのは、否定 grep の
  ような検査を workflow の run に直書きせず Makefile ターゲットに置くこと (レシピ内では YAML の
  制約が消える。例: ルート `Makefile` の `check-placeholder`)。
