#!/usr/bin/env bash
# devcontainer を実際に up して postStartCommand (init-firewall.sh の gh seed 整合検査 + firewall +
# install-proxy-ca.sh) まで end-to-end で完走するかを検査する。
#   usage: check_devcontainer_up.sh <workspace-folder> <expected-gh-user>
#
# なぜ要る: CI の build-images は image を build するだけで postStartCommand を実行しない。だが
# devcontainers CLI 経路でしか出ない runtime 契約 — overrideCommand が compose 宣言 entrypoint を
# 捨てる / cap_drop:ALL の root が node 所有パスを traverse できず EACCES 等 (docs/incidents.md) —
# は build では捕まらず、consumer の初 rebuild で初めて露見してきた。ここで実際に up して postStart
# まで走らせ、その class を回帰 pin する。要 docker + @devcontainers/cli。重い/ネット要のため既定の
# check には入れず CI (build-images) と投入ホストで明示実行する。
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

# devcontainer.json の場所は consumer (.devcontainer/) と kit dogfood (repo 直下) で異なるので両対応。
if [ -f "$wsabs/.devcontainer/devcontainer.json" ]; then cfg="$wsabs/.devcontainer/devcontainer.json";
elif [ -f "$wsabs/devcontainer.json" ]; then cfg="$wsabs/devcontainer.json";
else echo "devcontainer.json が $wsabs (/.devcontainer) に無い" >&2; exit 1; fi

# probe 専用の一意な compose project 名で up して user の実 devcontainer / 他 stack から隔離する
# (devcontainers CLI は COMPOSE_PROJECT_NAME を最優先する — devcontainers-cli.md「project 名の fallback」)。
# これで既存 devcontainer を reuse/破壊せず、cleanup も本 project のラベルが付いた資源だけを消す
# (副作用を残さない — AGENTS.md)。$$ (PID) で並行 probe 同士も衝突しない。
proj="dcup-smoke-$$"
cleanup() {
    docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1
    docker volume ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker volume rm -f >/dev/null 2>&1
    docker network ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | xargs -r docker network rm >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

out=$(COMPOSE_PROJECT_NAME="$proj" devcontainer up --workspace-folder "$wsabs" --config "$cfg" 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
    echo "devcontainer up が失敗 (postStartCommand が落ちた可能性 — 下記末尾を確認):" >&2
    printf '%s\n' "$out" | tail -30 >&2
    exit 1
fi
# up が exit 0 でも、gh seed 整合検査が実際に走り期待 user で通ったことを pin する (検査の素通り防止)。
printf '%s\n' "$out" | grep -qF "gh seed user consistency OK ($expect_user)" \
    || { echo "postStart の gh seed 整合検査が走っていない/期待 user '$expect_user' と不一致 (下記末尾を確認):" >&2; printf '%s\n' "$out" | tail -30 >&2; exit 1; }
echo "ok  devcontainer-up (postStart 完走 + gh seed OK: $expect_user)"
