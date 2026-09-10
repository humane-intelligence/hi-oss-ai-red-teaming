# Local test/dev harness for this stack — see README.md "Local testing".
# On the box the stack runs directly from docker-compose.yml (see README.md);
# here it overlays docker-compose.local.yml (network name, Grafana URL/port).
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.local.yml
SERVICES := prometheus loki promtail grafana celery-exporter node-exporter cadvisor blackbox-exporter glitchtip glitchtip-db

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up: ## Bring up the whole stack (Prometheus/Loki/Grafana/exporters/GlitchTip) against the local `make dev` stack
	[ -f .env ] || cp .env.example .env
	$(COMPOSE) up -d $(SERVICES)

.PHONY: down
down: ## Stop and remove the local monitoring stack
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail logs for the local monitoring stack
	$(COMPOSE) logs -f
