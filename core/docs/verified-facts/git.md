# git

confidence tag の凡例: [README](README.md)。

## merge-backend rebase の phantom-dirty (`core.checkStat=minimal` で回避)

- **現象**: この devcontainer (Docker Desktop の virtiofs/overlay FS 上) で `git rebase` (既定 = merge
  backend) が、作業ツリーがクリーンなのに `error: Your local changes to the following files would be
  overwritten by merge` を出して停止する (phantom-dirty)。対象ファイルは試行ごとに変わる。
  `git status` はクリーン・`.git/hooks` も smudge/clean filter も無く・手動 `git checkout` は正常、
  という切り分けから rebase 特有と判明。 `[empirical]`
- **原因**: merge backend の `unpack-trees` (`verify_uptodate`) は作業ツリー各ファイルの stat を
  **フル比較** (mtime/size に加え **ctime・ino・uid・gid・dev**) して「変更あり」を判定する。virtiofs/
  overlay ではこれらの余分な stat フィールド (特に ctime/ino) が内容不変でも揺れるため、git が
  「ローカル変更あり」と誤検知して checkout を拒否する。 `[docs][empirical]`
- **対処**: `core.checkStat=minimal` にすると比較が **size + 秒精度 mtime のみ**になり、揺れる
  フィールドを無視するので誤検知が消える。出典: `man git-config` の `core.checkStat`
  (`minimal` = ファイルの mtime と size だけを見る)。 `[docs][empirical]`
- **実測** (git 2.47.3。同一条件を各 10 回: `05cb5c4 + 固有3コミット` を `origin/main` に rebase):

  | config | phantom-dirty |
  |---|---|
  | 既定 (merge backend) | 9〜10 / 10 |
  | `rebase.backend=apply` | 10 / 10 (効かない) |
  | `core.trustctime=false` | 10 / 10 (ctime しか無視せず ino 等が残る) |
  | `core.fileMode=false` | 10 / 10 |
  | **`core.checkStat=minimal`** | **0 / 10** |

  `[empirical]`
- **配置は system (`/etc/gitconfig`)** にする: VS Code Dev Containers は起動時に host の `~/.gitconfig`
  を container の `~/.gitconfig` へコピー (上書き) する (container 内 `~/.gitconfig` に host 由来の
  identity や VS Code の `credential.helper` が現れるのが証拠) ため、user 設定 (`git config --global`)
  に焼いても起動時に消える。system config なら上書きされず、host 側が `core.checkStat` を設定して
  いない限り有効。`.devcontainer/core/Dockerfile` で `RUN git config --system core.checkStat minimal`。
  `[empirical]`
- 迂回策として `git am` (patch backend, `unpack-trees` を介さない) は当時通ったが、`rebase.backend=apply`
  は上表のとおり効かず不安定。恒久対処は `core.checkStat=minimal`。 `[empirical]`
