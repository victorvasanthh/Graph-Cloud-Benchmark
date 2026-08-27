# Entry points for the whole workflow. Run `make help` for the list.
#
# Everything that needs containers is meant to run inside the GitHub Codespace
# defined by .devcontainer/devcontainer.json. Nothing here expects Docker on a
# developer laptop.
#
# Self-hosted targets are brought up ONE AT A TIME. The configured caps total
# 4 vCPU and 8 GB, which does not fit on a 2-core Codespace; and even where it
# fits, three idle databases holding page cache while a fourth is measured
# would be measuring the host. `make suite` handles the sequencing.

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
PYTHON  ?= python
SERVICE ?= neo4j

.DEFAULT_GOAL := help
.PHONY: help install data probe up down logs wait status smoke suite bench report \
        test lint fmt clean check secrets

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install pinned dependencies
	$(PYTHON) -m pip install --no-cache-dir -r requirements-dev.txt

env:  ## Write .env with generated passwords for the self-hosted targets
	$(PYTHON) scripts/init_env.py

data:  ## Download and checksum-verify the cit-HepTh dataset
	$(PYTHON) scripts/download_data.py

doctor:  ## Verify docker, compose, and that cgroup limits are really enforced
	bash scripts/check_runtime.sh

verify-config:  ## Show the commit, pinned vs running images, and resolved queries
	$(PYTHON) scripts/show_effective_config.py

reset:  ## Remove containers AND volumes, so a new image starts on clean data
	$(COMPOSE) down -v

probe:  ## Report the resource limits actually enforced, vs the config
	$(PYTHON) scripts/probe_limits.py

up:  ## Start ONE container: make up SERVICE=memgraph
	@test -f .env || { echo "no .env found; copy .env.example and fill it in"; exit 1; }
	$(COMPOSE) up -d $(SERVICE)
	@echo "started $(SERVICE); run 'make wait SERVICE=$(SERVICE)' before benchmarking"

wait:  ## Block until SERVICE reports healthy
	@timeout=300; while [ $$timeout -gt 0 ]; do \
		state=$$($(COMPOSE) ps --format '{{.Health}}' $(SERVICE) 2>/dev/null | head -1); \
		if [ "$$state" = "healthy" ]; then echo "$(SERVICE) healthy"; exit 0; fi; \
		if [ "$$state" = "unhealthy" ]; then echo "$(SERVICE) unhealthy"; exit 1; fi; \
		sleep 5; timeout=$$((timeout - 5)); \
	done; \
	echo "timed out waiting for $(SERVICE)"; $(COMPOSE) logs --tail=40 $(SERVICE); exit 1

status:  ## Show container status and enforced limits
	$(COMPOSE) ps
	@echo
	@$(PYTHON) scripts/probe_limits.py

down:  ## Stop every container, keeping volumes
	$(COMPOSE) down

logs:  ## Tail logs for SERVICE
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

smoke-managed:  ## Smoke ONLY CognoDB and Aura (no containers started)
	bash scripts/run_suite.sh --smoke --managed-only

smoke-local:  ## Smoke ONLY the four containers (no cloud calls)
	bash scripts/run_suite.sh --smoke --self-hosted-only

smoke:  ## Feasibility check: 1 iteration per workload, every target
	bash scripts/run_suite.sh --smoke

suite:  ## The real run: each target in isolation, then merge and report
	bash scripts/run_suite.sh

bench-one:  ## Start ONE engine, wait for it, measure it, tear it down
	@test -n "$(SERVICE)" || { echo "usage: make bench-one SERVICE=arangodb TARGET=arangodb"; exit 2; }
	@test -n "$(TARGET)"  || { echo "usage: make bench-one SERVICE=arangodb TARGET=arangodb"; exit 2; }
	$(COMPOSE) up -d $(SERVICE)
	$(PYTHON) scripts/wait_for_target.py $(TARGET) --timeout 300
	$(PYTHON) scripts/probe_limits.py || echo "  (limit findings above; recorded, continuing)"
	$(PYTHON) scripts/run_benchmark.py --target $(TARGET)
	$(COMPOSE) rm -sf $(SERVICE)

bench:  ## Measure a target that is ALREADY running (see bench-one)
	$(PYTHON) scripts/run_benchmark.py $(if $(TARGET),--target $(TARGET),)

report:  ## Rebuild the report and charts from the most recent run
	$(PYTHON) scripts/make_report.py

test:  ## Run the unit tests (no database required)
	$(PYTHON) -m pytest -m "not integration"

lint:  ## Check formatting and lint rules
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

secrets:  ## Fail if anything that looks like a credential is tracked by git
	$(PYTHON) scripts/scan_secrets.py

fmt:  ## Apply formatting and safe lint fixes
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format .

check: lint test secrets  ## Everything CI runs

clean:  ## Remove generated working files, keeping committed results
	rm -rf .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find results -name '*.tmp' -delete
	find results -name '*.partial' -delete
