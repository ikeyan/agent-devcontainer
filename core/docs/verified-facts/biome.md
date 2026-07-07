# Biome

confidence tag の凡例: [README](README.md)。

## `files.includes` の negation (`!pattern`) はルートアンカー — ネストした同名パスには効かない

- **現象**: `files.includes` に `!http-cache` と書いても、ネストした
  `.devcontainer/podman/http-cache/` 配下のファイルは除外されない。除外されるのは
  プロジェクトルート直下の `http-cache/` だけで、サブディレクトリ内の同名パスには
  一致しない。除外するには **ルートからのフルパス**が必要 — 現在の tree では http-cache は kit
  re-home で core/ 配下にあるので、live な処方は `!.devcontainer/core/podman/http-cache` (biome.json も
  この値)。下の日付つき実測・発覚経緯は kit re-home 前の Task 3 当時のパス
  `.devcontainer/podman/http-cache` を使っている (鮮度規約: README「鮮度規約」)。 `[empirical]`
- **発覚経緯**: Task 3 で moby seccomp snapshot を `http-cache/...` から
  `.devcontainer/podman/http-cache/...` へ `git mv` した際、既存の `biome.json` には
  `!http-cache` (ルート直下向け) しか無く、`make check` の `check-biome` が移設後の
  snapshot ファイル (upstream からの pin 済み生データ、tab indent) を format 対象として
  拾ってしまいエラーになった。
- **実測** (biome 2.3.10、2026-07-07; パスは kit re-home 前の当時のもの): `.devcontainer/podman/http-cache/` 配下に
  tab-indent の JSON を置いた最小構成で `biome check` を実行。
  - `files.includes` が `["**", "!http-cache"]` のみ → 当該ファイルが format 違反として
    検出される (= 除外されていない)。
  - `files.includes` に `!.devcontainer/podman/http-cache` を追加 → 検出されなくなる
    (= 除外される)。
  - 本リポジトリの `biome.json` は両方を残している (`!http-cache` は元々ルート直下の
    別用途 dir (`http-cache/api.real-debrid.com` — real-debrid API spec snapshot) 向けで、
    そちらの除外に現に効いている**現用**の設定。ネストした `.devcontainer/podman/http-cache`
    の除外には寄与しない)。
- **教訓**: ディレクトリを `git mv` で移設するとき、既存の `!<basename>` ignore は
  移設先のネストしたパスには効かない。移設先の **フルパス** を新たな negation として
  追加する必要がある (本リポジトリでは `!.devcontainer/podman/seccomp.json` も同じ
  「フルパスで pin 済み生成物を除外する」規約に倣っている)。
- biome 公式 docs の glob 構文の該当記述は本セッションでは未参照 (ネット取得不可)。
  上記は実機実行による実測のみを根拠とする。
