# RAVEL V0
#
# Every target below is also reachable through scripts/, which carry the real
# logic. This file is a convenience index, not a second implementation.

.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
# Pyright is a Node tool, not a Python package, so it is not in the venv. It is
# on PATH after `make bootstrap`; override it with `make lint PYRIGHT=...` if
# this machine keeps it somewhere else.
PYRIGHT ?= pyright

# Source .env if it exists so credential-dependent targets work from a clean
# shell. `scripts/test_all.sh` does this itself; the targets below do not.
WITH_ENV := set -a; . ./.env 2>/dev/null || true; set +a;

.PHONY: help bootstrap env-check dev-up dev-down migrate test test-unit \
        test-integration test-dsh test-live test-e2e acceptance lint fmt \
        typecheck gateway tui acceptance-matrix acceptance-raw clean \
        up worker project account

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
	$(WITH_ENV) RAVEL_REQUIRE_DSH=1 $(VENV)/bin/pytest tests/dsh -m dsh

test-live: ## Real-Internet research acceptance; never mocked
	$(WITH_ENV) scripts/test_live_research.sh

test-e2e: ## Headless loop and TUI end-to-end tests
	$(VENV)/bin/pytest tests/e2e -m e2e

# The matrix and the run are one command. Phase 9's gate is that the acceptance
# run *prints a pass/fail matrix* — a pytest summary says how many tests passed,
# not which of the twenty items they were about, and says nothing about an item
# nobody wrote a test for. `scripts/acceptance_matrix.py` runs the suite, streams
# its output, and then answers the item-level question; the two names below are
# the same command, kept because both are written down elsewhere.
acceptance: ## Run A01-A20, the extra gates, and print the pass/fail matrix
	$(PY) scripts/acceptance_matrix.py

acceptance-matrix: acceptance ## Print the A01-A20 pass/fail matrix

# Run the suite without the matrix, for iterating on one item.
acceptance-raw: ## Run the acceptance suite, no matrix
	$(VENV)/bin/pytest tests/acceptance -m acceptance

# The checks that decide whether the tree is acceptable: the linter for style
# and likely mistakes, and the type checker for the interfaces between modules.
# `ruff format` is deliberately not one of them — the wrapping in this codebase
# is hand-authored so that a line break falls where the thought does, and a
# formatter that rewrote 88 files to no semantic effect would make every later
# diff unreadable. `ruff check` is the gate; run it before committing.
lint: ## Static checks
	$(VENV)/bin/ruff check src tests
	$(PYRIGHT) src tests

fmt: ## Fix what the linter can fix on its own
	$(VENV)/bin/ruff check --fix src tests

typecheck: ## Type-check the source and the tests
	$(PYRIGHT) src tests

gateway: ## Run the RAVEL Gateway
	$(VENV)/bin/uvicorn ravel.gateway.app:create_app --factory --reload

tui: ## Run the local Textual TUI
	$(VENV)/bin/python -m ravel.tui

# The four below are how a deployment is run rather than tested. `up` is the
# whole of V0: infrastructure, migrations, the Gateway, the execution worker,
# and the console a person sits at.
up: ## Start RAVEL V0 on this machine (infra, Gateway, worker, TUI)
	scripts/run_v0.sh

worker: ## Run the execution worker; V0 registers the mock compute and lab backends
	$(PY) scripts/run_worker.py

project: ## Drive one project to an ending; make project PROJECT=<project_id>
	@test -n "$(PROJECT)" || { echo "usage: make project PROJECT=<project_id>"; exit 2; }
	$(PY) scripts/run_project.py --project $(PROJECT)

account: ## Create an account; make account ARGS="--username ada --new-project ..."
	$(PY) scripts/create_account.py $(ARGS)

clean: ## Remove caches and build output
	rm -rf .pytest_cache .ruff_cache build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
