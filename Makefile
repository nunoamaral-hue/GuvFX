.PHONY: help check backend-test backend-lint frontend-lint frontend-build secret-scan governance-check research-check research-foundation-check market-data-check require-research-venv

help:
	@echo "Targets: check backend-test frontend-lint frontend-build secret-scan governance-check research-check market-data-check"

check: governance-check backend-test frontend-lint frontend-build

secret-scan:
	python3 scripts/check_no_secrets.py

governance-check: secret-scan
	python3 -m unittest discover -s tests -p 'test_no_secrets.py'

backend-test:
	@if [ -f backend/manage.py ]; then \
		if [ -x backend/.venv/bin/python ]; then \
			cd backend && .venv/bin/python manage.py test; \
		elif command -v python3 >/dev/null 2>&1; then \
			cd backend && python3 manage.py test; \
		else \
			echo "Error: Python not found. Install Python 3.11+ or create backend/.venv"; \
			exit 1; \
		fi; \
	else \
		echo "Skipping backend-test: backend/manage.py not found"; \
	fi

frontend-lint:
	@if [ -f frontend/package.json ]; then \
		cd frontend && npm run lint; \
	else \
		echo "Skipping frontend-lint: frontend/package.json not found"; \
	fi

frontend-build:
	@if [ -f frontend/package.json ]; then \
		cd frontend && npm run build; \
	else \
		echo "Skipping frontend-build: frontend/package.json not found"; \
	fi

# Research + market-data foundations (GFX-PKT-005B / 006C). Separate targets;
# NOT part of `check`. Require the isolated DuckDB venv (see research/README.md).
require-research-venv:
	@if [ ! -x .venv-research/bin/python ]; then \
		echo "Error: .venv-research not found. Create it first:"; \
		echo "  python3 -m venv .venv-research"; \
		echo "  .venv-research/bin/python -m pip install --only-binary=:all: --no-deps -r requirements-research.txt"; \
		exit 1; \
	fi

research-foundation-check: require-research-venv
	.venv-research/bin/python tools/research_smoke.py
	.venv-research/bin/python -m unittest discover -s tests -p 'test_research_foundation.py' -v

# GFX-PKT-006C synthetic market-data gate.
market-data-check: require-research-venv
	.venv-research/bin/python tools/market_data_synthetic_smoke.py
	.venv-research/bin/python -m unittest discover -s tests -p 'test_market_data_foundation.py' -v

# research-check composes both foundations so the market-data gate is not bypassed.
research-check: research-foundation-check market-data-check
