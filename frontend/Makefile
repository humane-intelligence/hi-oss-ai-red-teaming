# All commands run inside Docker — the host only needs Docker + make.
export DOCKER_UID := $(shell id -u)
export DOCKER_GID := $(shell id -g)
export COMPOSE_IGNORE_ORPHANS := true

COMPOSE := docker compose
RUN := $(COMPOSE) run --rm web

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: env ## First run: install dependencies into ./node_modules
	$(RUN) npm install

.PHONY: dev
dev: env ## Start the Vite dev server at http://localhost:5173
	$(COMPOSE) up web

.PHONY: down
down: env ## Stop and remove containers
	$(COMPOSE) down

.PHONY: sh
sh: env ## Open a shell in the container
	$(RUN) sh

.PHONY: gen
gen: env ## Regenerate the typed API client from the backend OpenAPI (backend must be up)
	$(RUN) npm run gen

.PHONY: build
build: env ## Production build: type-check + bundle into ./dist
	$(RUN) npm run build

.PHONY: lint
lint: env ## Lint (ESLint) + type-check (tsc) + formatting, in CI's order
	$(RUN) npm run lint
	$(MAKE) typecheck
	$(MAKE) format-check

.PHONY: eslint
eslint: env ## Lint (ESLint only, no tsc)
	$(RUN) npm run lint

.PHONY: typecheck
typecheck: env ## Type-check with tsc
	$(RUN) npm run typecheck

.PHONY: format
format: env ## Format with Prettier
	$(RUN) npm run format

.PHONY: format-check
format-check: env ## Check formatting without rewriting
	$(RUN) npm run format:check

.PHONY: test
test: env ## Run tests (Vitest, single run). Narrow with ARGS='src/path/file.test.tsx'
	$(RUN) npm run test -- $(ARGS)

.PHONY: env
env:
	@[ -f .env ] || cp .env.example .env
