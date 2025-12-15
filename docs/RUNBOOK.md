# Runbook (Local Dev + Verification)

> Keep this current. If paths or commands differ, update this file.

## Prereqs
- Python 3.11+
- Node.js (LTS recommended)
- PostgreSQL
- (Optional) Redis for Celery/background tasks

## Repo structure (edit to match reality)
- Backend path: `backend`
- Frontend path: `frontend`
- Note: `frontend/.next/` is a build artifact (do **not** edit; do **not** treat as source)

---

## Backend (Django)

### Setup
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> One-time setup: you only need to create the venv and install requirements once. After that, `make check` runs tests using `backend/.venv/bin/python` and does **not** require manually activating the venv.

### Migrations
```bash
python manage.py migrate
```

### Run server
```bash
python manage.py runserver
```

### Tests
```bash
python manage.py test
```

> If you see “permission denied to create database” during tests, ensure your test DB user has CREATE DATABASE rights, or configure Django to use an existing test DB.

---

## Frontend (Next.js)

### Install
```bash
cd frontend
npm install  # or: npm ci (if you have a lockfile and want reproducible installs)
```

### Dev
```bash
npm run dev
```

### Lint + Build (must be green before handoff)
```bash
npm run lint
npm run build
```

---

## One-command checks (recommended)
This repo includes a `Makefile`. After one-time setup (venv + npm deps), run checks from the repo root:

```bash
make check
```

### One-time setup (required before the first `make check`)

```bash
# Backend deps
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# Frontend deps
cd frontend
npm install
cd ..
```

If `make check` fails, record the error and workaround in `docs/KNOWN_ISSUES.md` and keep this runbook updated.
