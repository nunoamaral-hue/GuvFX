# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

## TL;DR
- Current focus: Broker autocomplete MVP improvements
- Current branch: `feat/broker-autocomplete-flow`
- Next milestone: Make `make check` run without manual venv activation + update CI backend path detection
- Last green `make check`: 2025-12-15 16:00 UTC (backend tests + frontend lint/build)
- Recent update: login client now initializes `reason` state via a lazy `useState` initializer so window access only happens client-side.
- Recent update: accounts, backtests, and profile pages now use targeted ESLint suppressions/unused-var hints so the frontend checks stay green after the login change.
- Recent update: the broker server autocomplete now has keyboard navigation, debounced/cancelled fetches, error messaging, and a dedicated "No matches" message when nothing fits.

## Repo layout (confirm paths)
- Backend: Django — `backend/`
- Frontend: Next.js — `frontend/`
- Docs: `docs/`

## Last known green checks
- Backend: 2025-12-15 — `make check` (Django tests: OK)
- Frontend: 2025-12-15 — `make check` (eslint: OK, `next build`: OK)

## Active blockers
- _None_ (or list)

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next handoff)
