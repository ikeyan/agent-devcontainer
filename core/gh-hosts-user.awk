# gh の hosts.yml から user 名を取り出す。init-firewall.sh の gh seed 整合検査と、それを exercise する
# check-gh-seed の fixture が共有する (parser と seed 形式を prose でなく実行で結合する — drift すると
# postStart が毎回誤 mismatch する)。dev コンテナ内 (Linux, gawk/mawk) でだけ走るので regex の \t 可。
/^[ \t]*user:/ { sub(/^[ \t]*user:[ \t]*/, ""); gsub(/["']/, ""); print; exit }
