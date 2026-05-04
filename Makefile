PY := .venv/bin/python
PIP := .venv/bin/pip
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy

.DEFAULT_GOAL := help

# ----------------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------------
.PHONY: install
install:  ## Install runtime + dev deps into .venv
	$(PIP) install -e ".[dev]"

# ----------------------------------------------------------------------------
# Quality gates -- these are the same gates CI runs.
# ----------------------------------------------------------------------------
.PHONY: lint
lint:  ## Ruff lint (reads pyproject.toml)
	$(RUFF) check derived ingestion orchestration scripts serving tests

.PHONY: format
format:  ## Auto-format with ruff
	$(RUFF) format derived ingestion orchestration scripts serving tests

.PHONY: typecheck
typecheck:  ## Mypy strict
	$(MYPY) derived ingestion orchestration scripts serving

.PHONY: test
test:  ## Pytest (skips live_pg unless PG_TEST_DSN is set)
	$(PYTEST)

.PHONY: test-live
test-live:  ## Pytest including live_pg (requires PG_TEST_DSN)
	$(PYTEST) -m "not slow"

.PHONY: check
check: lint typecheck test  ## Full local CI -- run before pushing

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
.PHONY: migrate
migrate:  ## Apply all pending SQL migrations against $$PG_DSN
	$(PY) -m scripts.migrate apply

.PHONY: migrate-status
migrate-status:  ## Show which migrations have been applied
	$(PY) -m scripts.migrate status

.PHONY: seed
seed:  ## Apply db/seeds/*.sql in order
	$(PY) -m scripts.migrate seed

# ----------------------------------------------------------------------------
# Help
# ----------------------------------------------------------------------------
.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*?##/ \
	  { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
