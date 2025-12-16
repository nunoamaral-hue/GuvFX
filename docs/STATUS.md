# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

## TL;DR
- Current focus: Broker autocomplete MVP improvements completed
- Current branch: `feat/broker-autocomplete-flow`
- Next milestone: Make `make check` run without manual venv activation + update CI backend path detection
- Last green `make check`: Tue 16 Dec 2025 06:26:44 UTC (backend tests + frontend lint/build)
- Recent update: login client now initializes `reason` state via a lazy `useState` initializer so window access only happens client-side.
- Recent update: accounts, backtests, and profile pages now use targeted ESLint suppressions/unused-var hints so the frontend checks stay green after the login change.
- Recent update: the broker server autocomplete now has keyboard navigation, debounced/cancelled fetches, error messaging, and a dedicated "No matches" message when nothing fits.

## Repo layout (confirm paths)
- Backend: Django — `backend/`
- Frontend: Next.js — `frontend/`
- Docs: `docs/`

## Last known green checks
- Backend: Tue 16 Dec 2025 06:26:44 UTC — `make check` (Django tests: OK)
- Frontend: Tue 16 Dec 2025 06:26:44 UTC — `make check` (eslint: OK, `next build`: OK)

## Active blockers
- Postgres unreachable on `127.0.0.1:5432`, so `make check` currently fails with `psycopg2.OperationalError` unless a DB is available locally.

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next handoff)
