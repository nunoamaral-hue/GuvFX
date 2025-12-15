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
npm install
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
This repo includes a `Makefile`. Use:
```
make check
```
If `make check` fails, record the error and workaround in `docs/KNOWN_ISSUES.md` and keep this runbook updated.
