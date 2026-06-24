# GuvFX — Project Status

> Update this file **whenever** project state changes. This is a current-state
> snapshot; deeper operational detail lives in `docs/RUNBOOK.md` and the handoff
> docs.

## Snapshot

- Date: 2026-06-23 (UTC)
- Canonical branch: `main`
- Current governance merge: `c17b7b8` — PR #31 *Add governance convergence
  foundation* merged into `main`. This introduced the scoped Claude rules,
  authority/packet boundaries, the secret scanner + governance Make/CI gate, the
  Notion map, the evidence convention, and the active-packet pointer.
- Documented production routes: `https://guvfx.com` (frontend),
  `https://api.guvfx.com` (backend API), `https://guac.guvfx.com/guacamole/`
  (Guacamole MT5 desktop). These are the routes recorded in `docs/RUNBOOK.md`;
  route availability and live production health were **not probed** by
  GFX-PKT-004A or its R1 remediation.
- Research/data foundation: PR #32 and PR #33 are merged to `main`
  (`80ef2f8`), establishing the DuckDB research foundation and the versioned
  market-data contracts (GFX-PKT-005B / R1 / R2).
- **GFX-PKT-006C** (synthetic market-data acquisition foundation) on branch
  `chore/market-data-synthetic-foundation`, continued by **R1**, **R2** and now
  **GFX-PKT-006C-R3** (semantic time and provenance integrity: exact-instant
  timezone comparison with no fractional truncation; semantic + ordered manifest
  timestamps; manifest provenance bound to the exact stored request/response and
  derived directory; publication bound to exact raw response bytes/SHA-256/raw-id;
  historical evidence byte discipline). PR **#34 remains draft and unmerged** for PM
  review (not accepted). Synthetic-only: no real data, NAS, broker, agent or
  deployment action. CI runs a `market-data-foundation` job.

## Verified current state

Facts supported by code, Git history, or CI in this repository:

- Monorepo with a Django + DRF backend (`backend/`) and a Next.js frontend
  (`frontend/`); see `docs/ARCHITECTURE.md`.
- Backend local apps registered in `backend/guvfx_backend/settings.py`
  (`INSTALLED_APPS`): `users`, `core`, `trading`, `strategies`, `backtests`,
  `analytics`, `ai_helper`, `execution`, `hosting`, `mt5`, `wims`,
  `intelligence`.
- GuvFX/WIMS producer–consumer boundary is implemented: `intelligence` packages
  inputs into transient envelopes and delivers them; `wims` consumes via
  `ConsumptionContract`. WIMS never imports `intelligence` (ADR-009 boundary,
  documented in `backend/intelligence/README.md` and `backend/wims/README.md`).
- Auth is cookie-based JWT (`users.auth_cookie.CookieJWTAuthentication`) with
  DRF default permission `IsAuthenticated`; `USE_TZ = True`, `TIME_ZONE = 'UTC'`.
- Governance/evidence layer is present on `main` as of `c17b7b8` (PR #31):
  `.claude/rules/`, `scripts/check_no_secrets.py`, `tests/test_no_secrets.py`,
  `evidence/`, `packets/`, `make governance-check`.

## Active feature work

- **Flow A (`flow-a-shadow` branch)** — a shadow, execution-suppressed signal
  pipeline (`backend/flow_a/`: `signal_intake`, `candidate`, `evaluation`,
  `quality_gate`, `suppression`, `pipeline`, `replay/`, and the
  `run_flow_a_shadow` management command). It runs in shadow mode only and is
  **not** merged into `main` and **not** promoted to paper or live trading.
  Treat it as research/validation work bounded by its own branch and governance
  path.

## Known gaps and blockers

Current, evidenced items only:

- Local `make check` cannot complete on a machine without a `backend/.venv` and a
  reachable PostgreSQL (`127.0.0.1:5432`); backend Django tests need a running
  PostgreSQL. GitHub Actions is the approved full-integration gate.
- MT5 mouse input via Guacamole has been observed to be unreliable (clicks
  intermittently drop while keyboard navigation works); see
  `docs/KNOWN_ISSUES.md`.

## Last known green checks

Kept distinct: historical local evidence vs. current governance CI evidence.

- **Historical (2025-12-15):** Backend GitHub Actions CI (Django tests) and
  Frontend GitHub Actions CI (lint + build) reported green, with `make check`
  green locally at that time.
- **Current governance CI (2026-06-23):** GitHub Actions push run for merge
  `c17b7b8` (PR #31) — jobs `governance`, `backend`, and `frontend` all
  succeeded.

## Production operations

Production runs behind Traefik with Let's Encrypt TLS on a VPS; the
GuvFX backend/frontend/Postgres stack and the Guacamole + MT5 desktop stack are
operated separately. Do **not** duplicate the full procedure here — see
`docs/RUNBOOK.md` (sections "VPS Production (GuvFX)" and "RUNBOOK — MT5 Free
Desktop") for the authoritative restart, verification, and handoff-mount steps.
This document does not assert live-trading readiness; promotion to paper or live
follows the governance decision path, not status notes.

## Owners

- PM: Nuno Amaral
