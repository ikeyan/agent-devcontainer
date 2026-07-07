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
