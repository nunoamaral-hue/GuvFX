# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

-## TL;DR
- Current focus: VPS production rollout (Traefik + Let’s Encrypt) + MT5 handoff automation and the Guacamole mouse reliability investigation.
- Current branch: `fix/broker-autocomplete-edgecases`
- Next milestone: Resolve the navigation/focus follow-up for broker autocomplete (P1 #4) while keeping the VPS/MT5 flow steady.
- Production URLs: `https://guvfx.com` (frontend), `https://api.guvfx.com` (backend), `https://guac.guvfx.com/guacamole/` (Guacamole).
- Last verification: `curl -Ik https://guvfx.com`, `curl -Ik https://api.guvfx.com`, `curl -Ik https://guac.guvfx.com/guacamole/`, and `docker logs --tail 200 traefik | egrep -i "acme|certificate|error"` (post-`make check` still blocked on PostgreSQL connectivity).
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
- `make check` cannot finish because Django/postgres cannot open `127.0.0.1:5432` (Operation not permitted); tests will need a reachable PostgreSQL server.
- MT5 mouse input via Guacamole is still unreliable (mouse clicks intermittently dead even though keyboard navigation works); needs deeper investigation before we rely on automation clicks.
- `make check` cannot finish because Django/postgres cannot open `127.0.0.1:5432` (Operation not permitted); tests need a reachable PostgreSQL server.
- MT5 mouse input via Guacamole is still unreliable; clicks sometimes drop until menu hotkeys or restarts re-enable input.

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)

# STATUS — MT5 Free Desktop (XRDP + VNC)

**Overall status:** ✅ STABLE / OPERATIONAL  
**Last verified:** 2025-12-18 (UTC)

### What is running
- XRDP listening on `:3389`
- xrdp-sesman listening on `:3350`
- Xvfb + Openbox running on `DISPLAY=:99` (VNC fallback)
- XRDP Xorg session allocated dynamically (e.g. `DISPLAY=:10`)
- MetaTrader 5 (Wine) auto-starts inside XRDP session

### Verified checks
- `terminal64.exe` running under user `mt5free`
- `wmctrl` lists:
  - `MetaTrader 5 - Netting`
  - `Login` window
- Guacamole RDP connection shows MT5 UI

### Persistence
- Wine prefix persisted via Docker volume: `/home/mt5free/.wine`
- Autostart script persisted via bind mount: `/home/mt5free/bin/autostart-rdp.sh`

### Notable guarantees
- No manual MT5 launch required
- No XRDP password re-entry after container restart
- Safe to rebuild container without losing MT5 state

## Incident Log

### 2026-03-17 — Traefik Stale Backend Routing (API Auth Failure)

**Classification:** Operational issue (routing layer) — NOT an architectural failure.

**Issue:** Intermittent API authentication failure due to inconsistent backend routing.

**Symptoms:**
- Browser login failure ("Failed to fetch")
- Intermittent 502 Bad Gateway responses
- CORS preflight failures
- Inconsistent API responses across requests

**Root Cause:** Traefik routing table contained multiple backend targets — one valid container IP and one stale (dead) container IP from a previous deployment. Requests routed to the stale IP returned 502 errors, causing an auth failure cascade.

**Affected Component:** `api.guvfx.com` → `guvfx-backend` service (Traefik routing layer only).

**Resolution:**
```bash
docker compose down --remove-orphans
docker compose up -d
```
This removed stale containers, rebuilt the Docker network, refreshed Traefik service discovery, and eliminated invalid backend targets.

**Validation:**
- CSRF endpoint: 10/10 success
- OPTIONS login preflight: 10/10 success
- Browser login: confirmed working
- API responses: stable

**Architecture Impact:** NONE
**Infrastructure Impact:** NONE
**Status:** RESOLVED

## Documentation Governance Mapping

This repository uses the following canonical mapping:

| Governance Reference | Canonical File |
|---|---|
| `GUVFX_IMPLEMENTATION_LOG.md` | `docs/STATUS.md` |
| `GUVFX_PLATFORM_STATE.md` | `docs/STATUS.md` (shared responsibility) |
| `GUVFX_TERMINAL_FARM_RUNBOOK.md` | `docs/RUNBOOK.md` |
| Incident / edge-case tracking | `docs/KNOWN_ISSUES.md` |

All references to `GUVFX_*` documents map to these files. The `docs/` directory is the single source of truth. Do not create duplicate documents with alternate naming or introduce parallel canonical structures.
