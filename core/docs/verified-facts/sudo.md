# sudo / sudoers

confidence tag の凡例: [README](README.md)。

## per-command Defaults (`Defaults!cmnd`) と env_keep

- sudoers の Defaults はコマンド単位に束縛できる: 文法は `Default_Type ::= ... | 'Defaults!' Cmnd_List`
  で、Parameter_List は `+=` を含む全形 (`env_keep += "VAR"` 等) を取れる。per-command entry は
  コマンド引数を含められない (引数が要るなら Cmnd_Alias を定義して参照する)。man 内の実例にも
  command 束縛 Defaults がある (`Defaults!/bin/echo !log_stdin`, `Defaults!PAGERS noexec`)。
  出典: sudo-project/sudo `docs/sudoers.man.in` (Default_Type 文法と EXAMPLES)。 `[docs(source)]`
- `Defaults!/usr/local/bin/init-firewall.sh env_keep += "PROJECT_GH_USER"` +
  NOPASSWD 2 行の形は `visudo -c -f` が「parsed OK」と判定する (visudo (sudo 1.9 系) で実測)。 `[empirical]`
- 「per-command の env_keep が env_reset を実際に貫通して環境変数を残す」semantics は
  core/Dockerfile (dev ステージ、sudoers 節) の build 時 probe が毎ビルドで実測する:
  `/usr/bin/env` に束縛した probe 変数を node → `sudo -n /usr/bin/env` で貫通確認し、probe 用
  sudoers は消す。CI の build-images job がこの probe を毎 PR 実行する = 実測が回帰 pin に
  なっている。 `[empirical]`
- 関連: cap_drop:ALL の container root は他 uid の `/proc/<pid>/environ` を読めないため、
  「sudo に env を通す」には env_keep が唯一の実用経路 (capabilities.md 参照)。
