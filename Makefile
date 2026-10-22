.DEFAULT_GOAL := help
SHELL := /bin/bash

# リポジトリルートの検証ハブ。`check` = `check-offline` (ルート成果物 + core)。
# 流儀は core/Makefile 冒頭コメントが規範 (相互独立・並列可・skip ガード無し・副作用なし)。例外は
# check-pre-push-hook の 1 つで、setup-repo 標準 (3 節) に従い hooks/pre-push を git が呼ぶ場所へ写す (無いときだけ)。
# 検査は consumer 相当の project 層と kit venv を前提にする — fresh clone では先に `make setup`。
# CI (.github/workflows/check.yml) が check の外で走らせる検査: workflow lint (check-actionlint。image を
# pull するのでネットが要る)・install.sh の冪等性・イメージ実ビルド・DNS egress (core の check-redact-dns-egress)。
MAKEFLAGS += --output-sync=target

# kit venv (`make setup` が作る。core/Makefile の $(PY) と同一実体)。
PY := $(CURDIR)/.venv/bin/python
# kit の node の道具 (`make setup` の npm ci が package-lock.json から入れる)。npx を通さない — 無いときに
# registry から取ってきて、lockfile と別の版で黙って走らせないため。
NODE_BIN := $(CURDIR)/node_modules/.bin

OFFLINE_CHECKS := check-core check-install-sh check-templates check-placeholder check-contamination check-hadolint-version check-pre-push-hook check-ts check-knip check-supply-chain
.PHONY: help check check-offline setup npm-ci check-actionlint $(OFFLINE_CHECKS)

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
# token は setup の置換・probe・本検査が同じ変数を使う (片方だけの改名で検査が空回りしない)。
PLACEHOLDER := @@PROJECT_NAME@@
check-placeholder: ## scaffold 産物に置換漏れ $(PLACEHOLDER) が無いか (要 setup 済み)
	@grep -rq '$(PLACEHOLDER)' templates || { echo "negative probe: templates に $(PLACEHOLDER) が無い — 検査対象の token が drift" >&2; exit 1; }
	@grep -rn '$(PLACEHOLDER)' project devcontainer.json Dockerfile; [ $$? -eq 1 ] && echo "ok  placeholder ($(PLACEHOLDER) 置換済み)"

