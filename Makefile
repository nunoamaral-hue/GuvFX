.PHONY: help check backend-test backend-lint frontend-lint frontend-build

help:
	@echo "Targets: check backend-test frontend-lint frontend-build"

check: backend-test frontend-lint frontend-build

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
