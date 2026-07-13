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

OFFLINE_CHECKS := check-core check-install-sh check-templates check-placeholder
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
	@curl -fsSL $(REVIEW_MD_UPSTREAM) -o REVIEW.md

# 置換漏れの devcontainer.json 等は silent に出荷されるため pin (AGENTS.md 再発防止の規律「既定は fail-closed」)。
# 1 行目は negative probe — 検出対象の token が templates で実際に使われていることを確認し、
# templates 側の placeholder 改名で検査が空回りするのを防ぐ。
# 2 行目を `! grep` に戻さない: 対象欠落 (grep exit 2) まで成功に反転する。不一致 (exit 1) だけが成功。
check-placeholder: ## scaffold 産物に置換漏れ @@PROJECT_NAME@@ が無いか (要 setup 済み)
	@grep -rq '@@PROJECT_NAME@@' templates || { echo "negative probe: templates に @@PROJECT_NAME@@ が無い — 検査対象の token が drift" >&2; exit 1; }
	@grep -rn '@@PROJECT_NAME@@' project devcontainer.json Dockerfile; [ $$? -eq 1 ] && echo "ok  placeholder (@@PROJECT_NAME@@ 置換済み)"

PROJECT_NAME ?= kitci
setup: ## fresh clone を検査可能にする: consumer 相当の project 層 (既存ファイルは触らない) + kit venv
	@[ -e project ] || { cp -Rp templates/project project && sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' project/compose.yaml; }
	@[ -e devcontainer.json ] || { cp -p templates/devcontainer.json devcontainer.json && sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' devcontainer.json; }
	@[ -e Dockerfile ] || bash core/bin/gen-dockerfile.sh > Dockerfile
	@UV_PROJECT=$(CURDIR)/core UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv core/uv-sync.sh --frozen
