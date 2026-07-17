# Linux capabilities (`cap_drop: ALL` 下)

confidence tag の凡例: [README](README.md)。

- 別 uid 所有ファイルへの **書込** は `DAC_OVERRIDE`、**chown** は `CHOWN`、別 uid の `0600` の
  **読取** は `DAC_READ_SEARCH` が要る。`drop ALL` したら特権操作ごとに必要 cap を列挙して付ける
  (または、その操作を所有 uid 側で行う)。 `[man][review][empirical]`
- `iptables -m set` (ipset マッチ) は `xt_set` 拡張が **raw socket** を開いて ipset に問い合わせる
  (`socket(AF_INET, SOCK_RAW|SOCK_CLOEXEC, IPPROTO_RAW)` → `getsockopt(SOL_IP, SO_IP_SET, …)`) ため
  **`CAP_NET_RAW`** が要る。無いと `socket()` が EPERM → `iptables: Can't open socket to ipset` で
  init-firewall.sh が落ち devcontainer が起動しない (実機確認)。ipset の作成/追加・ルール投入自体は
  `CAP_NET_ADMIN`。出典: iptables `extensions/libxt_set.h` `get_version()`。 `[docs(source)][empirical]`
- `cap_drop: ALL` の container **root** は、他 uid 所有の `/proc/<pid>/environ` (0400) を**読めない**
  (`DAC_OVERRIDE`/`DAC_READ_SEARCH` 無し。environ は加えて ptrace アクセス検査があり `SYS_PTRACE` も
  絡む)。同 uid のプロセスなら読める。検証: dev と同じ cap 構成
  (drop ALL + NET_ADMIN/NET_RAW/SETUID/SETGID) の container で PID 1 を uid 1000 にし、
  `exec -u 0 cat /proc/1/environ` → `Permission denied`、`exec -u 1000` → 成功 (podman 5.4.2)。
  → sudo で root 化したスクリプトへ compose の environment 値を渡すには sudoers の `env_keep` を
  使う (environ 読取は経路にならない)。 `[empirical]`
