# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

## TL;DR
- Current focus: Broker autocomplete flow + continuity system activation (handoff-ready workflow)
- Current branch: `feat/broker-autocomplete-flow`
- Next milestone: Make `make check` run without manual venv activation + update CI backend path detection
- Recent update: login client now initializes `reason` state via a lazy `useState` initializer so window access only happens client-side.
- Recent update: accounts, backtests, and profile pages now use targeted ESLint suppressions/unused-var hints so the frontend checks stay green after the login change.

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
