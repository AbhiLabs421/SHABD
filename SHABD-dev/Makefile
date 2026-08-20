# SHABD — single entry point for everyday operations.
#
# Typical flows:
#     make install        # set up local dev env
#     make test           # run all 49 tests
#     make demo           # launch a local server with sample spells
#     make docker         # build the production container
#     make compose        # run SHABD + Prometheus + Grafana
#     make bench          # run the benchmark
#     make publish        # tag, build, and publish to PyPI

PYTHON ?= python3
PORT   ?= 8765
SECRET ?= $(shell openssl rand -hex 32)

.PHONY: help install lint typecheck test test-core test-enterprise \
        comparison demo docker compose down bench publish clean

help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dev tools (ruff, mypy). SHABD itself has no runtime deps.
	$(PYTHON) -m pip install --upgrade pip ruff mypy build twine

lint: ## Run ruff
	ruff check shabd.py shabd_client.py tests/ examples/ scripts/

typecheck: ## Run mypy (lenient)
	mypy --ignore-missing-imports --no-strict-optional shabd.py shabd_client.py || true

test: test-core test-enterprise ## Run all 49 tests

test-core: ## Run the 31 core tests
	$(PYTHON) tests/test_shabd.py

test-enterprise: ## Run the 18 enterprise tests
	$(PYTHON) tests/test_enterprise.py

comparison: ## Live SHABD-vs-FastMCP matrix (needs `pip install fastmcp`)
	$(PYTHON) tests/test_comparison.py

demo: ## Launch the demo server on $(PORT)
	SHABD_SECRET=$(SECRET) $(PYTHON) examples/my_spells.py

docker: ## Build the production container as shabd:2.2
	docker build -t shabd:2.2 .

compose: ## Bring up SHABD + Prometheus + Grafana
	SHABD_SECRET=$(SECRET) docker compose up -d
	@echo "SHABD:      http://localhost:8765/dashboard"
	@echo "Prometheus: http://localhost:9090"
	@echo "Grafana:    http://localhost:3000 (admin/admin)"

down: ## Stop the compose stack
	docker compose down

bench: ## Run the throughput benchmark
	$(PYTHON) bench/run.py

publish: ## Build + upload to PyPI (needs PYPI_API_TOKEN)
	rm -rf dist build *.egg-info
	$(PYTHON) -m build
	$(PYTHON) -m twine upload dist/*

clean: ## Wipe build artifacts and caches
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
