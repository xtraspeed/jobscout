# Developer entrypoints. `make help` lists everything.

PY := python
VENV := .venv
BIN := $(VENV)/Scripts
PYTHON := $(BIN)/python
PIP := $(BIN)/pip
RUFF := $(BIN)/ruff
MYPY := $(BIN)/mypy
PYTEST := $(BIN)/pytest

.DEFAULT_GOAL := help
.PHONY: help venv install lint fmt typecheck test integration test-all cov api dashboard crawl-demo migrate up down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv (Python 3.11+)
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv ## Install runtime + dev dependencies
	$(PIP) install -e ".[dev]"

lint: ## Lint (ruff)
	$(RUFF) check src tests scripts
	$(RUFF) format --check src tests scripts

fmt: ## Autofix lint + format
	$(RUFF) check --fix src tests scripts
	$(RUFF) format src tests scripts

typecheck: ## Static types (mypy strict)
	$(MYPY) src scripts

test: ## Unit tests only (offline, temp SQLite)
	$(PYTEST) -m "not integration and not network"

integration: ## Integration tests (needs JOBSCOUT_TEST_DATABASE_URL)
	$(PYTEST) -m integration

test-all: ## Everything, including integration tests
	$(PYTEST)

cov: ## Unit tests with a coverage report
	$(PYTEST) -m "not integration and not network" --cov=jobscout --cov-report=term-missing

migrate: ## Apply migrations
	$(BIN)/alembic upgrade head

api: ## Run the query API with reload
	$(PYTHON) -m uvicorn jobscout.api.main:create_app --factory --reload --port 8000

dashboard: ## Run the Streamlit dashboard
	$(BIN)/streamlit run src/jobscout/dashboard/app.py

crawl-demo: ## Offline end-to-end crawl against recorded fixtures
	$(PYTHON) -m jobscout.cli crawl --target fixture --path tests/fixtures/demo_board

up: ## Build and start the full stack
	docker compose up --build --wait

down: ## Stop the stack
	docker compose down

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
