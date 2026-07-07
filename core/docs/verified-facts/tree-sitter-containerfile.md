# tree-sitter-containerfile / Dockerfile の AST node 名

confidence tag の凡例: [README](README.md)。

`bin/check_dockerfile_deps.py` が依拠する Dockerfile parse の node 構造。node 名を記憶で書いて外すのを
防ぐため、実際に parse して確認した名前を固定する。

## 検証方法・版

- 版: `tree-sitter==0.26.0` + `tree-sitter-containerfile==0.9.2` (core/pyproject.toml で
  `tree-sitter~=0.25` / `tree-sitter-containerfile~=0.9.2` を宣言、uv.lock で解決)。
- 確認: `Parser(Language(tree_sitter_containerfile.language()))` で下記を parse し、
  `node.type` / `node.named_children` / `node.start_point` を walk して確認した。 `[empirical]`

## node 構造 (このチェッカーが使う分)

- `source_file` の直下 named children に各命令が `from_instruction` / `copy_instruction` /
  `comment` … として並ぶ。 `[empirical]`
- `from_instruction` の named children:
  - `image_spec` (必ず 1 つ)。その named children:
    - `image_name` — 例 `node` / `ghcr.io/astral-sh/uv` / 先行ステージ名 `base`。
    - `image_tag` — 例 `:24.17.0-trixie` (省略可)。
    - `image_digest` — 例 `@sha256:61db…` (省略可)。**外部 image の digest 固定はこの child の有無で判定する。**
  - `image_alias` — `FROM … AS <name>` の `<name>` (`AS` が無ければ child ごと無い)。ステージ alias 定義。
  - フィールド名アクセス (`child_by_field_name("image_name")` 等) は **None を返す**。
    フィールドは公開されていないので `node.type` で named_children を絞る。 `[empirical]`
- `copy_instruction` の named children:
  - `param` — フラグ丸ごと 1 ノード。`.text` が `--from=uv` / `--chown=node:node` の全体を返す
    (中の `--` `=` は unnamed token。フラグ名/値の named child は無いので `.text` を文字列処理する)。
  - `path` — COPY のソース/宛先パス。
  - `COPY --from=<ref>` の `<ref>` は `param` の `.text` から `--from=` を剥がして得る。 `[empirical]`
- ステージ alias 参照 (`FROM base AS dev` / `COPY --from=base`) は、その alias を定義する
  `from_instruction` が **先行**していれば `image_alias` として既知になる (自己参照は不可)。 `[empirical]`
