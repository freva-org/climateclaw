.DEFAULT_GOAL := help
.PHONY: help install lint type test integration check up down

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies and git hooks
	uv sync --group dev
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push

lint: ## Run all pre-commit hooks (ruff, formatting, file checks) on all files
	uv run pre-commit run --all-files

type: ## Run mypy
	uv run --group type mypy

test: ## Run unit tests (no docker services needed)
	uv run --group test pytest -m "not integration"

integration: ## Run integration tests (needs `make up` first)
	CLIMATECLAW_CODE_SERVER_URL=http://localhost:8051 \
	CLIMATECLAW_WEB_SEARCH_SERVER_URL=http://localhost:8052 \
	uv run --group test pytest -m integration

check: lint type test ## Run lint, type and unit tests

up: ## Start the dev stack in the background
	./dev.sh up -d --build

down: ## Stop the dev stack
	./dev.sh down