# hadolint の版は core/Dockerfile の hadolint stage (tag+digest。Dependabot の docker が更新する) が正。kit と
# consumer の CI は core の hadolint-image が出す image から binary を取り出す。host はその image (linux の
# binary) を使えないので、PATH の hadolint の版が stage の tag と一致することを確かめる (ずれると同じ
# Dockerfile の lint 結果が環境で食い違う。版差の実例は canon: facts/hadolint/v2.15-findings-on-v2.14-clean-dockerfile)。
# 版の取り出しは本番と negative probe で同じ式 (下の 2 つの変数と core の hadolint-image) を通す。
HADOLINT_REF_VERSION = sed -nE 's|^hadolint/hadolint:v([0-9][0-9.]*)-alpine@sha256:[0-9a-f]{64}$$|\1|p'
HADOLINT_BIN_VERSION = sed -nE 's/^Haskell Dockerfile Linter ([0-9][0-9.]*)$$/\1/p'
HADOLINT_IMAGE = $(MAKE) -s --no-print-directory -C core hadolint-image
# CI の workflow (kit と consumer の template) は hadolint を hadolint-image の image から取り出し、release の
# URL を直書きしない。
HADOLINT_CI_SITES := .github/workflows/check.yml templates/github/workflows/devcontainer-check.yml
check-hadolint-version: ## PATH の hadolint の版が core/Dockerfile の hadolint stage と一致し、CI がその image を使うか
	@t=$$(mktemp -d "$${TMPDIR:-/tmp}/hadolint-version.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	printf 'FROM hadolint/hadolint:v9.9.9-alpine@sha256:%064d AS hadolint\n' 0 > "$$t/one"; \
	cat "$$t/one" "$$t/one" > "$$t/two"; : > "$$t/zero"; \
	[ "$$($(HADOLINT_IMAGE) HADOLINT_DOCKERFILE="$$t/one" | $(HADOLINT_REF_VERSION))" = 9.9.9 ] \
		|| { echo "negative: hadolint-image と版の抽出器が合成の stage から 9.9.9 を取れない" >&2; exit 1; }; \
	for n in zero two; do \
		o=$$($(HADOLINT_IMAGE) HADOLINT_DOCKERFILE="$$t/$$n" 2>&1) \
			&& { echo "negative: hadolint-image が stage $$n 行の Dockerfile を拒否しない" >&2; exit 1; }; \
		printf '%s\n' "$$o" | grep -q 'ちょうど 1 行でない' \
			|| { echo "negative: hadolint-image が stage $$n 行を想定外の理由で落ちた: $$o" >&2; exit 1; }; \
	done; \
	[ "$$(printf 'Haskell Dockerfile Linter 9.9.9\n' | $(HADOLINT_BIN_VERSION))" = 9.9.9 ] \
		|| { echo "negative: hadolint --version の版の抽出器が合成入力から版を取れない" >&2; exit 1; }; \
	ref=$$($(HADOLINT_IMAGE)) || exit 1; want=$$(printf '%s\n' "$$ref" | $(HADOLINT_REF_VERSION)); \
	[ -n "$$want" ] || { echo "hadolint-image の出力から版を取れない: '$$ref'" >&2; exit 1; }; \
	for f in $(HADOLINT_CI_SITES); do \
		grep -qF 'hadolint-image' "$$f" || { echo "$$f が hadolint を hadolint-image の image から取り出していない" >&2; exit 1; }; \
		grep -E 'hadolint/releases/download' "$$f"; st=$$?; \
		[ "$$st" -eq 1 ] || { echo "$$f に hadolint の release の直書き URL がある (grep exit=$$st)" >&2; exit 1; }; \
	done; \
	raw=$$(hadolint --version 2>&1) \
		|| { echo "hadolint を起動できない: $$raw (asdf なら: asdf plugin add hadolint https://github.com/ikeyan/asdf-hadolint.git && asdf install hadolint $$want && asdf set -u hadolint $$want)" >&2; exit 1; }; \
	have=$$(printf '%s\n' "$$raw" | $(HADOLINT_BIN_VERSION)); \
	[ -n "$$have" ] || { echo "hadolint --version の出力を解釈できない: '$$raw'" >&2; exit 1; }; \
	[ "$$have" = "$$want" ] || { echo "PATH の hadolint ($$have) が core/Dockerfile の hadolint stage ($$want) と不一致 (asdf install hadolint $$want && asdf set -u hadolint $$want)" >&2; exit 1; }; \
	echo "ok  hadolint-version ($$want)"

# node_modules が package-lock.json と食い違っていれば落とす (lockfile の更新の後に make setup を回し忘れると、古い
# 道具で緑になる)。npm-ci が入れたときの lockfile の写しと比べる (npm ls は package.json の範囲しか見ず、
# lockfile だけの更新を見逃す)。
NODE_DEPS_CURRENT = cmp -s package-lock.json node_modules/.kit-package-lock.json || { echo "node_modules が package-lock.json と食い違う: make setup (npm-ci) を回す" >&2; exit 1; }

# core の TypeScript (node が型を剥がして直接実行する。tsconfig.json) の型検査。noUnusedLocals /
# noUnusedParameters で使われない変数・import も落とす。negative probe は合成ファイルを同じ設定 (extends) で検査し、
# 使われない変数の診断 (TS6133) で落ちることを確かめる (型定義の解決の失敗などで落ちても検出とみなさない)。
check-ts: ## core の TypeScript を tsc で型検査 (使われない変数・import を含む。要 make setup)
	@$(NODE_DEPS_CURRENT); \
	t=$$(mktemp -d "$${TMPDIR:-/tmp}/check-ts.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	printf 'const unusedProbe: number = 1;\nexport {};\n' > "$$t/probe.ts"; \
	printf '{"extends": "%s/tsconfig.json", "compilerOptions": {"typeRoots": ["%s/node_modules/@types"]}, "include": ["%s/probe.ts"]}\n' \
		"$(CURDIR)" "$(CURDIR)" "$$t" > "$$t/tsconfig.json"; \
	o=$$($(NODE_BIN)/tsc -p "$$t" 2>&1) && { echo "negative: tsc が使われない変数を素通し" >&2; exit 1; }; \
	[ "$$(printf '%s\n' "$$o" | grep -c 'error TS')" -eq 1 ] && printf '%s\n' "$$o" | grep -q 'error TS6133' \
		|| { echo "negative: tsc が使われない変数 (TS6133) 以外の理由で落ちた: $$o" >&2; exit 1; }; \
	$(NODE_BIN)/tsc -p . && echo "ok  ts (tsc)"

# 使われない export・ファイル・依存を knip で列挙する (knip.jsonc)。error があれば exit 1 (canon: facts/knip/scoped-config)。
# negative probe は合成のプロジェクトに使われない export を置き、knip がそれを名指しして落ちることを確かめる
# (合成のプロジェクトでは cd して起動する。--directory では entry の import を辿らない。canon: facts/knip/scoped-config)。
# knip.jsonc の ignoreDependencies (@devcontainers/cli) の根拠は、smoke の probe が devcontainer の CLI を呼ぶこと。
# その呼び出しが残っていることも確かめる (消えたら除外も消す)。
check-knip: ## core の TypeScript と package.json の使われない export・ファイル・依存を knip で検査 (要 make setup)
	@$(NODE_DEPS_CURRENT); \
	t=$$(mktemp -d "$${TMPDIR:-/tmp}/check-knip.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	printf '{"name": "knip-probe", "private": true, "type": "module"}\n' > "$$t/package.json"; \
	printf '{"entry": ["main.ts"], "project": ["*.ts"]}\n' > "$$t/knip.json"; \
	printf 'import { used } from "./lib.ts";\nconsole.log(used);\n' > "$$t/main.ts"; \
	printf 'export const used = 1;\nexport const unusedProbe = 2;\n' > "$$t/lib.ts"; \
	o=$$(cd "$$t" && $(NODE_BIN)/knip --no-progress 2>&1); st=$$?; \
	[ "$$st" -eq 1 ] && printf '%s\n' "$$o" | grep -q 'unusedProbe' \
		|| { echo "negative: knip が使われない export を検出しない (exit $$st): $$o" >&2; exit 1; }; \
	grep -qE '(^|[^[:alnum:]_-])devcontainer up ' core/bin/check_devcontainer_up.sh \
		|| { echo "knip.jsonc の ignoreDependencies (@devcontainers/cli) の根拠 (check_devcontainer_up.sh の devcontainer up) が無い" >&2; exit 1; }; \
	$(NODE_BIN)/knip --no-progress && echo "ok  knip"

# 供給網の門の値 (setup-repo の 2 節)。待機日数は .npmrc / core/pyproject.toml / dependabot の全 ecosystem で
# 同じ日数にする (1 つだけ変えると、解決時の門と Dependabot の提案がずれる)。各値は 1 つの抽出関数で読み、
# negative probe も同じ関数を合成入力に通す。
check-supply-chain: ## 供給網の門 (待機日数・install script・sdist build) が .npmrc / pyproject / dependabot / package.json で揃っているか
	@npm_days() { sed -nE 's/^min-release-age=([0-9]+)$$/\1/p' "$$1"; }; \
	uv_days() { sed -nE 's/^exclude-newer = "([0-9]+) days"$$/\1/p' "$$1"; }; \
	cooldown_days() { sed -nE 's/^ +default-days: ([0-9]+)$$/\1/p' "$$1"; }; \
	ecosystems() { grep -cE '^ +- package-ecosystem: ' "$$1"; }; \
	t=$$(mktemp -d "$${TMPDIR:-/tmp}/supply-chain.XXXXXX") || exit 1; trap 'rm -rf "$$t"' EXIT; \
	printf 'min-release-age=9\n' > "$$t/npmrc"; printf 'exclude-newer = "9 days"\n' > "$$t/pyproject"; \
	printf '  - package-ecosystem: "a"\n    cooldown:\n      default-days: 9\n  - package-ecosystem: "b"\n' > "$$t/dependabot"; \
	[ "$$(npm_days "$$t/npmrc")" = 9 ] && [ "$$(uv_days "$$t/pyproject")" = 9 ] && [ "$$(cooldown_days "$$t/dependabot")" = 9 ] \
		&& [ "$$(ecosystems "$$t/dependabot")" -eq 2 ] \
		|| { echo "negative: 待機日数の抽出器が合成入力から値を取れない" >&2; exit 1; }; \
	n=$$(npm_days .npmrc); u=$$(uv_days core/pyproject.toml); c=$$(cooldown_days .github/dependabot.yml | sort -u); \
	[ -n "$$n" ] && [ "$$n" = "$$u" ] && [ "$$n" = "$$c" ] \
		|| { echo "待機日数が揃っていない: .npmrc=$$n core/pyproject.toml=$$u dependabot=$$(printf '%s' "$$c" | tr '\n' ' ')" >&2; exit 1; }; \
	[ "$$n" -ge 1 ] || { echo "待機日数が 1 日未満: $$n" >&2; exit 1; }; \
	[ "$$(cooldown_days .github/dependabot.yml | wc -l)" -eq "$$(ecosystems .github/dependabot.yml)" ] \
		|| { echo ".github/dependabot.yml の cooldown が全ての ecosystem に付いていない" >&2; exit 1; }; \
	grep -qx 'strict-allow-scripts=true' .npmrc || { echo ".npmrc に strict-allow-scripts=true が無い" >&2; exit 1; }; \
	grep -qx 'no-build = true' core/pyproject.toml || { echo "core/pyproject.toml の [tool.uv] に no-build = true が無い" >&2; exit 1; }; \
	node -e 'const p = require("./package.json"); if (typeof p.allowScripts !== "object" || p.allowScripts === null) process.exit(1)' \
		|| { echo "package.json に allowScripts (install script のホワイトリスト) が無い" >&2; exit 1; }; \
	echo "ok  supply-chain ($$n 日)"

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
		&& sed -i "s/$(PLACEHOLDER)/$$PROJECT_NAME/" project.tmp/compose.yaml \
		&& mv project.tmp project || { rm -rf project.tmp; exit 1; }; }
	@[ -f project/.env ] || : > project/.env
	@[ -e project/repos.txt ] || cp -p templates/project/repos.txt project/repos.txt
	@grep -q '^PROJECT_GH_USER=..*' project/.env || { \
		sed -i '/^PROJECT_GH_USER=$$/d' project/.env \
		&& { [ -z "$$(tail -c1 project/.env)" ] || echo >> project/.env; } \
		&& printf 'PROJECT_GH_USER=%s\n' "$$GH_USER" >> project/.env \
		&& grep -q "^PROJECT_GH_USER=$$GH_USER$$" project/.env \
		&& echo "setup: project/.env に PROJECT_GH_USER=$$GH_USER を設定"; }
	@[ -e devcontainer.json ] || { cp -p templates/devcontainer.json devcontainer.json && sed -i "s/$(PLACEHOLDER)/$$PROJECT_NAME/" devcontainer.json; }
	@[ -e Dockerfile ] || bash core/bin/gen-dockerfile.sh > Dockerfile
	@UV_PROJECT=$(CURDIR)/core UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv core/uv-sync.sh --frozen
	@$(MAKE) --no-print-directory npm-ci

# package-lock.json を node_modules に再現する (setup と CI が呼ぶ)。lockfile の再現なので、解決時の門 (.npmrc の
# min-release-age) を外す。外さないと Dependabot の security update (cooldown が効かない) が 3 日未満の版を
# lockfile に入れたとき CI が落ちる (canon: facts/npm/min-release-age-gates-npm-ci,
# facts/dependabot/cooldown-exempts-security-updates)。npm 11 未満は .npmrc の min-release-age と
# strict-allow-scripts を知らずに黙って通すので止める。
npm-ci: ## package-lock.json を node_modules に再現する (npm 11 以上)
	@v=$$(npm --version) || exit 1; \
	[ "$${v%%.*}" -ge 11 ] 2>/dev/null || { echo "npm 11 以上が要る (.npmrc の min-release-age / strict-allow-scripts は npm 11 の設定): npm $$v" >&2; exit 1; }; \
	npm ci --min-release-age=0 --no-audit --no-fund && cp package-lock.json node_modules/.kit-package-lock.json
