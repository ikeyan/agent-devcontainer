#!/usr/bin/env bash
# devcontainer を実際に up して postStartCommand (init-firewall.sh の gh seed 整合検査 + firewall +
# install-proxy-ca.sh) まで end-to-end で完走するかを検査する。
#   usage: check_devcontainer_up.sh <workspace-folder> <expected-gh-user>
#
# なぜ要る: CI の build-images は image を build するだけで postStartCommand を実行しない。だが
# devcontainers CLI 経路でしか出ない runtime 契約 — overrideCommand が compose 宣言 entrypoint を
# 捨てる / cap_drop:ALL の root が node 所有パスを traverse できず EACCES 等 (docs/incidents.md) —
# は build では捕まらず、consumer の初 rebuild で初めて露見してきた。ここで実際に up して postStart
# まで走らせ、その class を回帰 pin する。要 docker + @devcontainers/cli。
#
# 隔離: <workspace-folder> をそのまま up せず、一意な basename (dcup-smoke-<pid>) の temp ディレクトリへ
# 複製して up する。devcontainers CLI が UID 調整で建てる image 名は cwd 由来
# (getFolderImageName = vsc-<basename(cwd)>-<sha256(cwd)>-uid; devcontainers-cli.md) で project 名では
# 隔離できない。basename を probe 専用の一意名にすることで、compose の <proj>-<svc> image も folder 由来の
# vsc-<proj>-<hash> image も全て dcup-smoke-<pid> token を持つ一意な *tag 名* になり、user が同じ folder で
# 開いている実 devcontainer の tag (vsc-<realfolder>-*) とは別名になる (up 中の mount stub 書込みも temp
# 側に閉じる)。COMPOSE_PROJECT_NAME も同名にして container/volume/network を隔離する (CLI は
# COMPOSE_PROJECT_NAME を最優先 — devcontainers-cli.md)。
#   image content は Dockerfile + baked な core/+project/ + build-arg だけで決まり workspace は bind mount
#   なので、probe image は user の実 image と *同じ content-addressable ID* を持ちうる。ゆえに cleanup は
#   ID (docker images -q) でなく *tag 名* で rmi する — 共有 ID の user tag を巻き込まないため。
#
# 前提: <workspace-folder> は scaffold 済み (project/ + devcontainer.json + 生成 Dockerfile) で
# project/.env の PROJECT_GH_USER が <expected-gh-user> と一致すること。secrets-proxy は template
# rules.yaml (BW item 源なし) なら bootstrap 無しで healthy になる (rules_bw.py: env/file だけの rules
# は Vaultwarden 不要) ので dev が起動し postStart に到達する。
set -uo pipefail
ws=${1:?usage: check_devcontainer_up.sh <workspace-folder> <expected-gh-user>}
expect_user=${2:?usage: check_devcontainer_up.sh <workspace-folder> <expected-gh-user>}
command -v devcontainer >/dev/null 2>&1 || { echo "@devcontainers/cli が要る (npm i -g @devcontainers/cli)" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker が要る (compose provider 込み)" >&2; exit 1; }
# ws を絶対パス化。cd 失敗 (存在しない/権限無し) を fail-closed で捕まえる — 空の wsabs を後段の
# `cp/tar "$wsabs/."` に渡すと "/." = ルート全体の複製になるため、ここで必ず止める。
wsabs=$(cd "$ws" 2>/dev/null && pwd) || { echo "workspace-folder に入れない: $ws" >&2; exit 1; }
[ -n "$wsabs" ] || { echo "workspace-folder の絶対パス化に失敗: $ws" >&2; exit 1; }

# devcontainer.json の場所は consumer (.devcontainer/) と kit dogfood (repo 直下) で異なる。複製前に
# source 側で存在を検証して fail-fast する (無駄な複製を避ける)。cfg は複製後に同じ相対パスで導出。
if   [ -f "$wsabs/.devcontainer/devcontainer.json" ]; then cfgrel=".devcontainer/devcontainer.json";
elif [ -f "$wsabs/devcontainer.json" ];              then cfgrel="devcontainer.json";
else echo "devcontainer.json が $wsabs (/.devcontainer) に無い" >&2; exit 1; fi

proj="dcup-smoke-$$"
tmproot=$(mktemp -d) || { echo "temp ディレクトリの作成 (mktemp -d) に失敗" >&2; exit 1; }
[ -n "$tmproot" ] || { echo "mktemp -d が空を返した" >&2; exit 1; }

cleanup() {
    local rc=$?   # トラップ発火時の終了ステータスを保存 (下で必要なら fail-closed に昇格)。
    # 停止前に dev コンテナが走らせている image id を控える (下の chown helper に使う)。
    local devcid devimg=""
    devcid=$(docker ps -aq --filter "label=com.docker.compose.project=$proj" --filter "label=com.docker.compose.service=dev" 2>/dev/null | head -1)
    if [ -n "$devcid" ]; then devimg=$(docker inspect -f '{{.Image}}' "$devcid" 2>/dev/null); fi
    # container/volume/network は compose が付ける exact project label で消す (prefix over-match なし)。
    docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1
    docker volume ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker volume rm -f >/dev/null 2>&1
    docker network ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker network rm >/dev/null 2>&1
    # devcontainer up は workspace の bind/volume mount 先 (.claude/settings.local.json, .venv 等) を
    # host 上に root 所有で作る → 実行 uid では rm -rf できない。docker を root で走らせ所有権を戻してから
    # 消す (docker は本 script の必須依存。chown には既定 cap の CHOWN で足りる)。
    if [ -n "$devimg" ]; then
        docker run --rm -u 0 --entrypoint chown -v "$tmproot:/t" "$devimg" -R "$(id -u):$(id -g)" /t >/dev/null 2>&1
    fi
    rm -rf "$tmproot"
    # image は tag 名で消す (ID=docker images -q だと共有 content-ID の user tag を巻き込む)。契約と出典は
    # docs/verified-facts/devcontainers-cli.md。untag で孤児化した probe image を後で ID 指定で掃くため、
    # untag する *前* に probe token tag が指す ID 集合を控える (= ownership scope; 時刻 delta でなく所有で
    # 限定するので共有 daemon の無関係な dangling を巻き込まない)。
    local probe_ids
    probe_ids=$(docker images -q --filter "reference=vsc-$proj-*" --filter "reference=$proj-*" --filter "reference=${proj}_*" 2>/dev/null | sort -u)
    # 子 (folder 系 uid image) を先、親 (compose 系) を後に untag して dangling を避け、separator anchor で
    # 別 PID probe との取り違えを防ぐ。rmi は tag 名なので共有 ID でも他 (user) tag は残る。
    docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=vsc-$proj-*" 2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
    docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=$proj-*" --filter "reference=${proj}_*" 2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
    # untag 後、控えた probe ID のうち *tag を全て失った (dangling 化した)* ものだけを ID 指定で消す。
    # user と content-ID を共有する image は user tag が残るのでスキップ (削除しない)。無関係プロセスの
    # dangling は probe_ids に無いので触らない。
    printf '%s\n' "$probe_ids" | while read -r id; do
        [ -n "$id" ] || continue
        [ "$(docker image inspect "$id" --format '{{len .RepoTags}}' 2>/dev/null)" = "0" ] && docker rmi -f "$id" >/dev/null 2>&1
    done
    # fail-closed self-check: proj token を含む docker 資源 + temp tree の残留を検出 (rootless/SELinux で
    # chown/rm が効かず root 所有 temp が残る filesystem leak も loud に落とす)。token は必ず separator
    # (- / _) を伴うので anchor し、別 PID probe (dcup-smoke-7005) を substring 誤検出しない。残存したら
    # fail-closed に昇格 (成功終了を上書き)。
    local left
    left=$( { docker ps -a --format '{{.Names}}'; docker images --format '{{.Repository}}'; \
              docker volume ls --format '{{.Name}}'; docker network ls --format '{{.Name}}'; } 2>/dev/null \
            | grep -cE -- "${proj}[-_]" || true )
    if [ "$left" -ne 0 ] || [ -e "$tmproot" ]; then
        echo "cleanup 後も残留: docker 資源 $left 件 / temp=$tmproot ($([ -e "$tmproot" ] && echo 残 || echo 無)) — 手動削除が要る" >&2
        [ "$rc" -eq 0 ] && rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT   # tmproot/proj は設定済み → 以降どこで落ちても cleanup が走る (temp leak を塞ぐ)。

probe_ws="$tmproot/$proj"
mkdir -p "$probe_ws"
# scaffold だけ要る (postStart は workspace 内容を読まない)。.git/.venv/node_modules 等の巨大・無関係物を
# 除いて複製する (I/O 削減 + 前回 up の残した root 所有 mount stub 等での複製失敗を避ける)。pipefail で
# どちらかの tar が落ちれば非 0 → fail-closed。
tar -C "$wsabs" -cf - --exclude=./.git --exclude=./node_modules --exclude='./.venv' --exclude='./.devcontainer/.venv' . \
  | tar -C "$probe_ws" -xf - \
  || { echo "workspace の複製に失敗 ($wsabs → $probe_ws)" >&2; exit 1; }
cfg="$probe_ws/$cfgrel"

out=$(COMPOSE_PROJECT_NAME="$proj" devcontainer up --workspace-folder "$probe_ws" --config "$cfg" 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
    echo "devcontainer up が失敗 (postStartCommand が落ちた可能性 — 下記末尾を確認):" >&2
    printf '%s\n' "$out" | tail -30 >&2
    exit 1
fi
# positive control: 隔離が効いた (建った dev コンテナが proj ラベルを持つ) ことを確認してから、
# proj scope の cleanup に依存する。COMPOSE_PROJECT_NAME が無視されると別 project で建ち cleanup が
# 空振りするので、それを先に検出する。
docker ps -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | grep -q . \
    || { echo "隔離失効: project '$proj' のコンテナが無い (COMPOSE_PROJECT_NAME が反映されていない)" >&2; exit 1; }
# up が exit 0 でも、gh seed 整合検査が実際に走り期待 user で通ったことを pin する (検査の素通り防止)。
printf '%s\n' "$out" | grep -qF "gh seed user consistency OK ($expect_user)" \
    || { echo "postStart の gh seed 整合検査が走っていない/期待 user '$expect_user' と不一致 (下記末尾を確認):" >&2; printf '%s\n' "$out" | tail -30 >&2; exit 1; }
echo "ok  devcontainer-up (postStart 完走 + gh seed OK: $expect_user)"
