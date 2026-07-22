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
# 複製して up する。devcontainers CLI が features/UID 調整で建てる image 名は cwd 由来
# (getFolderImageName = vsc-<basename(cwd)>-<sha256(cwd)>[-uid]; devcontainers-cli.md) で project 名では
# 隔離できない。basename を probe 専用の一意名にすることで、compose の <proj>-<svc> image も folder 由来の
# vsc-<proj>-<hash> image も全て dcup-smoke-<pid> token を持ち、user が同じ folder で開いている実
# devcontainer の image (vsc-<realfolder>-*) とは決して衝突しない (up 中に user の共有 image を retag する
# 副作用も起きない)。COMPOSE_PROJECT_NAME も同名にして container/volume/network を隔離する
# (CLI は COMPOSE_PROJECT_NAME を最優先 — devcontainers-cli.md)。cleanup はこの token を持つ資源だけを消す。
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
wsabs=$(cd "$ws" && pwd)

proj="dcup-smoke-$$"
# 一意 basename の temp workspace へ複製 (basename が folder image 名に効くので proj 名と揃える)。
tmproot=$(mktemp -d)
probe_ws="$tmproot/$proj"
mkdir -p "$probe_ws"
cp -a "$wsabs/." "$probe_ws/"

cleanup() {
    # container/volume/network は compose が付ける exact project label で消す (prefix over-match なし)。
    docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1
    docker volume ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker volume rm -f >/dev/null 2>&1
    docker network ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker network rm >/dev/null 2>&1
    # image は compose project label が付かないので name (reference glob) で消す。compose 系
    # (<proj>-<svc>) と folder 系 (vsc-<proj>-<hash>[-uid]) の両方。separator (- / _) を anchor して
    # PID prefix の取り違え (dcup-smoke-700-* が dcup-smoke-7005-… に一致する) を防ぐ。reference filter は
    # repo:tag を glob 照合し複数指定は OR (devcontainers-cli.md / docker docs)。
    docker images -q --filter "reference=$proj-*" --filter "reference=${proj}_*" --filter "reference=vsc-$proj-*" \
        2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
    rm -rf "$tmproot"
    # fail-closed self-check: proj token を含む docker 資源が残っていないか (CI 外の投入ホストでも効く)。
    local left
    left=$( { docker ps -a --format '{{.Names}}'; docker images --format '{{.Repository}}'; \
              docker volume ls --format '{{.Name}}'; docker network ls --format '{{.Name}}'; } 2>/dev/null \
            | grep -c -- "$proj" || true )
    [ "$left" -eq 0 ] || echo "警告: cleanup 後も '$proj' を含む docker 資源が $left 件残存 — 手動削除が要る" >&2
    return 0
}
trap cleanup EXIT

# devcontainer.json の場所は consumer (.devcontainer/) と kit dogfood (repo 直下) で異なるので両対応。
if [ -f "$probe_ws/.devcontainer/devcontainer.json" ]; then cfg="$probe_ws/.devcontainer/devcontainer.json";
elif [ -f "$probe_ws/devcontainer.json" ]; then cfg="$probe_ws/devcontainer.json";
else echo "devcontainer.json が複製先 $probe_ws (/.devcontainer) に無い" >&2; exit 1; fi

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
