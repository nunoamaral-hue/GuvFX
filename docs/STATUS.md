# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

## TL;DR
- Current focus: VPS production rollout (Traefik+Let’s Encrypt) + MT5 handoff automation and MT5 mouse reliability.
- Current branch: `fix/broker-autocomplete-edgecases`
- Next milestone: Resolve the navigation/focus follow-up for broker autocomplete (P1 #4) while keeping the VPS/MT5 flow stable.
- Production URLs: `https://guvfx.com` (frontend), `https://api.guvfx.com` (backend API), `https://guac.guvfx.com/guacamole/` (Guacamole UI).
- Last green verification: `make check` on `main` (local; blocked by Postgres + Guacamole mouse flake).
- Open PRs: 0

## Repo layout (confirm paths)
- Backend: Django — `backend/`
- Frontend: Next.js — `frontend/`
- Docs: `docs/`

## Last known green checks
- Backend: 2025-12-15 — GitHub Actions CI ✅ (Django tests) + `make check` local ✅
- Frontend: 2025-12-15 — GitHub Actions CI ✅ (lint + build) + `make check` local ✅

## Active blockers
- `make check` cannot finish because Django/postgres cannot open `127.0.0.1:5432` (Operation not permitted); tests need a reachable PostgreSQL server.
- MT5 mouse input via Guacamole is still unreliable; clicks sometimes drop until menu hotkeys or restarts re-enable input.

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)
