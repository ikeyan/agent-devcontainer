.DEFAULT_GOAL := help
SHELL := /bin/bash

# リポジトリルートの検証ハブ。`check` = `check-offline` (ルート成果物 + core) + `check-online` (ネット要)。
# 流儀は core/Makefile 冒頭コメントが規範 (相互独立・並列可・skip ガード無し・副作用なし)。
# 検査は consumer 相当の project 層と kit venv を前提にする — fresh clone では先に `make setup`。
# CI (.github/workflows/check.yml) 限定でここに入らない検査: workflow lint (actionlint は CI でだけ
# 取得)・install.sh の冪等性・イメージ実ビルド・DNS egress (core の check-redact-dns-egress)。
MAKEFLAGS += --output-sync=target

# kit venv (`make setup` が作る。core/Makefile の $(PY) と同一実体)。
PY := $(CURDIR)/.venv/bin/python

OFFLINE_CHECKS := check-core check-install-sh check-templates check-placeholder check-contamination
.PHONY: help check check-offline check-online setup check-review-md sync-review-md $(OFFLINE_CHECKS)

help: ## 一覧
	@sed -nE 's/^([a-zA-Z_-]+):.*## (.*)$$/  \1\t\2/p' $(MAKEFILE_LIST) | expand -t 20

check: check-offline check-online ## 全検証 (offline + online)

check-offline: $(OFFLINE_CHECKS) ## ルート成果物 + core のネット不要検証

check-core: ## core の検査集約を委譲実行 (要 scaffold 済み project 層)
	@$(MAKE) -C core check

check-install-sh: ## install.sh を shellcheck (shellcheck 必須)
	@shellcheck -S warning install.sh && echo "ok  install.sh (shellcheck)"

check-templates: ## templates と .github の JSON/YAML が parse 可能か (kit venv の PyYAML 必須)
	@python3 -c 'import json; json.load(open("templates/claude/settings.json")); json.load(open("templates/devcontainer.json"))' \
	&& $(PY) -c 'import yaml; yaml.safe_load(open("templates/github/dependabot.yml")); yaml.safe_load(open(".github/dependabot.yml"))' \
	&& echo "ok  templates (json/yaml)"

check-online: check-review-md ## ネット必須の検証 (REVIEW.md 正本一致)

REVIEW_MD_UPSTREAM := https://raw.githubusercontent.com/ikeyan/agent-files/main/REVIEW.md

# 正本は外部 repo の main なので、drift は PR の内容と無関係に発生し得る。PR の CI は DRIFT_CHECK=warn で
# 警告 (GitHub annotation) に降格する (check.yml)。既定は fail。降格するのは「比較が成立して不一致
# (diff exit 1)」のみ — 取得失敗・比較不能は warn でも fail (未検査を緑にしない)。
check-review-md: ## REVIEW.md が正本 (ikeyan/agent-files) の最新版と一致するか (ネット要。DRIFT_CHECK=warn で drift を警告降格)
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	curl -fsSL $(REVIEW_MD_UPSTREAM) -o "$$tmp" || exit; \
	diff "$$tmp" REVIEW.md; st=$$?; \
	case $$st in \
	  0) echo "ok  REVIEW.md (agent-files 正本と一致)";; \
	  1) [ "$$DRIFT_CHECK" = warn ] && echo "::warning::REVIEW.md が agent-files 正本から drift — make sync-review-md で追従";; \
	  *) exit $$st;; \
	esac

sync-review-md: ## REVIEW.md を正本 (ikeyan/agent-files) の最新版で上書き (ネット要)
	@curl -fsSL --remove-on-error $(REVIEW_MD_UPSTREAM) -o REVIEW.md

# 置換漏れの devcontainer.json 等は silent に出荷されるため pin (AGENTS.md 再発防止の規律「既定は fail-closed」)。
# 1 行目は negative probe — 検出対象の token が templates で実際に使われていることを確認し、
# templates 側の placeholder 改名で検査が空回りするのを防ぐ。
# 2 行目を `! grep` に戻さない: 対象欠落 (grep exit 2) まで成功に反転する。不一致 (exit 1) だけが成功。
check-placeholder: ## scaffold 産物に置換漏れ @@PROJECT_NAME@@ が無いか (要 setup 済み)
	@grep -rq '@@PROJECT_NAME@@' templates || { echo "negative probe: templates に @@PROJECT_NAME@@ が無い — 検査対象の token が drift" >&2; exit 1; }
	@grep -rn '@@PROJECT_NAME@@' project devcontainer.json Dockerfile; [ $$? -eq 1 ] && echo "ok  placeholder (@@PROJECT_NAME@@ 置換済み)"

