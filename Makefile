.DEFAULT_GOAL := help
SHELL := /bin/bash

# リポジトリルートの検証ハブ。`check` がルート成果物 (install.sh / templates) と core の検査を
# 集約する。流儀は core/Makefile 冒頭コメントが規範 (相互独立・並列可・skip ガード無し・副作用なし)。
# 検査は consumer 相当の project 層と kit venv を前提にする — fresh clone では先に `make setup`。
# CI (.github/workflows/check.yml) 限定でここに入らない検査: workflow lint (actionlint は CI でだけ
# 取得)・install.sh の冪等性・イメージ実ビルド・DNS egress (core の check-redact-dns-egress)。
MAKEFLAGS += --output-sync=target

# kit venv (`make setup` が作る。core/Makefile の $(PY) と同一実体)。
PY := $(CURDIR)/.venv/bin/python

CHECKS := check-core check-install-sh check-templates
.PHONY: help check setup $(CHECKS)

help: ## 一覧
	@sed -nE 's/^([a-zA-Z_-]+):.*## (.*)$$/  \1\t\2/p' $(MAKEFILE_LIST) | expand -t 20

check: $(CHECKS) ## ルート成果物 + core を検証

check-core: ## core の検査集約を委譲実行 (要 scaffold 済み project 層)
	@$(MAKE) -C core check

check-install-sh: ## install.sh を shellcheck (shellcheck 必須)
	@shellcheck -S warning install.sh && echo "ok  install.sh (shellcheck)"

check-templates: ## templates と .github の JSON/YAML が parse 可能か (kit venv の PyYAML 必須)
	@python3 -c 'import json; json.load(open("templates/claude/settings.json")); json.load(open("templates/devcontainer.json"))' \
	&& $(PY) -c 'import yaml; yaml.safe_load(open("templates/github/dependabot.yml")); yaml.safe_load(open(".github/dependabot.yml"))' \
	&& echo "ok  templates (json/yaml)"

PROJECT_NAME ?= kitci
setup: ## fresh clone を検査可能にする: consumer 相当の project 層 (既存ファイルは触らない) + kit venv
	@[ -e project ] || { cp -Rp templates/project project && sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' project/compose.yaml; }
	@[ -e devcontainer.json ] || { cp -p templates/devcontainer.json devcontainer.json && sed -i 's/@@PROJECT_NAME@@/$(PROJECT_NAME)/' devcontainer.json; }
	@[ -e Dockerfile ] || bash core/bin/gen-dockerfile.sh > Dockerfile
	@UV_PROJECT=$(CURDIR)/core UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv core/uv-sync.sh --frozen
