# devcontainer 再起動後の podman boot ID 不一致の根絶 (/tmp tmpfs 化)

- 状態: 設計確定 (実装前)
- 日付: 2026-07-12
- 関連: `.devcontainer/core/PODMAN.md`, `.devcontainer/core/compose.yaml`, `.devcontainer/core/docs/verified-facts/podman.md`, `.devcontainer/core/docs/verified-facts/docker.md`
- 実装先: kit repo [`ikeyan/agent-devcontainer`](https://github.com/ikeyan/agent-devcontainer)。このリポジトリ側の変更はゼロで、installer 再実行または週次 `devcontainer-kit-update.yml` の PR で同期を受け取る。

## 目的

mac 再起動・Docker Desktop 再起動のあと devcontainer を再開すると podman が boot ID 不一致で起動不能になる問題を、検知・修復でなく発生条件の除去で根絶する。

## 何が問題か (機構、この環境で実測済み)

podman 5.4.2 の `libpod.(*Runtime).checkBootID` は、libpod tmpdir の alive ファイルに boot ID を記録し、起動時に `/proc/sys/kernel/random/boot_id` と照合する。不一致だと hard error になる:

```
Error: current system boot ID differs from cached boot ID; an unhandled reboot has occurred. Please delete directories "/tmp/storage-run-1000/containers" and "/tmp/storage-run-1000/libpod/tmp" and re-run Podman
```

発生条件の連鎖:

1. dev コンテナは `XDG_RUNTIME_DIR` 未設定なので、c/storage の runroot と libpod tmpdir は `/tmp/storage-run-1000/` 配下へフォールバックする (kit ledger podman.md で確定済み)。
2. dev コンテナの `/tmp` は overlay ルート FS 上にあり (tmpfs でない)、コンテナ stop→start を跨いで**残る**。
3. boot ID は Docker Desktop の Linux VM のものなので、VM 再起動 (mac 再起動・Docker Desktop 再起動) で変わる。
4. よって「VM 再起動 → 同じコンテナを start」で alive ファイルの旧 boot ID と現 boot ID が食い違い、podman が全コマンドで fail する。コンテナ recreate (rebuild) では `/tmp` が新品になるので起きない。

podman は「runtime dir は per-boot の tmpfs にある」ことを設計前提にしており (だから boot ID 不一致 = 想定外として hard fail する)、この環境ではその前提が崩れている。kit ledger podman.md の「runtime state は本来 ephemeral なので /tmp で問題ない」という記述はこの実測で反証された。

実測済みの補助事実:

- 上記エラーは alive ファイルに偽 boot ID を書くことで再現できる (可逆 probe)。
- エラーが名指しする 2 ディレクトリの削除で完全復旧する。graphroot は named volume `podman` 上なので image は無傷、alive は現 boot ID で再生成される。

## 設計

### 変更本体: core/compose.yaml の dev service に tmpfs mount

```yaml
tmpfs:
  - /tmp:mode=1777,size=1g
```

- 「/tmp は ephemeral」という Linux 一般の前提を回復する宣言的な修正。カーネル保証なので、起動経路 (devcontainer CLI 経由か素の `docker start` か) に依らず効く。
- docker の tmpfs mount はコンテナ stop で消える (per-start 保証)。podman が想定する「reboot で消える」(per-boot) より強い側に倒れる: alive ファイル不在は checkBootID でなく通常の reboot 後 refresh 経路に入り、正常に初期化される。
- `mode=1777` (sticky, world-writable) は /tmp の標準 semantics かつ docker の既定値。root 所有の mount でも uid 1000 の c/storage が自分の 700 サブディレクトリを作れるので、uid/gid オプションは不要。
- `size=1g` は明示上限 (未指定の既定はホスト RAM の 50%)。podman の runroot は locks/pid/mountpoint のみで極小、実データは volume 上の graphroot、大容量一時領域 `imageCopyTmpDir` は `/var/tmp` (ディスクのまま。RAM を食わない)。

外部契約の根拠 (実装時に kit ledger docker.md へ出典つきで追記する):

- compose-spec 05-services.md: サービスレベル `tmpfs` は短形式 `- <path>:<options>` を受け、options は `mode`,`uid`,`gid` 等のカンマ区切り (例 `- /data:mode=755,uid=1009,gid=1009`)。
- docker docs engine/storage/tmpfs.md: 既定 mode=1777、既定 size=ホスト RAM の 50%、`--tmpfs` のオプションは `size`/`mode`/`uid`/`gid`/`exec` 等、「When the container stops, the tmpfs mount is removed, and files written there won't be persisted」。

### 検査の pin: core/Makefile に `check-tmp-tmpfs`

`findmnt -no FSTYPE /tmp` が `tmpfs` であることを検証する。コンテナ内で走る実行時不変条件なので、compose 設定がどの層でドリフトしても session 開始 hook / CI の `make check` が捕まえる。副作用ゼロの純粋読取り。

boot ID 偽装 probe (alive 書換 → エラー再現 → 復旧) は podman の live 状態を触る副作用があるので定常検査にはせず、実装時の一回検証として手順と結果を ledger に記録する。

### ドキュメント / ledger 更新 (kit 側)

- `PODMAN.md`: boot ID チェックの機構、runtime dir が tmpfs 前提である理由、遭遇時の手動復旧 (`rm -rf /tmp/storage-run-1000/containers /tmp/storage-run-1000/libpod/tmp`) の節を追加。
- `docs/verified-facts/podman.md`: 「/tmp で問題ない」の記述を訂正し、`checkBootID` の実測事実 (エラー全文・対象ディレクトリ・偽装 probe・復旧検証) を追記。
- `docs/verified-facts/docker.md`: 上記 tmpfs 契約を追記。

### ロールアウト

1. kit repo に PR → merge。
2. このリポジトリへは installer 再実行か週次 PR で同期 (このリポジトリの手作業は同期の取り込みのみ)。
3. 効くのはコンテナ recreate (rebuild) 後。既存コンテナは recreate までは従来どおりで、遭遇時は上記の手動復旧を使う。

## 検討した代替案 (不採用)

- **XDG_RUNTIME_DIR=/run/user/1000 新設 + そこに tmpfs**: Linux 標準形だが、uid=1000 mount オプションと env 追加が要り、ledger 済みの「XDG 未設定で動く」姿勢の再検証まで波及する。得られる効果は /tmp 案と同じ。
- **ピンポイント tmpfs (/tmp/storage-run-1000)**: uid 埋め込みパスを compose にハードコードし、mount 先ディレクトリの所有権検査 (c/storage は fallback dir の所有者を検査する) とも干渉しうる。/tmp 全体案の下位互換。
- **postStartCommand で起動時 `rm -rf`**: コンテナ起動時は nested container が全滅しているので無条件削除自体は安全だが、devcontainer CLI 経由の起動でしか走らない手続き的修正。tmpfs が同じ保証を宣言的に与える。
- **SessionStart hook で検知・修復**: hook はコンテナ生存中にセッション毎・並行に走るため、動作中の nested container の runroot を消すリスクがある。「コンテナ起動につき一度」という正しい粒度はこの層にない。

## 検証計画 (実装時の事後条件)

1. kit 側で `docker compose config` が dev service に tmpfs mount をレンダリングすること。
2. rebuild 後のコンテナ内で `findmnt /tmp` が tmpfs であり、指定オプション (mode=1777, size=1g) が実際に載っていること (docs 記述の実機裏取り → ledger)。
3. rebuild 後に `podman info` / `podman run` が正常で、graphroot (volume) の image が残っていること。
4. コンテナ stop→start 後に `/tmp` が空で、podman が refresh 経路で正常初期化されること (boot ID 不一致の発生条件が消えたことの直接確認)。
5. `make check` が `check-tmp-tmpfs` を含んで緑であること。
