# workspace

このディレクトリは agent-devcontainer の workspace root (コンテナ内では `/workspace`)。`.devcontainer/project/repos.txt` に宣言した repo を `make -C .devcontainer/core clone-repos` が直下へ clone する (宣言が無ければ単一 repo 運用: このディレクトリ自身が作業対象)。

- 直下に clone した repo は独立した git repo。変更・commit・push はその repo の中で行い、その repo の `CLAUDE.md` / `AGENTS.md` に従う。
- workspace root 自身を git 管理している場合、clone した repo は追跡しない (`clone-repos` が `.git/info/exclude` に登録する)。
