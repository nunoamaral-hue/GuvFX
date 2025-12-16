# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

-## TL;DR
- Current focus: Reapply broker autocomplete edge-case improvements (debounce, abort, keyboard nav) and keep client-only login parsing safe ahead of the next release.
- Current branch: `fix/broker-autocomplete-edgecases`
- Next milestone: Resolve the navigation/focus follow-up for broker autocomplete (P1 #4) and lock in the release narrative.
- Open PRs: 0

## Repo layout (confirm paths)
- Backend: Django — `backend/`
- Frontend: Next.js — `frontend/`
- Docs: `docs/`

## Last known green checks
- Backend: 2025-12-15 — GitHub Actions CI ✅ (Django tests) + `make check` local ✅
- Frontend: 2025-12-15 — GitHub Actions CI ✅ (lint + build) + `make check` local ✅

## Active blockers
- `make check` cannot finish because Django/postgres cannot open `127.0.0.1:5432` (Operation not permitted); tests will need a reachable PostgreSQL server.

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)
