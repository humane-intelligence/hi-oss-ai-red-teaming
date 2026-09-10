# Run containers as the host user so bind-mounted dirs (app/, alembic/) stay
# writable regardless of the uid baked into the image.
export DOCKER_UID := $(shell id -u)
export DOCKER_GID := $(shell id -g)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## First run: install + hooks + migrate + seed local admin
	$(MAKE) install
	$(MAKE) install-hooks
	$(MAKE) migrate
	$(MAKE) seedlocal

.PHONY: install
install: ## Sync dev + test deps from the lockfile (all groups)
	uv sync --locked --all-groups

.PHONY: install-hooks
install-hooks: ## Install prek git hooks into .git/hooks (run once after clone)
	uv run prek install

.PHONY: update
update: ## Upgrade all uv deps (relock) and prek hook versions
	uv lock --upgrade
	uv sync --locked --all-groups
	uv run prek auto-update

.PHONY: lock
lock: ## Relock uv.lock from pyproject (no upgrade; e.g. after editing requires-python)
	uv lock

.PHONY: updatehooks
updatehooks: ## Bump prek hook versions only (no dep relock)
	uv run prek auto-update

.PHONY: services
services: ## Start data services (db + redis) in background, wait for healthy
	[ -f .env ] || cp .env.example .env
	docker compose up -d --wait db redis

.PHONY: up
up: ## Start the full docker compose stack (app, worker, beat, flower, db, redis) detached
	[ -f .env ] || cp .env.example .env
	docker compose up -d --build

.PHONY: upllm
upllm: ## Start the full stack plus the opt-in self-hosted SLM (llama.cpp); first run downloads the model
	[ -f .env ] || cp .env.example .env
	docker compose --profile llm up -d --build

.PHONY: upmonitoring
upmonitoring: services ## Start postgres-exporter for the local monitoring stack (opt-in `monitoring` profile; run `make seedlocal` after, for the role it connects as)
	docker compose --profile monitoring up -d postgres-exporter

.PHONY: down
down: ## Stop and remove docker compose services (incl. opt-in profiles like the SLM)
	docker compose --profile "*" down

.PHONY: logs
logs: ## Tail docker compose logs (follow mode)
	docker compose logs -f

.PHONY: shell
shell: ## Exec bash inside the running app container
	docker compose exec app bash

.PHONY: dbshell
dbshell: services ## Open a pgcli session against the compose db on localhost
	set -a; . ./.env; set +a; PGPASSWORD="$$DATABASE_PASSWORD" uv run pgcli -h localhost -p $${DATABASE_PORT:-5432} -U "$$DATABASE_USER" -d "$$DATABASE_NAME"

.PHONY: lint
lint: ## Run the backend prek hooks (ruff, ty, deptry, bandit, housekeeping); frontend hooks are skipped
	SKIP=fe-eslint,fe-format-check uv run prek run --all-files --stage pre-push

# The generic hooks that police files no other CI job covers; kept in step with
# the pre-commit-hooks block in ../.pre-commit-config.yaml.
HOUSEKEEPING_HOOKS := trailing-whitespace end-of-file-fixer mixed-line-ending \
    check-yaml check-json check-toml check-merge-conflict check-case-conflict \
    detect-private-key

.PHONY: docslint
docslint: ## Repo-wide housekeeping hooks only (whitespace/EOF/EOL, yaml/json/toml syntax) — CI's gate for non-backend changes
	uv run prek run --all-files $(HOUSEKEEPING_HOOKS)

.PHONY: ty
ty: ## Run the ty type checker (also part of lint)
	uv run --no-sync ty check

.PHONY: deptry
deptry: ## Run deptry unused/missing-deps check (also part of lint)
	uv run --no-sync deptry .

.PHONY: audit
audit: ## Scan locked dependencies for known CVEs (pip-audit)
	uv run --no-sync --with pip-audit pip-audit

.PHONY: format
format: ## Run ruff format + ruff check --fix
	uv run ruff format .
	uv run ruff check --fix .