# consumer 固有値の core/templates への再混入を防ぐ (kit issue #10 の再発防止)。禁止 token は
# `ikeyan` (kit 自身の canonical 参照だけ allowlist) と旧 project 名の残滓 `tools-redact` / `tools_`。
# `ikeyan.github.io` / `ikeyan/tools` は `ikeyan` 規則が包含する。allowlist は行単位でなく token 単位
# (sed で無害化してから再 grep) — 同一行に allowlist 対象と違反が同居しても違反が残って検出される。
#  - repo slug は直後の `/` まで含めて固定: 接頭辞拡張 (例: ikeyan/agent-devcontainer-x = 別 repo) は
#    無害化されず `ikeyan` が残って検出される。裸 slug (末尾 `/` 無し) も allowlist 外 = 検出される。
#  - `tools-redact`/`tools_` は直前が英数の場合のみ無害化: 別語の部分一致 (例: wheel 正規化名の
#    setuptools_scm) を誤検出しない。残滓の実形 (tools_proxy-bw 等) は行頭/記号の後に現れ検出が残る。
# core/docs/verified-facts/ は実測記録の歴史的台帳なので対象外。fail-closed: 走査エラー (grep exit>1 =
# 読めないファイル等 / find 非 0 / sanitize sed の失敗) を「クリーン」へ反転させず PIPESTATUS で明示
# fail し (-a で binary も本文走査)、ファイル/ディレクトリ名への混入も同じ流儀で走査する。
# negative probe: FORBIDDEN_RE の各 token (regex の | 区切りから導出 — token 追加に fixture が自動追従。
# token は literal 前提) + allowlist 同居行 + slug 接頭辞拡張 + ファイル名 fixture を検出でき、かつ
# 純 allowlist/別語 fixture を誤検出しないことを確認してから本走査する。
FORBIDDEN_RE := ikeyan|tools-redact|tools_
check-contamination: ## core/ と templates/ に consumer 固有値 (ikeyan / tools-redact / tools_) が混入していないか
	@[ -d core ] && [ -d templates ] || { echo "core/ か templates/ が無い (走査対象欠落)" >&2; exit 1; }; \
	scan() { local raw st ps; raw=$$(grep -rnaE '$(FORBIDDEN_RE)' --exclude-dir=verified-facts "$$@"); st=$$?; \
		[ $$st -le 1 ] || return 2; \
		printf '%s\n' "$$raw" | sed -e 's#ikeyan/agent-devcontainer/#ALLOWED/#g' \
			-e 's#ikeyan/agent-files/#ALLOWED/#g' \
			-e 's#\([A-Za-z0-9]\)tools-redact#\1ALLOWED#g' \
			-e 's#\([A-Za-z0-9]\)tools_#\1ALLOWED_#g' \
			| grep -E '$(FORBIDDEN_RE)'; ps=("$${PIPESTATUS[@]}"); \
		[ "$${ps[1]}" -eq 0 ] || return 2; return "$${ps[2]}"; }; \
	name_scan() { local all ps; all=$$(find "$$@" -path '*/verified-facts' -prune -o -print) || return 2; \
		printf '%s\n' "$$all" | grep -E '$(FORBIDDEN_RE)'; ps=("$${PIPESTATUS[@]}"); return "$${ps[1]}"; }; \
	probe=$$(mktemp -d); \
	toks=$$(printf '%s' '$(FORBIDDEN_RE)' | tr '|' ' '); \
	{ for tok in $$toks; do echo "plain $$tok"; done; \
	  echo "coexist github.com/ikeyan/agent-devcontainer/blob と ikeyan.github.io の同居"; \
	  echo "prefix1 ikeyan/agent-devcontainer-x"; \
	  echo "prefix2 ikeyan/agent-files-x"; } > "$$probe/f"; \
	hits=$$(scan "$$probe"); \
	for tok in $$toks ikeyan.github.io agent-devcontainer-x agent-files-x; do \
		echo "$$hits" | grep -qF "$$tok" \
			|| { echo "negative: 混入検出器が fixture の $$tok を素通し" >&2; rm -rf "$$probe"; exit 1; }; \
	done; rm -rf "$$probe"; \
	clean=$$(mktemp -d); \
	printf 'https://raw.githubusercontent.com/ikeyan/agent-devcontainer/main/install.sh\nsetuptools_scm-8.whl と mytools-redact-probe\n' > "$$clean/f"; \
	scan "$$clean" >/dev/null; st=$$?; rm -rf "$$clean"; \
	[ $$st -eq 1 ] || { echo "negative: 純 allowlist/別語 fixture を誤検出か走査エラー (st=$$st)" >&2; exit 1; }; \
	nprobe=$$(mktemp -d); mkdir "$$nprobe/ikeyan-dir"; : > "$$nprobe/tools_probe"; \
	nhits=$$(name_scan "$$nprobe"); \
	{ echo "$$nhits" | grep -q 'ikeyan-dir' && echo "$$nhits" | grep -q 'tools_probe'; } \
		|| { echo "negative: ファイル名検出器が fixture を素通し" >&2; rm -rf "$$nprobe"; exit 1; }; \
	rm -rf "$$nprobe"; \
	hits=$$(scan core templates); st=$$?; \
	[ $$st -ne 2 ] || { echo "混入走査が走査エラーで不完全 (読めないファイル/sed 失敗?)" >&2; exit 1; }; \
	[ $$st -eq 1 ] || { echo "consumer 固有値が core/templates に混入 (project 層へ移すか allowlist を見直す):" >&2; \
		echo "$$hits" >&2; exit 1; }; \
	names=$$(name_scan core templates); st=$$?; \
	[ $$st -ne 2 ] || { echo "ファイル名走査が走査エラーで不完全" >&2; exit 1; }; \
	[ $$st -eq 1 ] || { echo "consumer 固有値がファイル/ディレクトリ名に混入:" >&2; echo "$$names" >&2; exit 1; }; \
	echo "ok  contamination (core/templates に consumer 固有値なし)"

