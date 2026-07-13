# Docker-first workflow. The app is only ever run in a container; these targets wrap
# the common docker/compose commands so you never need a local Python/Node toolchain.

COMPOSE_DEV := docker compose -f compose.dev.yml
IMAGE := energy-optimizer

.PHONY: help build build-dev up down logs dev test lint typecheck fe-build shell clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build the production image
	docker build --target runtime -t $(IMAGE):latest .

build-dev: ## Build the dev/test image
	docker build --target dev -t $(IMAGE):dev .

up: ## Run the production stack (detached)
	docker compose up -d --build

down: ## Stop the production stack
	docker compose down

logs: ## Tail production logs
	docker compose logs -f

dev: ## Run the hot-reloading dev server
	$(COMPOSE_DEV) up --build

test: build-dev ## Run the test suite in Docker
	$(COMPOSE_DEV) run --rm --no-deps app pytest -q

lint: build-dev ## Run ruff in Docker
	$(COMPOSE_DEV) run --rm --no-deps app ruff check src tests

typecheck: build-dev ## Run mypy in Docker
	$(COMPOSE_DEV) run --rm --no-deps app mypy src

fe-build: ## Build the SPA (inside a node container)
	docker run --rm -v "$(CURDIR)/frontend":/app/frontend -v "$(CURDIR)/src":/app/src \
		-w /app/frontend node:20-slim sh -c "npm install && npm run build"

shell: build-dev ## Open a shell in the dev image
	$(COMPOSE_DEV) run --rm --no-deps app bash

clean: ## Remove build artefacts and local data
	rm -rf frontend/node_modules frontend/dist src/energy_optimizer/web/static
	docker image rm $(IMAGE):latest $(IMAGE):dev 2>/dev/null || true
