.DEFAULT_GOAL := help
SHELL := /bin/bash

# リポジトリルートの検証ハブ。`check` = `check-offline` (ルート成果物 + core)。
# 流儀は core/Makefile 冒頭コメントが規範 (相互独立・並列可・skip ガード無し・副作用なし)。
# 検査は consumer 相当の project 層と kit venv を前提にする — fresh clone では先に `make setup`。
# CI (.github/workflows/check.yml) が check の外で走らせる検査: workflow lint (check-actionlint。image を
# pull するのでネットが要る)・install.sh の冪等性・イメージ実ビルド・DNS egress (core の check-redact-dns-egress)。
MAKEFLAGS += --output-sync=target

# kit venv (`make setup` が作る。core/Makefile の $(PY) と同一実体)。
PY := $(CURDIR)/.venv/bin/python
# kit の node の道具 (`make setup` の npm ci が package-lock.json から入れる)。npx を通さない — 無いときに
# registry から取ってきて、lockfile と別の版で黙って走らせないため。
NODE_BIN := $(CURDIR)/node_modules/.bin

OFFLINE_CHECKS := check-core check-install-sh check-templates check-placeholder check-contamination check-hadolint-version check-pre-push-hook check-ts check-knip
.PHONY: help check check-offline setup hadolint-image check-actionlint $(OFFLINE_CHECKS)

help: ## 一覧
	@sed -nE 's/^([a-zA-Z_-]+):.*## (.*)$$/  \1\t\2/p' $(MAKEFILE_LIST) | expand -t 20

check: check-offline ## 全検証

check-offline: $(OFFLINE_CHECKS) ## ルート成果物 + core のネット不要検証

check-core: ## core の検査集約を委譲実行 (要 scaffold 済み project 層)
	@$(MAKE) -C core check

