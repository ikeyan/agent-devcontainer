#!/usr/bin/env bash
# devcontainer を実際に up して postStartCommand (init-firewall.sh の gh seed 整合検査 + firewall +
# install-proxy-ca.sh) まで end-to-end で完走するかを検査する。
#   usage: check_devcontainer_up.sh <expected-gh-user>
#
# なぜ要る: CI の build-images は image を build するだけで postStartCommand を実行しない。だが
# devcontainers CLI 経路でしか出ない runtime 契約 — overrideCommand が compose 宣言 entrypoint を
# 捨てる (canon: incidents/gh-seed-check-three-rewrites-runtime-contract-assumed) / cap_drop:ALL の root が
# node 所有パスを traverse できず EACCES 等 (canon: incidents/gh-seed-check-home-traverse-blocks-devcontainer-open) —
# は build では捕まらず、consumer の初 rebuild で初めて露見してきた。ここで実際に up して postStart
# まで走らせ、その class を回帰 pin する。要 docker + @devcontainers/cli。
#
# 何を走らせるか: **kit の template scaffold**を temp dir に生成して up する (caller の workspace は
# 使わない)。この検査の目的は kit 自身 (生成 Dockerfile + core/compose + postStart) の回帰 pin であり、
# consumer が編集する project/compose.yaml・devcontainer.json (領域外 bind、workspace ファイルを参照する
# lifecycle、symlink 等) を走らせる必要はない。それらを走らせると isolation の脱出経路が無限に増える一方、
# kit の回帰は template でこそ再現する。→ kit templates/ から scaffold して edit 由来の脱出を構造的に断つ。
# kit-only (templates/ が要る。consumer には無いので非対応)。
#
# 隔離: **daemon 全体で一意な** basename (dcup-smoke-<mktemp 乱数>) の temp dir に scaffold する。
#   - devcontainers CLI が UID 調整で建てる image 名は cwd 由来 (getFolderImageName =
#     vsc-<basename(cwd)>-<sha256(cwd)>-uid; canon: facts/devcontainer/image-naming-and-safe-cleanup) で project 名では隔離できない。basename を
#     一意名にすると、compose の <proj>-<svc> image も folder 系 vsc-<proj>-<hash> image も全て proj token を
#     持つ一意な tag 名になり、user が同じ folder で開いている実 devcontainer の tag とは別名になる。token を
#     PID でなく mktemp 乱数から採るのは共有 daemon を別 PID namespace の probe が叩くと $$ が衝突しうるため。
#     COMPOSE_PROJECT_NAME も同名にして container/volume/network を隔離する。
#   - image content は Dockerfile + core/+project/ + build-arg で決まり workspace は bind mount なので、probe
#     image は user の実 image と同じ content-addressable ID を持ちうる。ゆえに cleanup は ID でなく tag 名で
#     rmi する (共有 ID の user tag を巻き込まない)。契約と限界は canon: facts/devcontainer/image-naming-and-safe-cleanup。
set -uo pipefail
expect_user=${1:?usage: check_devcontainer_up.sh <expected-gh-user>}
command -v devcontainer >/dev/null 2>&1 || { echo "@devcontainers/cli が要る (npm i -g @devcontainers/cli)" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker が要る (compose provider 込み)" >&2; exit 1; }
# kit root を script 位置から求める (core/bin/ の 2 つ上)。templates/ が無ければ consumer なので非対応。
selfdir=$(cd "$(dirname "$0")" && pwd) || { echo "script dir を解決できない" >&2; exit 1; }
kitroot=$(cd "$selfdir/../.." && pwd) || { echo "kit root を解決できない" >&2; exit 1; }
[ -d "$kitroot/templates/project" ] && [ -f "$kitroot/templates/devcontainer.json" ] && [ -d "$kitroot/core" ] \
    || { echo "kit-only smoke: $kitroot に templates/project + templates/devcontainer.json + core/ が要る (consumer では非対応)" >&2; exit 1; }

tmproot=$(mktemp -d) || { echo "temp ディレクトリの作成 (mktemp -d) に失敗" >&2; exit 1; }
[ -n "$tmproot" ] || { echo "mktemp -d が空を返した" >&2; exit 1; }
# proj token は mktemp の乱数 suffix から採る (PID でなく daemon 全体で一意に)。docker image/compose 名の
# 規則に合わせ小文字 + 英数字のみへ正規化。
proj="dcup-smoke-$(basename "$tmproot" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"

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
    # devcontainer up は workspace の bind/volume mount 先を host 上に root 所有で作る → 実行 uid では rm でき
    # ない。docker を root で走らせ所有権を戻してから消す (docker は本 script の必須依存。CHOWN で足りる)。
    if [ -n "$devimg" ]; then
        docker run --rm -u 0 --entrypoint chown -v "$tmproot:/t" "$devimg" -R "$(id -u):$(id -g)" /t >/dev/null 2>&1
    fi
    rm -rf "$tmproot"
    # image は tag 名で child-first に消す (ID だと共有 content-ID の user tag を巻き込む)。子 (folder 系
    # uid image) → 親 (compose 系) の順。separator anchor で別 probe token との取り違えを防ぐ。契約と限界は
    # canon: facts/devcontainer/image-naming-and-safe-cleanup。
    docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=vsc-$proj-*" 2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
    docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=$proj-*" --filter "reference=${proj}_*" 2>/dev/null | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
    # fail-closed self-check: proj token を含む docker 資源 + temp tree の残留を検出。daemon が落ちて検査
    # クエリ自体が失敗すると空 → 誤 clean になるので、まず docker 応答性を確認し不応答なら fail-closed。
    if ! docker ps -q >/dev/null 2>&1; then
        echo "cleanup 検証中に docker が不応答 — 残留を確認できないため fail-closed" >&2
        [ "$rc" -eq 0 ] && rc=1
    else
        # token は必ず separator (- / _) を伴うので anchor する。残存 or temp 残りは fail-closed に昇格。
        local left
        left=$( { docker ps -a --format '{{.Names}}'; docker images --format '{{.Repository}}'; \
                  docker volume ls --format '{{.Name}}'; docker network ls --format '{{.Name}}'; } 2>/dev/null \
                | grep -cE -- "${proj}[-_]" || true )
        if [ "$left" -ne 0 ] || [ -e "$tmproot" ]; then
            echo "cleanup 後も残留: docker 資源 $left 件 / temp=$tmproot ($([ -e "$tmproot" ] && echo 残 || echo 無)) — 手動削除が要る" >&2
            [ "$rc" -eq 0 ] && rc=1
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT   # tmproot/proj は設定済み → 以降どこで落ちても cleanup が走る (temp leak を塞ぐ)。

# kit template を probe_ws に scaffold (root layout: project/ core/ devcontainer.json Dockerfile を並置)。
# CI build-images の scaffold と同じ手順。@@PROJECT_NAME@@ 置換 + PROJECT_GH_USER 設定 + Dockerfile 生成。
probe_ws="$tmproot/$proj"
mkdir -p "$probe_ws"
cp -Rp "$kitroot/templates/project"          "$probe_ws/project"        || { echo "template project の scaffold に失敗" >&2; exit 1; }
cp -p  "$kitroot/templates/devcontainer.json" "$probe_ws/devcontainer.json" || { echo "template devcontainer.json の scaffold に失敗" >&2; exit 1; }
cp -Rp "$kitroot/core"                        "$probe_ws/core"           || { echo "core の scaffold に失敗" >&2; exit 1; }
sed -i "s/@@PROJECT_NAME@@/smoke/g" "$probe_ws/project/compose.yaml" "$probe_ws/devcontainer.json"
# PROJECT_GH_USER を expect_user に (gh seed の baked user = これ。既存行は消して 1 本化)。
{ grep -v '^PROJECT_GH_USER=' "$probe_ws/project/.env" 2>/dev/null; printf 'PROJECT_GH_USER=%s\n' "$expect_user"; } > "$probe_ws/project/.env.tmp" \
    && mv "$probe_ws/project/.env.tmp" "$probe_ws/project/.env" || { echo "project/.env の PROJECT_GH_USER 設定に失敗" >&2; exit 1; }
( cd "$probe_ws" && bash core/bin/gen-dockerfile.sh ) > "$probe_ws/Dockerfile" || { echo "Dockerfile の生成に失敗" >&2; exit 1; }
cfg="$probe_ws/devcontainer.json"

out=$(COMPOSE_PROJECT_NAME="$proj" devcontainer up --workspace-folder "$probe_ws" --config "$cfg" 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
    echo "devcontainer up が失敗 (postStartCommand が落ちた可能性 — 下記末尾を確認):" >&2
    printf '%s\n' "$out" | tail -30 >&2
    exit 1
fi
# positive control: 隔離が効いた (建った dev コンテナが proj ラベルを持つ) ことを確認してから、proj scope の
# cleanup に依存する。COMPOSE_PROJECT_NAME が無視されると別 project で建ち cleanup が空振りする。
docker ps -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null | grep -q . \
    || { echo "隔離失効: project '$proj' のコンテナが無い (COMPOSE_PROJECT_NAME が反映されていない)" >&2; exit 1; }
# up が exit 0 でも、gh seed 整合検査が実際に走り期待 user で通ったことを pin する (検査の素通り防止)。
printf '%s\n' "$out" | grep -qF "gh seed user consistency OK ($expect_user)" \
    || { echo "postStart の gh seed 整合検査が走っていない/期待 user '$expect_user' と不一致 (下記末尾を確認):" >&2; printf '%s\n' "$out" | tail -30 >&2; exit 1; }
echo "ok  devcontainer-up (postStart 完走 + gh seed OK: $expect_user)"
