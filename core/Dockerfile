# devcontainer イメージ定義。編集点は 2 層: .devcontainer/core/Dockerfile (kit 本体の template) と
# .devcontainer/project/Dockerfile.{top,dev} (project 固有 fragment)。template のマーカー行を fragment の
# 内容に置換したものが生成物 .devcontainer/Dockerfile になる (.devcontainer/core/bin/gen-dockerfile.sh。
# 再生成は `make -C .devcontainer/core gen-dockerfile`、drift 検査は check-dockerfile-gen)。
# build context は .devcontainer/ なので、COPY のソースパスは `core/...` / `project/...` で書く。

FROM ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419 AS uv

FROM node:24.17.0-trixie@sha256:61db8992b5c481488fe236ea69fe94035ba73df76a474051ed2e9713f3a15e5a AS base

# sha256 検証の `… | sha256sum -c -` を pipefail 下で走らせる (左辺の失敗も拾う。hadolint DL4006)。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# glibc は TZ 環境変数を直接参照するので、これだけで date / node 等の時刻が JST 表示になる
# (tzdata は image 同梱)。ホスト依存で上書きしたいときは compose の environment: で渡す。
ENV TZ=Asia/Tokyo
ENV DEVCONTAINER=true
# claude の設定 dir を固定する (既定は版依存)。
ENV CLAUDE_CONFIG_DIR=/home/node/.claude

ARG CLAUDE_CODE_VERSION=latest
# package.json の packageManager と合わせる
ARG PNPM_VERSION=11.5.2

# GitHub Releases 成果物は version と sha256 を対で固定し、取得後に sha256sum -c で検証する
# (CDN 改竄・取り違えを fail-closed で弾く)。version 更新時は sha256 も必ず更新する。出典と
# 取得手順は docs/verified-facts/github-releases-sha256.md「GitHub Releases 成果物の sha256」。arch は
# dpkg --print-architecture (amd64|arm64) を正規キーにし、上流の資産名へ case で写す。
ARG GIT_DELTA_VERSION=0.18.2
ARG GIT_DELTA_SHA256_amd64=1658c7b61825d411b50734f34016101309e4b6e7f5799944cf8e4ac542cebd7f
ARG GIT_DELTA_SHA256_arm64=937781aa7788e1510858743fff6c9a8b4a69fe0a22a7c8a69493e633227939a9
ARG ZSH_IN_DOCKER_VERSION=1.2.0
ARG ZSH_IN_DOCKER_SHA256=f74e5b08c295b6c3886654bb63c688e5ea16c58a4209435c4ddbab2c42fe9b41

RUN apt-get update && apt-get install -y --no-install-recommends \
  less \
  git \
  procps \
  ca-certificates \
  fzf \
  zsh \
  man-db \
  unzip \
  gnupg2 \
  gh \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  jq \
  nano \
  vim \
  locales \
  && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
  && locale-gen \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# devcontainer 固有の claude managed 設定 (最優先・上書き不可。permissions は scope 横断で merge)。
# 上書きしたい個人 pref は user settings (claude-config volume の settings.json) 側に置く。
COPY core/claude/managed-settings.json /etc/claude-code/managed-settings.json

# git: この devcontainer の FS (Docker Desktop の virtiofs/overlay) では unpack-trees の stat フル比較
# (ctime/inode/uid/gid/dev) が内容不変でも揺れ、merge-backend の `git rebase` が
# "local changes would be overwritten" を誤発生させる (実測: 既定 10/10 発生, checkStat=minimal で 0/10)。
# 比較を size + 秒精度 mtime に落として誤検知を消す。user 設定 (~/.gitconfig) は VS Code Dev Containers
# が起動時に host の gitconfig をコピー (上書き) するため消えるので、system (/etc/gitconfig) に置く。
RUN git config --system core.checkStat minimal

COPY --from=uv /uv /uvx /usr/local/bin/

# managed CPython を取得させず system python を使う。これが無いと firewall に阻まれた
# managed-python download で EHOSTUNREACH になる。
ENV UV_PYTHON_DOWNLOADS=never

# cwd に依らず uv sync/run が pyproject.toml/uv.lock を見つけ、venv は host の .venv を隠す
# named volume のマウント先 (/workspace/.devcontainer/.venv、compose.yaml) に作られるよう固定する。
ENV UV_PROJECT=/workspace/.devcontainer/core
ENV UV_PROJECT_ENVIRONMENT=/workspace/.devcontainer/.venv