check-install-sh: ## install.sh と kit 補助スクリプト (bin/) を shellcheck (shellcheck 必須)
	@shellcheck -S warning install.sh bin/*.sh && echo "ok  install.sh + bin/*.sh (shellcheck)"

check-templates: ## templates と .github の JSON/YAML が parse 可能か (kit venv の PyYAML 必須)
	@python3 -c 'import json; json.load(open("templates/claude/settings.json")); json.load(open("templates/devcontainer.json"))' \
	&& $(PY) -c 'import yaml; yaml.safe_load(open("templates/github/dependabot.yml")); yaml.safe_load(open(".github/dependabot.yml"))' \
	&& echo "ok  templates (json/yaml)"

# 置換漏れの devcontainer.json 等は silent に出荷されるため pin (AGENTS.md 再発防止の規律「既定は fail-closed」)。
# 1 行目は negative probe — 検出対象の token が templates で実際に使われていることを確認し、
# templates 側の placeholder 改名で検査が空回りするのを防ぐ。
# 2 行目を `! grep` に戻さない: 対象欠落 (grep exit 2) まで成功に反転する。不一致 (exit 1) だけが成功。
check-placeholder: ## scaffold 産物に置換漏れ @@PROJECT_NAME@@ が無いか (要 setup 済み)
	@grep -rq '@@PROJECT_NAME@@' templates || { echo "negative probe: templates に @@PROJECT_NAME@@ が無い — 検査対象の token が drift" >&2; exit 1; }
	@grep -rn '@@PROJECT_NAME@@' project devcontainer.json Dockerfile; [ $$? -eq 1 ] && echo "ok  placeholder (@@PROJECT_NAME@@ 置換済み)"

# hadolint の版は core/Dockerfile の hadolint stage (tag+digest。Dependabot の docker が更新する) を正とする。
# CI (check.yml) は hadolint-image が出す image から binary を取り出すので、版と digest を直書きする site は
# Dockerfile だけ。host の hadolint はその image (linux の binary) を使えないので、PATH の hadolint の版が
# Dockerfile の tag と一致することを確かめる (ずれると同じ Dockerfile の lint 結果が環境で食い違う。版差の
# 実例は canon: facts/hadolint/v2.15-findings-on-v2.14-clean-dockerfile)。check.yml が hadolint-image を
# 使い続けていることと、release の直書き URL が無いことも pin する。負のプローブは抽出器 (sed) を合成入力で
# 自己テストする — 抽出器が退化すると全 pin が空文字で素通しになる。
HADOLINT_FROM_RE := ^FROM (hadolint/hadolint:v([0-9][0-9.]*)-alpine@sha256:[0-9a-f]{64}) AS hadolint$$
hadolint-image: ## core/Dockerfile の hadolint stage の image (tag+digest) を出す (CI が binary を取り出す)
	@ref=$$(sed -nE 's|$(HADOLINT_FROM_RE)|\1|p' core/Dockerfile); \
	[ "$$(printf '%s' "$$ref" | grep -c .)" -eq 1 ] \
		|| { echo "core/Dockerfile の hadolint stage (FROM hadolint/hadolint:v<版>-alpine@sha256:<digest> AS hadolint) がちょうど 1 行でない: '$$ref'" >&2; exit 1; }; \
	printf '%s\n' "$$ref"

check-hadolint-version: ## PATH の hadolint の版が core/Dockerfile の hadolint stage と一致し、check.yml がそれを使うか
	@probe='FROM hadolint/hadolint:v9.9.9-alpine@sha256:'"$$(printf '%064d' 0)"' AS hadolint'; \
	[ "$$(printf '%s\n' "$$probe" | sed -nE 's|$(HADOLINT_FROM_RE)|\2|p')" = 9.9.9 ] \
		&& [ "$$(printf 'Haskell Dockerfile Linter 9.9.9\n' | sed -nE 's/^Haskell Dockerfile Linter ([0-9][0-9.]*)$$/\1/p')" = 9.9.9 ] \
		|| { echo "negative: check-hadolint-version の抽出器が合成入力から版を取れない" >&2; exit 1; }; \
	ref=$$($(MAKE) -s hadolint-image) || exit 1; \
	want=$$(printf '%s\n' "$$ref" | sed -nE 's|^hadolint/hadolint:v([0-9][0-9.]*)-.*|\1|p'); \
	grep -qF 'make -s hadolint-image' .github/workflows/check.yml \
		|| { echo "check.yml が hadolint を make -s hadolint-image の image から取り出していない" >&2; exit 1; }; \
	grep -E 'hadolint/releases/download' .github/workflows/check.yml; st=$$?; \
	[ "$$st" -eq 1 ] || { echo "check.yml に hadolint の release の直書き URL がある (grep exit=$$st)" >&2; exit 1; }; \
	command -v hadolint >/dev/null \
		|| { echo "hadolint が PATH に無い (asdf plugin add hadolint https://github.com/ikeyan/asdf-hadolint.git && asdf install hadolint $$want && asdf set -u hadolint $$want)" >&2; exit 1; }; \
	raw=$$(hadolint --version 2>/dev/null) \
		|| { echo "hadolint を起動できない (asdf の shim なら版が未設定: asdf install hadolint $$want && asdf set -u hadolint $$want)" >&2; exit 1; }; \
	have=$$(printf '%s\n' "$$raw" | sed -nE 's/^Haskell Dockerfile Linter ([0-9][0-9.]*)$$/\1/p'); \
	[ -n "$$have" ] || { echo "hadolint --version の出力を解釈できない: '$$raw'" >&2; exit 1; }; \
	[ "$$have" = "$$want" ] || { echo "PATH の hadolint ($$have) が core/Dockerfile の hadolint stage ($$want) と不一致 (asdf install hadolint $$want && asdf set -u hadolint $$want)" >&2; exit 1; }; \
	echo "ok  hadolint-version ($$want)"

# core の TypeScript (node が型を剥がして直接実行する。tsconfig.json) の型検査。noUnusedLocals /
# noUnusedParameters で使われない変数・import も落とす。negative probe は合成ファイルを同じ設定で検査する。
check-ts: ## core の TypeScript を tsc で型検査 (使われない変数・import を含む。要 make setup)
	@t=$$(mktemp -d "$${TMPDIR:-/tmp}/check-ts.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	printf 'const unusedProbe: number = 1;\nexport {};\n' > "$$t/probe.ts"; \
	printf '{"extends": "%s/tsconfig.json", "include": ["%s/probe.ts"]}\n' "$(CURDIR)" "$$t" > "$$t/tsconfig.json"; \
	$(NODE_BIN)/tsc -p "$$t" >/dev/null 2>&1 && { echo "negative: tsc が使われない変数を素通し" >&2; exit 1; }; \
	$(NODE_BIN)/tsc -p . && echo "ok  ts (tsc)"

# 使われない export・ファイル・依存を knip で列挙する (knip.jsonc)。error があれば exit 1 (canon: facts/knip/scoped-config)。
check-knip: ## core の TypeScript と package.json の使われない export・ファイル・依存を knip で検査 (要 make setup)
	@$(NODE_BIN)/knip --no-progress && echo "ok  knip"

# actionlint の版と digest は .github/actionlint/Dockerfile の FROM (Dependabot の docker が更新する) が正。
# その image を docker で走らせる (image を pull するのでネットが要り、check-offline に入れない)。
check-actionlint: ## kit と templates の workflow を actionlint で検査 (docker 必須・初回は image を pull)
	@img=$$(sed -nE 's|^FROM (rhysd/actionlint:[^ ]+@sha256:[0-9a-f]{64})$$|\1|p' .github/actionlint/Dockerfile); \
	[ "$$(printf '%s' "$$img" | grep -c .)" -eq 1 ] \
		|| { echo ".github/actionlint/Dockerfile の FROM rhysd/actionlint:<tag>@sha256:<digest> がちょうど 1 行でない: '$$img'" >&2; exit 1; }; \
	docker run --rm -v "$(CURDIR):/repo:ro" -w /repo "$$img" .github/workflows/*.yml templates/github/workflows/*.yml \
		&& echo "ok  actionlint"

# 検査でなく状態を揃える段: hooks/pre-push を git が呼ぶ場所へ写す (無いときだけ。扱いは
# bin/install_pre_push_hook.sh 冒頭)。副作用は意図したもので、この 1 ファイルに限る。
# negative probe は使い捨てのリポで、上書きの拒否・core.hooksPath の拒否・2 回目の no-op を確かめる。
# probe は subshell の中で利用者の global/system の git 設定から切り離す (global の core.hooksPath があると、
# 別の理由で落ちて probe が素通りする)。落ちた理由は文言で区別する。実リポへの写しは subshell の外で、
# push と同じ利用者の設定で判定する。
check-pre-push-hook: ## hooks/pre-push を $$GIT_COMMON_DIR/hooks へ写す (無いとき) + 置き換え拒否の negative probe
	@t=$$(mktemp -d "$${TMPDIR:-/tmp}/pre-push-hook.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	( unset $$(git rev-parse --local-env-vars); export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1; \
	for d in fresh foreign hookspath dangling noexec globalpath; do git init -q "$$t/$$d" || exit 1; done; \
	printf '#!/bin/sh\n' > "$$t/foreign/.git/hooks/pre-push" && chmod +x "$$t/foreign/.git/hooks/pre-push" \
		&& git -C "$$t/hookspath" config core.hooksPath "$$t/elsewhere" \
		&& ln -s "$$t/nowhere" "$$t/dangling/.git/hooks/pre-push" \
		&& cp hooks/pre-push "$$t/noexec/.git/hooks/pre-push" && chmod -x "$$t/noexec/.git/hooks/pre-push" \
		&& printf '[core]\n\thooksPath = %s\n' "$$t/elsewhere" > "$$t/gitconfig" \
		&& git -C "$$t/fresh" -c user.name=probe -c user.email=probe@invalid commit -q --allow-empty -m probe \
		&& git -C "$$t/fresh" worktree add -q "$$t/linked" || exit 1; \
	bin/install_pre_push_hook.sh "$$t/linked" >/dev/null && cmp -s hooks/pre-push "$$t/fresh/.git/hooks/pre-push" \
		|| { echo "negative: linked worktree から共有の hooks へ写せない" >&2; exit 1; }; \
	bin/install_pre_push_hook.sh "$$t/dangling" >/dev/null && [ -f "$$t/dangling/.git/hooks/pre-push" ] \
		&& [ ! -L "$$t/dangling/.git/hooks/pre-push" ] && [ ! -e "$$t/nowhere" ] \
		|| { echo "negative: 壊れた symlink を置き換えない、または symlink の先に書いた" >&2; exit 1; }; \
	! o=$$(bin/install_pre_push_hook.sh "$$t/noexec" 2>&1) && printf '%s\n' "$$o" | grep -q '上書きしない' \
		|| { echo "negative: 同じ中身で実行ビットの無い pre-push を拒否しない" >&2; exit 1; }; \
	! o=$$(GIT_CONFIG_GLOBAL="$$t/gitconfig" bin/install_pre_push_hook.sh "$$t/globalpath" 2>&1) \
		&& printf '%s\n' "$$o" | grep -q 'hook の置き場' \
		|| { echo "negative: global の core.hooksPath を拒否しない" >&2; exit 1; }; \
	o=$$(bin/install_pre_push_hook.sh "$$t/fresh" 2>&1) && [ -z "$$o" ] \
		|| { echo "negative: 2 回目が出力なしの成功 (no-op) でない: $$o" >&2; exit 1; }; \
	! o=$$(bin/install_pre_push_hook.sh "$$t/foreign" 2>&1) && printf '%s\n' "$$o" | grep -q '上書きしない' \
		&& [ "$$(cat "$$t/foreign/.git/hooks/pre-push")" = '#!/bin/sh' ] \
		|| { echo "negative: 別の pre-push を非 0 で拒否せず、または上書きした" >&2; exit 1; }; \
	! o=$$(bin/install_pre_push_hook.sh "$$t/hookspath" 2>&1) && printf '%s\n' "$$o" | grep -q 'hook の置き場' \
		&& [ ! -e "$$t/hookspath/.git/hooks/pre-push" ] \
		|| { echo "negative: core.hooksPath を非 0 で拒否せず、または写した" >&2; exit 1; } ) \
	&& bin/install_pre_push_hook.sh && echo "ok  pre-push-hook"

# 実体は bin/check_contamination.sh (設計コメント・token 定義・negative probe 群もそちら)。
# 検出器自体が複雑な bash なので、Makefile 埋込でなくスクリプトにして shellcheck の圏内に置く
# (check-install-sh が lint する)。
check-contamination: ## core/ と templates/ に consumer 固有値 (ikeyan / tools-redact / tools_) が混入していないか
	@bash bin/check_contamination.sh core templates

# PROJECT_NAME は compose project 名 ([a-z0-9][a-z0-9_-]*)、GH_USER は GitHub user 名 ([A-Za-z0-9-]+、
# Dockerfile が assert)。charset が異なる別概念なので変数を分ける (例: PROJECT_NAME=my_proj は valid
# だが gh user としては invalid — 混用すると image build まで失敗が遅延する)。
# charset は sed 実行前に検証する (どちらの charset も sed 特殊文字 / & \ を含まない = 置換が安全)。
# scaffold は project.tmp に組み立ててから mv する — 途中失敗の半端な project/ を残すと存在ガードで
# 再実行が no-op になり回復不能になるため (@@PROJECT_NAME@@ の残存は check-placeholder が pin 済み)。
# PROJECT_GH_USER の書込は後段の補完 (backfill) に一本化する — 新規 scaffold (template の空行を
# 置換) も、この変数の導入前に scaffold した既存 checkout の修復も同じ経路を通す。project/repos.txt も
# 同様に、その導入前に scaffold した既存 checkout へ template を補完する (無いと check-repos が止まる)。
# 既存 project/ はそれ以外触らない (kit repo の project/ は gitignore された検査用 artifact であり
# consumer 所有物ではない)。補完は .env の実在と末尾改行を保証してから追記する (改行なし末尾に >> すると前の変数に
# 癒着して静かに壊す) + 追記結果を grep で assert (sed s/// は不一致でも exit 0 のため)。
# PROJECT_NAME / GH_USER は recipe へ export で渡す (`$(VAR)` の本文テキスト展開でなくシェル環境経由 =
# 値はデータ扱い、シェルの再展開・quote 破壊・バッククォート実行を受けない)。gate はこの値を検証し、
# 書込値・echo と一致する。make はコマンドライン `VAR=値` の `$` を parse 時に展開する (`$USER`→`SER`)
# 点だけは recipe で防げない — が空白・記号を含む値は charset gate が弾き、展開後の最終値が echo に出る。
PROJECT_NAME ?= kitci
GH_USER ?= kitci
export PROJECT_NAME GH_USER
setup: ## fresh clone を検査可能にする: consumer 相当の project 層 + kit venv + node の道具 (既存 project は PROJECT_GH_USER / repos.txt の欠落だけ補完)
	@[[ "$$PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$$ ]] || { echo "PROJECT_NAME が不正 ([a-z0-9][a-z0-9_-]*): '$$PROJECT_NAME'" >&2; exit 1; }
	@[[ "$$GH_USER" =~ ^[A-Za-z0-9-]+$$ ]] || { echo "GH_USER が不正 (GitHub user 名 [A-Za-z0-9-]+): '$$GH_USER'" >&2; exit 1; }
	@[ -e project ] || { rm -rf project.tmp && cp -Rp templates/project project.tmp \
		&& sed -i "s/@@PROJECT_NAME@@/$$PROJECT_NAME/" project.tmp/compose.yaml \
		&& mv project.tmp project || { rm -rf project.tmp; exit 1; }; }
	@[ -f project/.env ] || : > project/.env
	@[ -e project/repos.txt ] || cp -p templates/project/repos.txt project/repos.txt
	@grep -q '^PROJECT_GH_USER=..*' project/.env || { \
		sed -i '/^PROJECT_GH_USER=$$/d' project/.env \
		&& { [ -z "$$(tail -c1 project/.env)" ] || echo >> project/.env; } \
		&& printf 'PROJECT_GH_USER=%s\n' "$$GH_USER" >> project/.env \
		&& grep -q "^PROJECT_GH_USER=$$GH_USER$$" project/.env \
		&& echo "setup: project/.env に PROJECT_GH_USER=$$GH_USER を設定"; }
	@[ -e devcontainer.json ] || { cp -p templates/devcontainer.json devcontainer.json && sed -i "s/@@PROJECT_NAME@@/$$PROJECT_NAME/" devcontainer.json; }
	@[ -e Dockerfile ] || bash core/bin/gen-dockerfile.sh > Dockerfile
	@UV_PROJECT=$(CURDIR)/core UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv core/uv-sync.sh --frozen
	@# lockfile の再現なので、解決時の門 (.npmrc の min-release-age) を外す (canon: facts/npm/min-release-age-gates-npm-ci)。
	@npm ci --min-release-age=0 --no-audit --no-fund