# Host pytest against compose db+redis; conftest.py pins settings before import,
# so .env is intentionally bypassed.
.PHONY: test
test: services ## Run pytest on host against compose services (ARGS=... e.g. -m unit, tests/api, -k health -x)
	uv run pytest $(ARGS)

.PHONY: test-cov
test-cov: services ## Pytest on host with coverage (term + HTML); also accepts ARGS=...
	uv run pytest --cov --cov-report=term-missing --cov-report=html $(ARGS)

.PHONY: coverage-md
coverage-md: ## Print the last coverage run as a markdown table (used by CI)
	uv run coverage report --format=markdown

.PHONY: check
check: ## Local gate: lint + test
	$(MAKE) lint
	$(MAKE) test

# Migrations run in Docker (prod-like env); alembic/ is mounted so generated files
# land on the host. --no-deps: make services already started db/redis.
.PHONY: migrate
migrate: services ## Apply all pending Alembic migrations (upgrade head)
	docker compose run --rm --no-deps app alembic upgrade head

.PHONY: makemigrations
makemigrations: services ## Generate a new migration: make makemigrations MSG='add users table'
	@test -n "$(MSG)" || (echo "Error: MSG is required.  Usage: make makemigrations MSG='description'"; exit 1)
	docker compose run --rm --no-deps app alembic revision --autogenerate -m "$(MSG)"

.PHONY: mergemigrations
mergemigrations: services ## Merge divergent heads after a branch merge: make mergemigrations MSG='merge X and Y'
	@test -n "$(MSG)" || (echo "Error: MSG is required.  Usage: make mergemigrations MSG='merge heads'"; exit 1)
	docker compose run --rm --no-deps app alembic merge heads -m "$(MSG)"

.PHONY: undomigration
undomigration: services ## Roll back the last applied migration (downgrade -1)
	docker compose run --rm --no-deps app alembic downgrade -1

.PHONY: showmigrations
showmigrations: services ## Show the full migration history
	docker compose run --rm --no-deps app alembic history --indicate-current

.PHONY: currentmigration
currentmigration: services ## Show the current applied revision
	docker compose run --rm --no-deps app alembic current

.PHONY: migrationsql
migrationsql: ## Emit pending migrations as raw SQL (no live DB needed)
	docker compose run --rm --no-deps app alembic upgrade --sql head

.PHONY: checkmigrations
checkmigrations: ## Fail if the migration history has more than one head (no live DB needed)
	@heads=$$(docker compose run --rm --no-deps app alembic heads | grep -c '(head)'); \
	if [ "$$heads" -gt 1 ]; then \
		echo "Error: $$heads migration heads found — a rebase/merge introduced a branch."; \
		echo "Repoint one migration's down_revision onto the other (see 'make showmigrations')."; \
		docker compose run --rm --no-deps app alembic heads; \
		exit 1; \
	fi

# Destroys all local data — never run against a shared environment.
.PHONY: resetdata
resetdata: ## Drop ALL compose volumes (incl. the SLM model cache) and re-migrate (local dev)
	docker compose --profile "*" down -v
	$(MAKE) migrate

.PHONY: syncroles
syncroles: services ## Sync canonical system roles (any env; run as a deploy step)
	docker compose run --rm --no-deps app python -m scripts.sync_roles

.PHONY: synclicenses
synclicenses: services ## Sync the curated data licenses (any env; run as a deploy step after migrate)
	docker compose run --rm --no-deps app python -m scripts.sync_licenses

.PHONY: syncannotationlabels
syncannotationlabels: services ## Sync the curated annotation labels (any env; run as a deploy step after migrate)
	docker compose run --rm --no-deps app python -m scripts.sync_annotation_labels

.PHONY: rewraptranscripts
rewraptranscripts: services ## Re-wrap sealed message text onto the active conversation key (rotation step; ARGS=--dry-run to just count)
	docker compose run --rm --no-deps app python -m scripts.rewrap_transcripts $(ARGS)

