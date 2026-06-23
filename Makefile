.PHONY: help check backend-test backend-lint frontend-lint frontend-build secret-scan governance-check research-check

help:
	@echo "Targets: check backend-test frontend-lint frontend-build secret-scan governance-check research-check"

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

# Research foundation (GFX-PKT-005B). Separate target; NOT part of `check`.
# Requires the isolated DuckDB venv created per research/README.md.
research-check:
	@if [ ! -x .venv-research/bin/python ]; then \
		echo "Error: .venv-research not found. Create it first:"; \
		echo "  python3 -m venv .venv-research"; \
		echo "  .venv-research/bin/python -m pip install --only-binary=:all: --no-deps -r requirements-research.txt"; \
		exit 1; \
	fi
	.venv-research/bin/python tools/research_smoke.py
	.venv-research/bin/python -m unittest discover -s tests -p 'test_research_foundation.py' -v