# PROJECT_NAME は compose project 名 ([a-z0-9][a-z0-9_-]*)、GH_USER は GitHub user 名 ([A-Za-z0-9-]+、
# Dockerfile が assert)。charset が異なる別概念なので変数を分ける (例: PROJECT_NAME=my_proj は valid
# だが gh user としては invalid — 混用すると image build まで失敗が遅延する)。
# charset は sed 実行前に検証する (どちらの charset も sed 特殊文字 / & \ を含まない = 置換が安全)。
# scaffold は project.tmp に組み立ててから mv する — 途中失敗の半端な project/ を残すと存在ガードで
# 再実行が no-op になり回復不能になるため。置換は grep で事後 assert (sed s/// は不一致でも exit 0 —
# @@PROJECT_NAME@@ 側の残存は check-placeholder が pin 済み)。既存 project/ は原則触らないが、
# PROJECT_GH_USER の欠落だけは補完する — この変数の導入前に scaffold した checkout でも
# `make setup` が文書どおりの復旧手段として機能するように (kit repo の project/ は gitignore された
# 検査用 artifact であり consumer 所有物ではない)。
PROJECT_NAME ?= kitci
GH_USER ?= kitci
setup: ## fresh clone を検査可能にする: consumer 相当の project 層 + kit venv (既存 project は PROJECT_GH_USER 欠落だけ補完)
	@[[ "$(PROJECT_NAME)" =~ ^[a-z0-9][a-z0-9_-]*$$ ]] || { echo "PROJECT_NAME が不正 ([a-z0-9][a-z0-9_-]*): '$(PROJECT_NAME)'" >&2; exit 1; }
	@[[ "$(GH_USER)" =~ ^[A-Za-z0-9-]+$$ ]] || { echo "GH_USER が不正 (GitHub user 名 [A-Za-z0-9-]+): '$(GH_USER)'" >&2; exit 1; }
	@[ -e project ] || { rm -rf project.tmp && cp -Rp templates/project project.tmp \
		&& sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' project.tmp/compose.yaml \
		&& sed -i 's/^PROJECT_GH_USER=$$/PROJECT_GH_USER=$(GH_USER)/' project.tmp/.env \
		&& grep -q '^PROJECT_GH_USER=$(GH_USER)$$' project.tmp/.env \
		&& mv project.tmp project || { rm -rf project.tmp; exit 1; }; }
	@grep -q '^PROJECT_GH_USER=..*' project/.env || { \
		sed -i '/^PROJECT_GH_USER=$$/d' project/.env \
		&& printf 'PROJECT_GH_USER=%s\n' '$(GH_USER)' >> project/.env \
		&& echo "setup: project/.env に PROJECT_GH_USER=$(GH_USER) を補完 (この変数の導入前の scaffold を修復)"; }
	@[ -e devcontainer.json ] || { cp -p templates/devcontainer.json devcontainer.json && sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' devcontainer.json; }
	@[ -e Dockerfile ] || bash core/bin/gen-dockerfile.sh > Dockerfile
	@UV_PROJECT=$(CURDIR)/core UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv core/uv-sync.sh --frozen
