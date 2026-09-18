# RAVEL V0
#
# Every target below is also reachable through scripts/, which carry the real
# logic. This file is a convenience index, not a second implementation.

.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help bootstrap env-check dev-up dev-down migrate test test-unit \
        test-integration test-dsh test-live test-e2e acceptance lint fmt \
        gateway tui acceptance-matrix clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install the Ubuntu canonical environment
	scripts/bootstrap_ubuntu.sh

env-check: ## Verify every Phase -1 dependency is usable
	$(PY) scripts/env_check.py

dev-up: ## Start PostgreSQL, Temporal, MinIO and wait for readiness
	scripts/dev_up.sh

dev-down: ## Stop infrastructure (keeps volumes)
	scripts/dev_down.sh

migrate: ## Apply database migrations to head
	$(VENV)/bin/alembic upgrade head

test: ## Unit + integration tests
	scripts/test_all.sh

test-unit: ## Unit tests only; no services required
	scripts/test_all.sh --unit

test-integration: ## Integration tests; requires make dev-up
	$(VENV)/bin/pytest tests/integration -m integration

test-dsh: ## Phase 0 DSH integration gate against a real pinned runtime
	RAVEL_REQUIRE_DSH=1 $(VENV)/bin/pytest tests/dsh -m dsh

test-live: ## Real-Internet research acceptance; never mocked
	scripts/test_live_research.sh

test-e2e: ## Headless loop and TUI end-to-end tests
	$(VENV)/bin/pytest tests/e2e -m e2e

acceptance: ## Run A01-A20 plus the extra gates
	$(VENV)/bin/pytest tests/acceptance -m acceptance

acceptance-matrix: ## Print the A01-A20 pass/fail matrix
	$(PY) scripts/acceptance_matrix.py

lint: ## Static checks
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

fmt: ## Auto-format
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

gateway: ## Run the RAVEL Gateway
	$(VENV)/bin/uvicorn ravel.gateway.app:create_app --factory --reload

tui: ## Run the local Textual TUI
	$(VENV)/bin/python -m ravel.tui

clean: ## Remove caches and build output
	rm -rf .pytest_cache .ruff_cache build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