# named volume のマウント先と npm global prefix を node 所有で用意する。dev は cap_drop ALL で
# 後から chown できないため、所有権はこの時点で決め切る。
# - 空の named volume の初期所有権は、ここで作る image 側ディレクトリの所有権を引き継ぐ
#   (claude config / node_modules / pnpm store / commandhistory)。
# - 履歴ファイルはここで器を用意するだけ (書込み設定 HISTFILE/PROMPT_COMMAND は後段の shell 設定)。
RUN mkdir -p \
      /usr/local/share/npm-global \
      /commandhistory \
      /workspace/node_modules \
      /workspace/.devcontainer/.venv \
      /home/node/.claude \
      /home/node/.local/share/pnpm \
      /home/node/.local/share/containers \
  && touch /commandhistory/.bash_history \
  && chown -R node:node /usr/local/share/npm-global /commandhistory /workspace /home/node/.claude /home/node/.local

# /home/node/.claude/settings.json の初期値。空の claude-config volume へ seed され、以降は
# user 設定として overridable (claude が volume 側のこのファイルを書き換える: effortLevel/theme 等)。
COPY --chown=node:node core/claude/user-settings.json /home/node/.claude/settings.json

WORKDIR /workspace

RUN ARCH=$(dpkg --print-architecture) && \
  case "$ARCH" in \
    amd64) SHA=$GIT_DELTA_SHA256_amd64 ;; \
    arm64) SHA=$GIT_DELTA_SHA256_arm64 ;; \
    *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
  esac && \
  curl -fsSL -o /tmp/git-delta.deb "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  echo "${SHA}  /tmp/git-delta.deb" | sha256sum -c - && \
  dpkg -i /tmp/git-delta.deb && \
  rm /tmp/git-delta.deb

USER node

ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
# sfw-shims を先頭に置く: pnpm/npm/npx/pip/uv は常に Socket Firewall 経由で実行される
ENV PATH=/usr/local/share/sfw-shims:$PATH:/usr/local/share/npm-global/bin

ENV SHELL=/bin/zsh
ENV EDITOR=nano
ENV VISUAL=nano

# script は実行前に sha256 検証する (wget|sh の直結は中身を検証できないため temp に落として確認)。
RUN curl -fsSL -o /tmp/zsh-in-docker.sh "https://github.com/deluan/zsh-in-docker/releases/download/v${ZSH_IN_DOCKER_VERSION}/zsh-in-docker.sh" && \
  echo "${ZSH_IN_DOCKER_SHA256}  /tmp/zsh-in-docker.sh" | sha256sum -c - && \
  sh /tmp/zsh-in-docker.sh \
  -p git \
  -p fzf \
  -a "source /usr/share/doc/fzf/examples/key-bindings.zsh" \
  -a "source /usr/share/doc/fzf/examples/completion.zsh" \
  -a "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  -x && \
  rm /tmp/zsh-in-docker.sh

# sfw は npm パッケージがラッパーで、初回実行時に GitHub Releases から実体バイナリを
# 取得する。firewall 適用前のビルド時に --help で取得を済ませておく
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} pnpm@${PNPM_VERSION} sfw && \
  sfw --help >/dev/null

USER root

COPY core/sfw-shim.sh /usr/local/share/sfw-shims/.sfw-exec
RUN chmod +x /usr/local/share/sfw-shims/.sfw-exec && \
  for cmd in pnpm npm npx pip pip3 uv; do \
  ln -s .sfw-exec "/usr/local/share/sfw-shims/$cmd"; \
  done

USER node


FROM hadolint/hadolint:v2.14.0-alpine@sha256:7aba693c1442eb31c0b015c129697cb3b6cb7da589d85c7562f9deb435a6657c AS hadolint

FROM docker/compose-bin:v5.3.1@sha256:38fd4a3c7458b931d339b1eb657df0bef790a39bcaeb93ff9f70f59849b905c5 AS compose

# @@PROJECT:top@@

