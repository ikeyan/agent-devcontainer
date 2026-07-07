FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

# sha256 検証の `… | sha256sum -c -` を fail-closed にするため pipefail を効かせる (hadolint DL4006)。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# glibc は TZ 環境変数を直接参照するので、これだけでログ等の時刻が JST 表示になる。
ENV TZ=Asia/Tokyo

# Bitwarden CLI のバージョン。Vaultwarden のサーバ版と互換が取れるものに合わせる。
# リリースタグ cli-v<VERSION> / アセット bw-linux[-arm64]-<VERSION>.zip が存在すること
# を確認すること。linux arm64 の単体バイナリは 2026.4 以降のみ配布される。
# https://github.com/bitwarden/clients/releases
# 成果物は version と sha256 を対で固定し sha256sum -c で検証する (出典は GitHub API の asset
# .digest。docs/verified-facts/github-releases-sha256.md「GitHub Releases 成果物の sha256」)。version 更新時は sha256 も更新。
ARG BW_CLI_VERSION=2026.5.0
ARG BW_CLI_SHA256_amd64=5ddbf5383bec9c71613d7b699ffa5249b5e7b68b2a3d15b4c75738ba8a8f9a19
ARG BW_CLI_SHA256_arm64=5b744b976ab49400c8b4470b1db51c2911e66cdd7a8398ad36a5ab6a85568d27

# mitmproxy のバージョン (addon API はメジャー間で変わりうるので固定)
ARG MITMPROXY_VERSION=11.1.3

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      unzip \
      openssl \
      tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      "mitmproxy==${MITMPROXY_VERSION}" \
      "PyYAML>=6,<7"

# Bitwarden CLI (単体バイナリ。node 不要)。buildx が渡す TARGETARCH (amd64|arm64) で
# 資産を選び、ホストと同じアーキのネイティブバイナリを入れる。これをしないと Apple
# Silicon (arm64) で x86-64 バイナリを Rosetta 実行しようとして、コンテナ内に
# /lib64/ld-linux-x86-64.so.2 が無く "rosetta error" で落ちる。
ARG TARGETARCH
RUN case "${TARGETARCH:-amd64}" in \
      arm64) bw_asset="bw-linux-arm64-${BW_CLI_VERSION}.zip"; SHA=$BW_CLI_SHA256_arm64 ;; \
      amd64) bw_asset="bw-linux-${BW_CLI_VERSION}.zip";       SHA=$BW_CLI_SHA256_amd64 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bw.zip \
      "https://github.com/bitwarden/clients/releases/download/cli-v${BW_CLI_VERSION}/${bw_asset}" \
    && echo "${SHA}  /tmp/bw.zip" | sha256sum -c - \
    && unzip -q /tmp/bw.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/bw \
    && rm /tmp/bw.zip

# 非 root ユーザ。named volume の所有権を合わせるため事前に mkdir + chown
RUN useradd -m -u 1000 mitm \
    && mkdir -p /home/mitm/.mitmproxy /home/mitm/.bw /ca /app /config \
    && chown -R mitm:mitm /home/mitm /ca /app

COPY addon.py /app/addon.py
COPY rules_bw.py /app/rules_bw.py
# mitmweb(Web UI) を container loopback に閉じたまま host から見るための stdio<->TCP 踏み台。
# host 側 (../proxy-web-tunnel.py) が docker exec で呼ぶ。default(mitmdump) 運用では未使用。
COPY tcp-bridge.py /app/tcp-bridge.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER mitm
WORKDIR /app

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
