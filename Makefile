# Monorepo orchestrator: unified stack + delegation to backend/ and frontend/.
export DOCKER_UID := $(shell id -u)
export DOCKER_GID := $(shell id -g)
# Subdir composes see only their own services → silence cross-subdir "orphan" warnings.
export COMPOSE_IGNORE_ORPHANS := true
# FE→BE over the compose network (overrides the standalone host.docker.internal default).
export BACKEND_URL ?= http://app:8000

COMPOSE := docker compose
# Keep local config + the non-relocatable backend venv.
CLEAN_EXCLUDES ?= --exclude=.env --exclude=.venv --exclude=settings.local.json

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo "  be-<t> / fe-<t> / mon-<t>   delegate target <t> to backend/, frontend/, or monitoring/ (e.g. make be-migrate, make fe-gen, make mon-up)"

.PHONY: setup
setup: env ## First run: backend deps/hooks/migrate/seed + frontend setup
	$(MAKE) -C backend setup
	$(MAKE) -C frontend setup

.PHONY: dev
dev: env ## Start the whole stack (rebuilds changed images) in the foreground
	$(COMPOSE) up --build

.PHONY: up
up: env ## Start the whole stack (rebuilds changed images) detached
	$(COMPOSE) up -d --build

.PHONY: down
down: ## Stop and remove the whole stack (incl. opt-in profiles like the SLM)
	$(COMPOSE) --profile "*" down

.PHONY: logs
logs: ## Tail logs for the whole stack
	$(COMPOSE) logs -f

.PHONY: gen
gen: ## Contract loop: refresh backend OpenAPI + regenerate FE client (backend must be up)
	$(MAKE) -C backend openapidump
	$(MAKE) -C frontend gen

.PHONY: lint
lint: ## Static checks: backend (ruff/ty/deptry/bandit) + frontend (eslint + tsc)
	$(MAKE) -C backend lint
	$(MAKE) -C frontend lint

.PHONY: format
format: ## Auto-format backend + frontend
	$(MAKE) -C backend format
	$(MAKE) -C frontend format

.PHONY: test
test: ## Tests: backend + frontend
	$(MAKE) -C backend test
	$(MAKE) -C frontend test

.PHONY: check
check: ## Full quality gate: lint + test
	$(MAKE) lint
	$(MAKE) test

.PHONY: clean-dry
clean-dry: ## Preview what `make clean` would remove (repo-wide; keeps .env/.venv/local settings)
	git clean -fdx --dry-run $(CLEAN_EXCLUDES)

.PHONY: clean
clean: ## Remove untracked + ignored files repo-wide (keeps .env/.venv/local settings)
	git clean -fdx $(CLEAN_EXCLUDES)

.PHONY: env
env:
	@[ -f backend/.env ] || cp backend/.env.example backend/.env
	@[ -f frontend/.env ] || cp frontend/.env.example frontend/.env

be-%:
	$(MAKE) -C backend $*

fe-%:
	$(MAKE) -C frontend $*

mon-%:
	$(MAKE) -C monitoring $*