# ============================================================================
# devcontainer 本体 (dev): base に rootless Podman 一式を足す。構成と blast-radius は PODMAN.md。
#
# newuidmap/newgidmap に file-cap を付与し setuid ビットを外す。この環境では setuid→cap 付与が効かず、
# setuid 任せだと rootless Podman の uid_map write が EPERM になるため (docs/verified-facts/podman.md
# 「rootless Podman / newuidmap」)。setcap には libcap2-bin。
#
# dnsmasq-base は make check の check-redact-config が dev 内で `dnsmasq --test` を叩くため
# (dev 自身は dnsmasq を起動しない。redact ステージは full dnsmasq を別途入れる)。
# ============================================================================
FROM base AS dev
# SHELL は stage 毎に宣言が要る (hadolint は FROM 跨ぎの継承を見ない。DL4006)。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
  podman \
  uidmap \
  slirp4netns \
  libcap2-bin \
  sudo \
  proxychains4 \
  shellcheck \
  dnsmasq-base \
  && apt-get clean && rm -rf /var/lib/apt/lists/* \
  && setcap cap_setuid+ep /usr/bin/newuidmap \
  && setcap cap_setgid+ep /usr/bin/newgidmap \
  && chmod u-s /usr/bin/newuidmap /usr/bin/newgidmap
COPY core/podman/storage.conf core/podman/containers.conf core/podman/registries.conf /etc/containers/

# --chmod=0755 で 0755 root 所有に正規化する (上流 image の /bin/hadolint は 0777 = 同 uid が
# 上書き可能なので締める)。
COPY --from=hadolint --chmod=0755 /bin/hadolint /usr/local/bin/hadolint

# podman の compose provider (= docker compose v2/compose-go、CI と同一エンジン)。これを置くと
# `podman compose config` が daemon 無しで動き、make check の compose 検証が devcontainer でも走る
# (SKIP 解消)。--chmod=0755 で 0755 root 所有に正規化する (hadolint と同様、同 uid の上書きを締める)。
COPY --from=compose --chmod=0755 /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# gh CLI の認証は dev に実トークンを置かず secrets-proxy が substitute する。token placeholder だけの
# hosts.yml を seed し (.devcontainer/project/rules.yaml「api.github.com」)、user 名 (consumer 値) は
# テンプレート (core/gh/hosts.yml) の @@GITHUB_USER@@ へ build arg PROJECT_GH_USER で焼き込む。
# 所有権の設計 (根拠と防御範囲: docs/verified-facts/gh.md「gh CLI / config dir の読取と書込」、
# updateRemoteUserUID の制約: devcontainers-cli.md):
#   - dir と hosts.yml (token の書込先) は root 所有 = gh auth login の実トークン焼込事故を封じる
#   - config.yml は node 所有のまま = 正当な gh config set / gh alias set を壊さない
#   - config.yml の version seed が起動時マイグレーションの hosts.yml 書換を抑止 (gh/config.yml)
# 値の変更は rebuild が要る (build arg)。「値を変えたが rebuild していない」drift は init-firewall.sh
# が起動時に照合して止める (旧イメージ窓は README「更新に伴う既知の移行」)。~/.config/gh は volume
# でなく ephemeral なのでイメージから復元される。
ARG PROJECT_GH_USER
COPY core/gh/hosts.yml /home/node/.config/gh/hosts.yml
COPY core/gh/config.yml /home/node/.config/gh/config.yml
# 事後条件を明示 assert する: user 名の形 ([[ =~ ]] は値全体に一致 — 行単位 grep だと複数行値の
# 1 行目だけで素通しする。空も弾く) / @@GITHUB_USER@@ の全展開 / @@SECRET_github_token@@ が両方の
# oauth_token: 値として残存 (コメント内の出現では満たされない位置指定。secrets-proxy の placeholder
# は build で消してはならない)。set -e + 各段に専用の || ハンドラ — && と || を混ぜて連結すると
# 前段の失敗が後段の無関係な診断へ流れ込む (左結合)。
RUN set -e; \
  [[ "$PROJECT_GH_USER" =~ ^[A-Za-z0-9-]+$ ]] \
    || { echo "PROJECT_GH_USER が不正 (project/.env に GitHub user 名 [A-Za-z0-9-]+ を設定): '$PROJECT_GH_USER'" >&2; exit 1; }; \
  sed -i "s/@@GITHUB_USER@@/$PROJECT_GH_USER/g" /home/node/.config/gh/hosts.yml \
    || { echo "hosts.yml への user 名焼込 (sed) が失敗" >&2; exit 1; }; \
  ! grep -q '@@GITHUB_USER@@' /home/node/.config/gh/hosts.yml \
    || { echo "hosts.yml に @@GITHUB_USER@@ が残った (置換漏れ)" >&2; exit 1; }; \
  [ "$(grep -c '^ *oauth_token: "@@SECRET_github_token@@"$' /home/node/.config/gh/hosts.yml)" -eq 2 ] \
    || { echo "hosts.yml の oauth_token 値から @@SECRET_github_token@@ が消えた (secrets-proxy placeholder 破壊)" >&2; exit 1; }; \
  chown node:node /home/node/.config; \
  chown -R root:root /home/node/.config/gh; \
  chown node:node /home/node/.config/gh/config.yml; \
  chmod 755 /home/node/.config/gh; \
  chmod 644 /home/node/.config/gh/hosts.yml /home/node/.config/gh/config.yml

# init-firewall.sh / install-proxy-ca.sh の再実行 (postStartCommand・CDN IP ローテーション対応) は
# dev だけが要る。sudo ごと dev 専用にする。
COPY core/init-firewall.sh /usr/local/bin/
COPY core/gh-hosts-user.awk /usr/local/bin/
COPY core/install-proxy-ca.sh /usr/local/bin/
# env_keep: init-firewall.sh の gh seed 整合検査は現在の PROJECT_GH_USER (compose environment) を
# 見る必要があるが、sudo の env_reset が剥がし、cap_drop:ALL の root は /proc/1/environ も読めない
# (node 所有 0400 + CAP_DAC_OVERRIDE/CAP_SYS_PTRACE 無し — docs/verified-facts/capabilities.md)。
# 検査は root 不要 (hosts.yml は 644) だが root スクリプト内に置く: 既存 consumer の postStartCommand
# 配線 (sudo 2 コマンド) が全 consumer に届く唯一の実行点で、node 実行の別スクリプトに分けると
# devcontainer.json 未更新の consumer で検査だけが silent に消える (fail-open)。
# per-command Defaults の文法と env_keep の semantics は docs/verified-facts/sudo.md。
# visudo -c で構文を build 時に検証する — sudoers の構文エラーは sudo 全体を無効化し、
# postStartCommand ごと devcontainer 起動を壊すため fail-closed に build で止める。
# 続く probe は「per-command env_keep が env_reset を実際に貫通する」semantics 自体を毎 build で
# 実測する (本命 entry は init-firewall.sh 束縛で build 中に実行できないため、probe 用 sudoers を
# 一時的に置いて /usr/bin/env で検証し、消す)。3 点 (sudoers/compose/reader) の名前一致は
# bin/redact_invariants_check.sh §11 が pin する。
# set -e + 各段専用ハンドラ — && 連鎖の末尾に || を混ぜると前段 (printf/chmod/visudo) の失敗が
# 末尾の env_keep 診断へ流れ込む (左結合)。visudo 構文エラー等は各コマンドの自前 stderr で落とす。
RUN set -e; \
  chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/install-proxy-ca.sh; \
  printf '%s\n%s\n%s\n' \
    'Defaults!/usr/local/bin/init-firewall.sh env_keep += "PROJECT_GH_USER"' \
    "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" \
    "node ALL=(root) NOPASSWD: /usr/local/bin/install-proxy-ca.sh" \
    > /etc/sudoers.d/node-firewall; \
  chmod 0440 /etc/sudoers.d/node-firewall; \
  visudo -c -f /etc/sudoers.d/node-firewall; \
  printf '%s\n%s\n' \
    'Defaults!/usr/bin/env env_keep += "KIT_ENVKEEP_PROBE"' \
    "node ALL=(root) NOPASSWD: /usr/bin/env" \
    > /etc/sudoers.d/kit-envkeep-probe; \
  chmod 0440 /etc/sudoers.d/kit-envkeep-probe; \
  [ "$(runuser -u node -- env KIT_ENVKEEP_PROBE=ok sudo -n /usr/bin/env | grep -c '^KIT_ENVKEEP_PROBE=ok$')" = 1 ] \
    || { echo "sudoers の per-command env_keep が機能しない (init-firewall.sh への PROJECT_GH_USER 伝搬が壊れる)" >&2; exit 1; }; \
  rm /etc/sudoers.d/kit-envkeep-probe

# project 層の直結許可ドメイン (init-firewall.sh が読む)。project/allow-domains.txt が編集点。
COPY project/allow-domains.txt /etc/agent-devcontainer/allow-domains.txt

# proxychains: HTTP_PROXY を尊重しないコマンドを secrets-proxy 経由へ通す入口。conf テンプレートと
# `pc` ラッパを置く (使い方・設計は README「HTTP_PROXY を尊重しないコマンド (proxychains)」)。
# proxychains の [ProxyList] は数値 IP のみで secrets-proxy の IP は動的なので、`pc` が起動時に解決する。
COPY core/proxychains4.conf /etc/proxychains4.conf
COPY core/pc.sh /usr/local/bin/pc
RUN chmod 0644 /etc/proxychains4.conf && chmod 0755 /usr/local/bin/pc

# @@PROJECT:dev@@

USER node


# ============================================================================
# redact サンドボックス派生ステージ。共通祖先 base を流用する
# (claude-code / uv / node+tsx / iptables+ipset+dig を既に持つ)。base に対する差分は3点:
#   - dnsmasq (allowlist フィルタ resolver) を追加する。
#   - mitmproxy を system python に焼き込む。runtime は api.anthropic.com しか開かないので
#     uv が Python/mitmproxy を取りに行けない。ビルド時 (=ネット開放) のうちに入れておく。
#   - dnsmasq 専用 uid・redact スクリプト・設定を配置し、entrypoint を差し替える。
#
# redact compose は target: redact でこのステージをビルドする (base は BuildKit が依存として
# 自動ビルドする)。別 Dockerfile に切り出すと FROM が外部イメージ参照になり版固定 (hadolint DL3006)
# が要る — 同一 Dockerfile のステージにすることで避けている。
# ============================================================================
FROM base AS redact

USER root

# mitmproxy のバージョンは pyproject.toml の [dependency-groups] redact を単一の情報源にする
# (redact_flow.py がこの system python で import する)。
COPY core/pyproject.toml /tmp/pyproject.toml

# uv はシム (sfw) を避けて実体を直叩きする。--system の site-packages は誰でも読めるので、
# entrypoint が node へ降格した子も python3 で import できる。
# uv (musl 静的バイナリ) の pip install 直前に /etc/resolv.conf を先頭の nameserver 1 行だけに
# 削る (理由: docs/verified-facts/network.md「musl (uv) resolver / 複数 nameserver」)。ただし書き換えは
# nameserver 行が 2 つ以上あるときだけ: トリムが要るのは nested rootless podman のような複数 nameserver
# 環境で、そこでは resolv.conf は書込可。BuildKit (docker 既定) は RUN 中 /etc/resolv.conf を read-only
# bind にするが (moby/buildkit#1267)、その環境は nameserver 1 行なので書き込み自体が発生しない。
# トリムが必要なのに書けない環境では printf が自然に失敗する (fail-closed 維持)。
RUN apt-get update && apt-get install -y --no-install-recommends \
      dnsmasq \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && if [ "$(grep -c '^nameserver' /etc/resolv.conf)" -gt 1 ]; then \
         resolv=$(awk '!/^nameserver/ || !n++' /etc/resolv.conf) && printf '%s\n' "$resolv" > /etc/resolv.conf; \
       fi \
    && /usr/local/bin/uv pip install --system --break-system-packages --project /tmp --group redact \
    && rm -f /tmp/pyproject.toml \
    && useradd --system --no-create-home --shell /usr/sbin/nologin redact-dns

# 設定・スクリプトを配置する (生成物の context は .devcontainer/ なので core/redact/ 配下を参照する)。
COPY core/redact/dnsmasq.conf core/redact/interview.md core/redact/settings.json /etc/redact/
COPY core/redact/init-firewall.sh /usr/local/bin/redact-init-firewall.sh
COPY core/redact/entrypoint.sh /usr/local/bin/redact-entrypoint.sh
RUN chmod +x /usr/local/bin/redact-init-firewall.sh /usr/local/bin/redact-entrypoint.sh

# entrypoint は root で動く (iptables/dnsmasq/resolv.conf)。内部で node へ降格して claude を起動する。
# 最後が root なのは意図的 (DL3002 抑制): 子への降格は entrypoint が runuser で行う。
# hadolint ignore=DL3002
USER root
ENTRYPOINT ["/usr/local/bin/redact-entrypoint.sh"]