.PHONY: seedlocal
seedlocal: services ## Sync canonical roles and seed a full local dataset, one login per role (local only)
	docker compose run --rm --no-deps app python -m scripts.seed_local

.PHONY: emailpreview
emailpreview: ## Send one sample of every email template to the local Mailpit inbox (localhost:8025)
	[ -f .env ] || cp .env.example .env
	docker compose up -d --wait mailpit
	docker compose run --rm --no-deps -e EMAIL_BACKEND=smtp -e SMTP_HOST=mailpit -e SMTP_PORT=1025 -e SMTP_TLS=none app python -m scripts.preview_emails

.PHONY: openapidump
openapidump: ## Regenerate docs/openapi.yaml from the live FastAPI app
	[ -f .env ] || cp .env.example .env
	PYTHONPATH=. uv run python scripts/dump_openapi.py

.PHONY: erddump
erddump: ## Regenerate docs/erd.md (Mermaid ERD) from the SQLModel metadata
	PYTHONPATH=. uv run python scripts/dump_erd.py

.PHONY: permissionsdump
permissionsdump: ## Regenerate docs/permissions.md (roles/permissions matrix)
	PYTHONPATH=. uv run python scripts/dump_permissions.py

# Imports the app (needs .env) to layer required-permission notes onto openapi.yaml.
.PHONY: postmandump
postmandump: ## Regenerate docs/postman mirror collection from docs/openapi.yaml
	[ -f .env ] || cp .env.example .env
	PYTHONPATH=. uv run python scripts/dump_postman.py

.PHONY: postmanpush
postmanpush: ## Push generated collections to your Postman workspace (needs POSTMAN_API_KEY + POSTMAN_WORKSPACE_ID)
	@set -a; [ -f .env ] && . ./.env; set +a; PYTHONPATH=. uv run python scripts/push_postman.py

.PHONY: postmanpushdry
postmanpushdry: ## Preview what postmanpush would create/update (no mutations; works without creds)
	@set -a; [ -f .env ] && . ./.env; set +a; PYTHONPATH=. uv run python scripts/push_postman.py --dry-run

# Newman runs from the published image — no host node needed. `--network host` reaches localhost:8000.
# The third mount is writable on purpose: chaining exports the shared ids through it.
.PHONY: newman
newman: ## Run one self-contained Postman scenario collection under Newman (COLL=<name>, e.g. COLL=12-licensing)
	@[ -n "$(COLL)" ] || { echo "COLL is required — e.g. make newman COLL=12-licensing (a collection that needs 00-bootstrap goes to newmanchained)"; exit 2; }
	cd docs/postman && docker run --rm --network host \
	  -v "$$PWD/collections/scenarios:/s:ro" -v "$$PWD/environments:/env:ro" -v "$$PWD/environments:/out" \
	  postman/newman:latest run "/s/$(COLL).postman_collection.json" \
	  -e /env/local.postman_environment.json --reporters cli --reporter-cli-no-banner

.PHONY: newmanchained
newmanchained: ## Run a collection chained after 00-bootstrap, whose exported environment 02-06 all need (COLL=<name>)
	@[ -n "$(COLL)" ] || { echo "COLL is required — e.g. make newmanchained COLL=04-models"; exit 2; }
	cd docs/postman && docker run --rm --network host \
	  -v "$$PWD/collections/scenarios:/s:ro" -v "$$PWD/environments:/env:ro" -v "$$PWD/environments:/out" \
	  postman/newman:latest run /s/00-bootstrap.postman_collection.json \
	  -e /env/local.postman_environment.json --export-environment /out/chained.json \
	  --reporters cli --reporter-cli-no-banner \
	  && docker run --rm --network host \
	  -v "$$PWD/collections/scenarios:/s:ro" -v "$$PWD/environments:/out" \
	  postman/newman:latest run "/s/$(COLL).postman_collection.json" \
	  -e /out/chained.json --reporters cli --reporter-cli-no-banner
