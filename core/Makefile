.DEFAULT_GOAL := help
SHELL := /bin/bash

# .devcontainer の構成ファイルを push 前に検証する check 集約。
# check-* は相互に独立 (一時領域は mktemp で分離) — make -j で並列実行可。
# 各 check は可能なら negative probe を持ち、検査器が不正を実際に弾くことを pin する。
# 方針: 検査に要る道具 (shellcheck/hadolint/PyYAML/jsonschema/mitmproxy/hypothesis/tree-sitter/
# dnsmasq/docker or podman (+compose)/gh/proxychains4) は CI/devcontainer の両方に実在する前提で組み、
# 明示の skip ガードは置かない (無い環境ではその場で自然にエラーになる。green が「全 check が実際に
# 走った」ことを意味するようにする)。python 依存 target は kit 専用 venv ($(PY)、後述) に一元化する。
MAKEFLAGS += --output-sync=target

# shebang ではなく拡張子で走査 (この配下の .sh は全て bash)。redact-flow は拡張子なしのため明示。
SHELL_SRCS := $(wildcard *.sh bin/*.sh secrets-proxy/*.sh redact/*.sh) bin/redact-flow

# kit 専用 python (uv sync で作る venv。CI (.github/workflows/check.yml) と devcontainer
# (Dockerfile の UV_PROJECT_ENVIRONMENT) の両方が $(CURDIR)/../.venv = .devcontainer/.venv に作る
# ので、CI/devcontainer で分岐せずこの 1 本で参照できる)。
PY := $(CURDIR)/../.venv/bin/python

# compose 検証エンジン: CI は docker、devcontainer は podman (provider = docker compose plugin)。
# docker が無ければ podman を採用する。どちらも無い環境は次の呼び出しが自然にエラーになる。
COMPOSE := $(shell command -v docker >/dev/null 2>&1 && echo "docker compose" || echo "podman compose")
# compose は常に project 層 (../project/compose.yaml) を先頭に重ねる。先頭ファイルのディレクトリが
# project directory になり project/.env が補間に効く。後勝ちで core が優先 (docs/verified-facts/docker.md)。
COMPOSE_FILES := -f ../project/compose.yaml -f compose.yaml
# redact-image 実ビルド用エンジン (daemonless の podman も可)。docker が無ければ podman を採用する。
ENGINE := $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
# podman は OCI 出力フォーマット (既定) だと Dockerfile の SHELL を無視するため docker フォーマットで建て、
# sha256 検証 pipe の pipefail を honor させる (出典・理由は docs/verified-facts/podman.md「podman build / SHELL (OCI vs docker フォーマット)」。
# docker engine は --format を取らないので podman 時だけ付ける)。
BUILD_FORMAT := $(shell [ "$(ENGINE)" = podman ] && echo "--format docker")

# make check が回す検査群。1 行 1 target にして追加時の diff を最小化する (DRY: check 集約と .PHONY で共用)。
CHECKS := \
	check-compose \
	check-compose-paths \
	check-proxy-web-config \
	check-shell \
	check-shellcheck \
	check-addon \
	check-addon-tests \
	check-rules \
	check-rules-schema \
	check-rules-bw \
	check-dockerfile-gen \
	check-dockerfile \
	check-dockerfile-deps \
	check-redact-config \
	check-redact-selfcheck \
	check-redact-py \
	check-redact-invariants \
	check-gh-seed \
	check-proxychains \
	check-seccomp

# check-redact-image / check-redact-dns-egress は重い/ネット要のため check 集約には入れず明示実行する (下記)。
.PHONY: help check gh-seed proxy-web proxy-web-off check-redact-image check-redact-dns-egress gen-seccomp gen-dockerfile $(CHECKS)

help: ## 一覧
	@sed -nE 's/^([a-zA-Z_-]+):.*## (.*)$$/  \1\t\2/p' $(MAKEFILE_LIST) | expand -t 20

check: $(CHECKS) ## .devcontainer 全体を検証

# config は daemon 不要 (パース + マージのみ)。docker/podman compose は CI/devcontainer 必須。
check-compose: ## compose.yaml を compose config で検証 (docker/podman compose 必須)
	@$(COMPOSE) $(COMPOSE_FILES) config >/dev/null 2>&1 \
		|| { echo "compose.yaml invalid:" >&2; $(COMPOSE) $(COMPOSE_FILES) config 2>&1 >/dev/null | tail -20 >&2; exit 1; }; \
	bad=$$(mktemp); printf 'services: [unclosed\n' > "$$bad"; \
	$(COMPOSE) -f "$$bad" config >/dev/null 2>&1 \
		&& { echo "negative: compose config が不正 YAML を素通し" >&2; rm -f "$$bad"; exit 1; }; \
	rm -f "$$bad"; echo "ok  compose ($(COMPOSE))"

# マージ済み compose config のホスト側パス (build.context / type:bind の source / security_opt の
# seccomp ファイル) が実在するかを stat で pin する。相対ホストパスは project directory (= 最初の -f =
# ../project/) 基準で解決される (docs/verified-facts/devcontainers-cli.md / docker.md「compose 複数 -f」)。
# core/ を意図した ./X が project/ 基準で不在パスに解決される誤アンカーは compose config 自体は素通す
# ため、ここで fail-closed に捕まえる。config は daemon 不要。yq 非依存 (python3+PyYAML = $(PY))。
# negative probe: 不在 bind source を注入した override を重ね、検出器が弾くことを確認する。
check-compose-paths: ## マージ compose のホスト側 bind source/context/seccomp の実在を検証 (compose+PyYAML 必須)
	@$(COMPOSE) $(COMPOSE_FILES) config 2>/dev/null \
		| $(PY) bin/check_compose_paths.py --project-dir ../project >/dev/null 2>&1 \
		|| { echo "compose のホスト側パスが実在しない (相対アンカー誤り?):" >&2; \
		     $(COMPOSE) $(COMPOSE_FILES) config 2>/dev/null | $(PY) bin/check_compose_paths.py --project-dir ../project 2>&1 >/dev/null | grep '^NG' >&2; exit 1; }; \
	bad=$$(mktemp); printf 'services:\n  dev:\n    volumes:\n      - ../core/DOES-NOT-EXIST-xyz:/probe:ro\n' > "$$bad"; \
	$(COMPOSE) $(COMPOSE_FILES) -f "$$bad" config 2>/dev/null \
		| $(PY) bin/check_compose_paths.py --project-dir ../project >/dev/null 2>&1 \
		&& { echo "negative: check-compose-paths が不在 bind source を素通し" >&2; rm -f "$$bad"; exit 1; }; \
	rm -f "$$bad"; echo "ok  compose-paths ($(COMPOSE))"

# override(compose.mitmweb.yaml) を基底に合成した compose が妥当か、かつ既定 compose に
# Web UI が漏れていないかを検証する。config は daemon 不要。compose は CI/devcontainer 必須。
check-proxy-web-config: ## compose.mitmweb.yaml 合成の妥当性 & 既定への Web 漏れ無しを検証 (compose 必須)
	@$(COMPOSE) $(COMPOSE_FILES) -f compose.mitmweb.yaml config >/dev/null 2>&1 \
		|| { echo "compose.mitmweb.yaml 合成が不正:" >&2; \
		     $(COMPOSE) $(COMPOSE_FILES) -f compose.mitmweb.yaml config 2>&1 >/dev/null | tail -20 >&2; exit 1; }; \
	$(COMPOSE) $(COMPOSE_FILES) config 2>/dev/null | grep -Eq '8081|mitmweb' \
		&& { echo "negative: 既定 compose に Web UI が漏れている" >&2; exit 1; }; \
	echo "ok  proxy-web-config ($(COMPOSE))"

# secrets-proxy を mitmweb (ライブ傍受 Web UI) に切替える入口 (デバッグ用)。
# dev は絶対に touch しない: VS Code が overrideCommand で dev を無限 sleep で生かしており、
# その override は VS Code の compose 呼び出しにしか無い。plain な `up` で dev を作り直すと
# ベース CMD が headless 実行され即 exit する (= 「devcontainer 起動直後に exit」の原因)。よって
# override 合成は secrets-proxy 1 本だけに適用する (--no-deps + サービス名明示 + detached)。
# project 名は compose の `name: tools` で VS Code と共有するので、稼働中の secrets-proxy を
# その場で mitmweb に差し替えられる。起動ログの `?token=...` を拾って表示する。戻すのは proxy-web-off。
# --build 必須: entrypoint.sh / tcp-bridge.py は image に COPY される (bind mount ではない) ので、
# 変更を反映するには再ビルドが要る (キャッシュが効くので通常は一瞬)。
# mitmweb は container loopback に bind し publish は張らない (dev から到達不可)。host からは最後に
# foreground で開く踏み台トンネル (proxy-web-tunnel.py: docker exec 経由) でだけ届く。Ctrl-C で
# トンネルを閉じる (secrets-proxy は動いたまま; 完全に戻すのは proxy-web-off)。要 host python3。
proxy-web: ## secrets-proxy を mitmweb に切替え host 踏み台トンネルを開く (Ctrl-C で閉じる)
	$(COMPOSE) $(COMPOSE_FILES) -f compose.mitmweb.yaml up -d --build --no-deps secrets-proxy
	@tok=; for _ in $$(seq 1 30); do \
	  tok=$$($(COMPOSE) $(COMPOSE_FILES) -f compose.mitmweb.yaml logs secrets-proxy 2>&1 | grep -oE 'token=[0-9a-f]+' | tail -1); \
	  [ -n "$$tok" ] && break; sleep 1; \
	done; \
	if [ -n "$$tok" ]; then echo "open (トンネル起動後): http://127.0.0.1:8081/?$$tok"; \
	else echo "token 未検出 (mitmweb 起動待ちかも)。手動: $(COMPOSE) $(COMPOSE_FILES) logs secrets-proxy | grep token"; fi
	@COMPOSE="$(COMPOSE) $(COMPOSE_FILES)" python3 proxy-web-tunnel.py

# proxy-web を解除し secrets-proxy を既定 (mitmdump) に戻す。基底 compose だけで secrets-proxy を
# 作り直す = SECRETS_PROXY_WEB / mitmweb 設定が落ちて mitmdump に戻る。dev は無停止。
proxy-web-off: ## secrets-proxy を既定 (mitmdump) に戻す (dev 無停止)
	$(COMPOSE) $(COMPOSE_FILES) up -d --build --no-deps secrets-proxy

# secrets-proxy の初回対話認証。bw login/unlock で得た BW_SESSION を `proxy-bw` volume (session.key) へ
# 保存する (README「セットアップ (初回 1 回)」)。project 層 (name: tools) 込みで compose を呼ぶので、
# 通常の up/exec と同じ project namespace の secrets-proxy に対して実行される。
bootstrap: ## secrets-proxy の初回対話認証 (BW_SESSION を proxy-bw volume へ保存)
	$(COMPOSE) $(COMPOSE_FILES) run --rm -it secrets-proxy bootstrap

check-shell: ## shell script を bash -n で構文チェック
	@rc=0; for f in $(SHELL_SRCS); do \
		bash -n "$$f" || { echo "syntax error: $$f" >&2; rc=1; }; \
	done; \
	printf 'if true; then\n' | bash -n /dev/stdin 2>/dev/null \
		&& { echo "negative: bash -n が未完了構文を素通し" >&2; rc=1; }; \
	if [ $$rc -ne 0 ]; then exit 1; fi; echo "ok  shell (bash -n)"

check-shellcheck: ## shell script を shellcheck (CI/devcontainer 必須)
	@shellcheck -S warning $(SHELL_SRCS) && echo "ok  shellcheck"

check-addon: ## addon.py / 踏み台スクリプトの構文を compile() で検査 (.pyc を残さない)
	@for f in secrets-proxy/addon.py secrets-proxy/tcp-bridge.py proxy-web-tunnel.py; do \
		python3 -c "compile(open('$$f').read(),'$$f','exec')" \
			|| { echo "$$f invalid" >&2; exit 1; }; \
	done; \
	echo "ok  addon.py / tcp-bridge.py / proxy-web-tunnel.py"

# project 層の rules.yaml (実設定) を core 側の rules.schema.json (JSON Schema) で構造検証する。
# negative probe は schema が未知キーを弾くことを pin。PyYAML/jsonschema は kit venv ($(PY)) に入っている。
check-rules: ## ../project/rules.yaml を JSON Schema で検証 (PyYAML+jsonschema 必須)
	@$(PY) -c 'import json,sys,yaml,jsonschema; sc=json.load(open("secrets-proxy/rules.schema.json")); v=jsonschema.validators.validator_for(sc)(sc); v.check_schema(sc); errs=sorted(v.iter_errors(yaml.safe_load(open("../project/rules.yaml")) or {}), key=lambda e: list(e.path)); [sys.stderr.write("  rules.yaml /"+"/".join(map(str,e.path))+": "+e.message+"\n") for e in errs]; sys.exit(1) if errs else (sys.exit(1) if v.is_valid({"bogus_key":1}) else None)' \
		|| { echo "rules invalid (schema 検証失敗 / negative probe)" >&2; exit 1; }; \
	echo "ok  rules (schema)"

# rules.schema.json の substitute / into 分岐を fixture で gate する。check-rules は実体の
# rules.yaml だけを検証するため、rules.yaml が踏まない分岐 (substituteEntry の oneOf、into 必須化)
# はそこでは exercise されない。positive/negative fixture でスキーマ退行を pin する。
check-rules-schema: ## rules.schema.json の substitute/into 分岐を fixture で検証 (jsonschema 必須)
	@cd secrets-proxy && PYTHONDONTWRITEBYTECODE=1 $(PY) test_rules_schema.py

# entrypoint.sh が「rules に Vaultwarden(item) 源がある時だけ unlock を要求する」判定 (rules_bw.py)
# の単体テスト。env/file だけの rules で BW を要求しない / item で要求する / parse 不能で fail-closed、
# を pin する。PyYAML だけで動く (mitmproxy 不要) ので軽量 check に入れる。
check-rules-bw: ## rules_bw.py (entrypoint の bw 源判定) の単体テスト (PyYAML 必須)
	@cd secrets-proxy && PYTHONDONTWRITEBYTECODE=1 $(PY) test_rules_bw.py

# Dockerfile は生成物: core/Dockerfile (marker template) + project/Dockerfile.{top,dev} から
# bin/gen-dockerfile.sh が ../Dockerfile を生成する (gen-seccomp と同じ生成+drift 検査パターン)。
GEN_DOCKERFILE := bash bin/gen-dockerfile.sh
gen-dockerfile: ## ../Dockerfile を core template + project fragment から再生成
	$(GEN_DOCKERFILE) > ../Dockerfile
check-dockerfile-gen: ## ../Dockerfile が生成結果と一致するか (drift 検査 + marker 検出の negative probe)
	@set -o pipefail; \
	$(GEN_DOCKERFILE) | diff -u ../Dockerfile - || exit 1; \
	bad=$$(mktemp); printf 'FROM scratch\n' > "$$bad"; \
	$(GEN_DOCKERFILE) "$$bad" >/dev/null 2>&1 \
		&& { echo "negative: gen-dockerfile が marker 欠落 template を素通し" >&2; rm -f "$$bad"; exit 1; }; \
	rm -f "$$bad"; echo "ok  dockerfile-gen"

check-dockerfile: ## Dockerfile (template + 生成物) を hadolint (CI/devcontainer 必須)
	@rc=0; for f in Dockerfile ../Dockerfile secrets-proxy/Dockerfile; do \
		hadolint "$$f" || rc=1; \
	done; \
	if [ $$rc -ne 0 ]; then exit 1; fi; echo "ok  dockerfile"

# Dockerfile (template + 生成物 + secrets-proxy) の外部 image 依存を AST で検査する:
# 外部 FROM は @sha256 digest 固定 / COPY --from は先行ステージ alias のみ (Dependabot 追従性)。
# 根拠と AST node 名は check_dockerfile_deps.py header + docs/verified-facts/{dependabot,tree-sitter-containerfile}.md。
# tree_sitter は kit venv ($(PY)) に入っている。
check-dockerfile-deps: ## Dockerfile 外部 image の digest 固定 / COPY --from alias を AST 検査 (tree-sitter 必須)
	@$(PY) bin/check_dockerfile_deps.py Dockerfile ../Dockerfile secrets-proxy/Dockerfile

# redact サンドボックスの構成を検証する。compose は必須 env をダミーで与えて config 検証
# (negative: REDACT_APP 欠落で弾けること)。dnsmasq.conf は placeholder を埋めて --test
# (dnsmasq は CI/devcontainer に在る)。settings.json は JSON として読めることを確認。
# compose config は CI/devcontainer とも docker/podman compose が必須。
check-redact-config: ## redact/ の compose/dnsmasq/settings を検証 (docker/podman compose 必須)
	@python3 -c 'import json;json.load(open("redact/settings.json"))' \
		|| { echo "redact/settings.json が不正 JSON" >&2; exit 1; }; \
	REDACT_APP=demo REDACT_REPO=/repo REDACT_RAW=/repo/raw.flows \
		$(COMPOSE) -f redact/compose.yaml config >/dev/null 2>&1 \
		|| { echo "redact/compose.yaml が不正" >&2; exit 1; }; \
	REDACT_REPO=/repo REDACT_RAW=/repo/raw.flows \
		$(COMPOSE) -f redact/compose.yaml config >/dev/null 2>&1 \
		&& { echo "negative: redact compose が REDACT_APP 欠落を素通し" >&2; exit 1; } || true; \
	t=$$(mktemp); sed 's/__UPSTREAM_DNS__/127.0.0.11/g' redact/dnsmasq.conf > "$$t"; \
	dnsmasq --test --conf-file="$$t" >/dev/null 2>&1 \
		|| { echo "redact/dnsmasq.conf が不正" >&2; rm -f "$$t"; exit 1; }; \
	rm -f "$$t"; echo "ok  redact-config (settings/compose/dnsmasq)"

# extract/*/redact_flow.py を合成 flow で round-trip し、エンジン契約 (valid NDJSON / 行数一致 /
# 例外なし) を検査する。詳細と切り分けは bin/redact_selfcheck.py の docstring。mitmproxy は
# kit venv ($(PY)) に入っている。__pycache__ を残さない。
check-redact-selfcheck: ## extract/*/redact_flow.py のエンジン契約を検査 (mitmproxy 必須)
	@PYTHONDONTWRITEBYTECODE=1 $(PY) bin/redact_selfcheck.py

# redact エンジン・テンプレート・selfcheck 自体の Python 構文を compile() で検査する。
# selfcheck は実行時にエンジンを import するが、テンプレートはどの check も見ていなかった。
check-redact-py: ## redact 系 Python (エンジン/テンプレート/selfcheck) の構文検査
	@for f in redact/flows_to_ndjson.py redact/redact_flow.template.py bin/redact_selfcheck.py; do \
		python3 -c "compile(open('$$f').read(),'$$f','exec')" || { echo "$$f invalid" >&2; exit 1; }; \
	done; \
	bad=$$(mktemp); printf 'def (:\n' > "$$bad"; \
	python3 -c "compile(open('$$bad').read(),'$$bad','exec')" 2>/dev/null \
		&& { echo "negative: compile 検出器が不正 Python を素通し" >&2; rm -f "$$bad"; exit 1; }; \
	rm -f "$$bad"; echo "ok  redact-py (compile)"

# Codex review が捕まえた redact の「意味的」不具合 (構文 valid だが解釈が違う) を回帰 pin する。
# 中身と各 pin の根拠は bin/redact_invariants_check.sh の冒頭コメント参照。#1〜#9 は git と grep だけ、
# #10 (マージ compose の姿勢) だけ compose config + PyYAML が要る (CI/devcontainer 必須なので常時走る)。
check-redact-invariants: ## redact の意味的不変条件を pin (bin/redact_invariants_check.sh; §10 は compose 必須)
	@bash bin/redact_invariants_check.sh

# committed の config.yml+hosts.yml を temp に渡し gh を 1 回走らせ、byte 不変なら no-op = ro 安全
# (詳細: docs/verified-facts/gh.md「gh CLI / config マイグレーション」)。gh は CI/devcontainer 必須。
check-gh-seed: ## installed gh が gh/ seed を migration で書き換えないか検証 (gh 必須)
	@tmp=$$(mktemp -d); cp gh/config.yml gh/hosts.yml "$$tmp/"; \
	before=$$(cat "$$tmp/config.yml" "$$tmp/hosts.yml" | md5sum); \
	GH_CONFIG_DIR="$$tmp" GH_NO_UPDATE_NOTIFIER=1 gh config get version >/dev/null 2>&1 || true; \
	after=$$(cat "$$tmp/config.yml" "$$tmp/hosts.yml" | md5sum); \
	rm -rf "$$tmp"; \
	[ "$$before" = "$$after" ] || { echo "gh-seed: installed gh が seed を migration で書換える (ro hosts.yml だと gh が起動時に落ちる) → make -C .devcontainer/core gh-seed で再生成して commit" >&2; exit 1; }; \
	tmp2=$$(mktemp -d); cp gh/hosts.yml "$$tmp2/hosts.yml"; \
	GH_CONFIG_DIR="$$tmp2" GH_NO_UPDATE_NOTIFIER=1 gh config get version >/dev/null 2>&1 || true; \
	if [ ! -f "$$tmp2/config.yml" ]; then echo "negative: gh-seed 検出器が migration を捕捉できない" >&2; rm -rf "$$tmp2"; exit 1; fi; \
	rm -rf "$$tmp2"; echo "ok  gh-seed (installed gh は seed を no-op で読む = ro 安全)"

# 挙動と理由: docs/verified-facts/gh.md「gh CLI / config マイグレーション」。生成後は diff を確認して commit する。
gh-seed: ## gh/{config,hosts}.yml を installed gh の migration で更新 (gh upgrade 後に実行, ネット不要)
	@command -v gh >/dev/null 2>&1 || { echo "gh が無い" >&2; exit 1; }; \
	tmp=$$(mktemp -d); cp gh/config.yml gh/hosts.yml "$$tmp/"; \
	ver=$$(GH_CONFIG_DIR="$$tmp" GH_NO_UPDATE_NOTIFIER=1 gh config get version 2>/dev/null); \
	[ -n "$$ver" ] || { echo "version を導出できない (migration がネット必須化した可能性)" >&2; rm -rf "$$tmp"; exit 1; }; \
	{ printf '# gh の config マイグレーション台帳。生成元: make -C .devcontainer/core gh-seed\n'; \
	  printf 'version: "%s"\n' "$$ver"; } > gh/config.yml; \
	cp "$$tmp/hosts.yml" gh/hosts.yml; \
	rm -rf "$$tmp"; \
	echo "gh-seed: gh/config.yml (version=$$ver) と gh/hosts.yml を installed gh から更新"

# redact イメージを実ビルドし、node へ降格した子が `python3` で mitmproxy を import でき、その
# python が >=3.12 であることを確認する (mitmproxy 12.x の要件)。ベース OS の python が古い／子から
# system site-packages が読めない、等の回帰を実ビルドで捕まえる (静的検査では捕まらない)。
# Dockerfile の redact ステージを --target でビルドする (dev ステージも依存として自動ビルドされる;
# dev を既にビルド済みなら BuildKit キャッシュで再利用)。重い & ネットを使うので既定の check 集約には
# 入れない — 投入ホストで明示的に叩く。
check-redact-image: ## redact イメージを実ビルドし node 子の python3>=3.12+mitmproxy を検査 (要 docker/podman+ネット; 重いので明示実行)
	@$(ENGINE) build $(BUILD_FORMAT) -q -t tools-redact-checkimg --target redact -f ../Dockerfile .. >/dev/null \
		|| { echo "redact image build 失敗 (要ネット)" >&2; exit 1; }; \
	$(ENGINE) run --rm --user node --entrypoint bash tools-redact-checkimg -lc \
		'python3 -c "import sys, mitmproxy; assert sys.version_info >= (3, 12), sys.version"' \
		|| { echo "redact-image: node 子が python3>=3.12 + mitmproxy を使えない" >&2; exit 1; }; \
	echo "ok  redact-image ($(ENGINE); node python3>=3.12 + mitmproxy import)"

# Docker `internal:` network が外部 DNS を転送しない (= internal net 自体が DNS exfil を塞ぐ) ことを
# 実機で pin する。redact を proxy 中継 + internal net へ移す設計の要石を回帰で守る
# (docs/verified-facts/docker.md「internal network の DNS」参照)。pin 対象は docker の埋め込み DNS —
# podman (aardvark-dns) は対象外なので $(ENGINE) でなく docker 固定。daemon + busybox 系イメージ +
# 外部 DNS が要るので check 集約には入れず、kit CI (.github/workflows/check.yml) と投入ホストで明示実行する。
check-redact-dns-egress: ## internal net が外部 DNS を転送しないことを実機 pin (docker+ネット必須; 重いので明示実行)
	@bash redact/dns_egress_test.sh

# addon.py の単体/property テスト (mitmproxy の test ヘルパで hook を駆動。実ネットワーク不要)。
# mitmproxy + hypothesis は kit venv ($(PY)) に入っている。__pycache__ を残さない。
check-addon-tests: ## secrets-proxy/test_addon.py を実行 (mitmproxy+hypothesis 必須)
	@cd secrets-proxy && PYTHONDONTWRITEBYTECODE=1 $(PY) test_addon.py

# proxychains (HTTP_PROXY 非対応コマンドを secrets-proxy へ通す入口) の 2 段検証:
#  (1) 静的整合性 — proxychains4.conf の proxy port が compose の HTTP_PROXY (secrets-proxy:PORT) と一致し、
#      pc.sh が secrets-proxy を解決していること。
#  (2) 挙動 — proxychains 経由でホスト名が CONNECT に載る (rules.yaml 照合の前提) ことを hermetic に検証
#      (test_proxychains.py)。proxychains4/curl は devcontainer と CI に在る前提で常時走らせる。
check-proxychains: ## proxychains の config 整合性 + 透過ルーティング挙動を検証 (proxychains4/curl 必須)
	@cport=$$(grep -oE 'secrets-proxy:[0-9]+' compose.yaml | head -1 | cut -d: -f2); \
	pport=$$(awk '/^\[ProxyList\]/{f=1;next} f&&/^http /{print $$3; exit}' proxychains4.conf); \
	{ [ -n "$$cport" ] && [ "$$cport" = "$$pport" ]; } \
		|| { echo "proxychains port 不一致: compose=$$cport proxychains4.conf=$$pport" >&2; exit 1; }; \
	grep -q 'getent hosts secrets-proxy' pc.sh \
		|| { echo "pc.sh が secrets-proxy を解決していない" >&2; exit 1; }; \
	PYTHONDONTWRITEBYTECODE=1 python3 test_proxychains.py

# tailored seccomp profile (rootless Podman 用)。gen-seccomp が gen-seccomp.ts の出力を seccomp.json に
# 書き、check-seccomp は再生成結果と commit 済みを diff する。設計と追従は PODMAN.md。
# node が .ts を直接実行する (型ストリッピング、node>=23.6。docs/verified-facts/nodejs.md)。
SECCOMP_GEN := node podman/gen-seccomp.ts
SECCOMP_JSON := podman/seccomp.json
gen-seccomp: ## seccomp.json を再生成 (HTTP_CACHE_REFRESH=1 で default.json も再取得)
	$(SECCOMP_GEN) >/dev/null && $(SECCOMP_GEN) > $(SECCOMP_JSON)
check-seccomp: ## seccomp.json が default.json snapshot + 変換と一致するか (node>=23.6 必須)
	@$(SECCOMP_GEN) | diff -u $(SECCOMP_JSON) - && echo "ok  seccomp"
