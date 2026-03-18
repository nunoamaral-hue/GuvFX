# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Full check (backend tests + frontend lint + frontend build)
make check

# Backend only
cd backend && .venv/bin/python manage.py test

# Frontend only
cd frontend && npm run lint
cd frontend && npm run build

# Run a single Django test module/class/method
cd backend && .venv/bin/python manage.py test trading.tests.TestTradingAccount.test_create

# Dev servers
cd backend && .venv/bin/python manage.py runserver   # :8000
cd frontend && npm run dev                            # :3000
```

Backend tests require a local PostgreSQL instance. The Makefile uses `backend/.venv/bin/python` so manual venv activation is not needed.

## Commit Convention

Conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:` (refactor only with explicit ticket). Branch names: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`.

## Working Agreement (from AGENTS.md)

- **Small diffs** -- minimal, targeted patches. No whole-file rewrites.
- **No silent deletions** -- call out any removed code in plan and summary.
- **No drive-by refactors** -- don't clean up unrelated code, formatting, imports, or naming.
- **Preserve behavior** unless explicitly asked to change it.
- **Update handoff docs** on every meaningful change: `docs/STATUS.md`, `docs/HANDOFF.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md`.
- **Start-of-task reading order**: `docs/STATUS.md` -> `docs/HANDOFF.md` -> `docs/NEXT.md` -> `docs/RUNBOOK.md` -> `docs/KNOWN_ISSUES.md`.
- **End of task**: run `make check`, summarize changes (files + reason), update handoff docs.

## Architecture

**Full-stack trading strategy platform** for designing, running, and monitoring algorithmic strategies with backtesting and live MT5 broker connectivity.

### Backend (`backend/`) -- Django 5.1 + DRF 3.15, Python 3.11+

Django apps (all under `backend/`):

| App | Purpose |
|-----|---------|
| users | Custom User model (email-based login), JWT auth via simplejwt |
| core | Health checks, shared utilities |
| trading | BrokerServer, TradingAccount, trade records |
| strategies | Strategy definitions, StrategyAssignment, marketplace |
| backtests | BacktestConfig, BacktestRun, results, metrics |
| analytics | Performance metrics, dashboards, trade history |
| ai_helper | AI-assisted strategy suggestions |
| execution | ExecutionJob orchestration (async trade execution) |
| hosting | Infrastructure management |
| mt5 | Mt5Credential, Mt5Instance, Guacamole integration |

**Auth**: JWT via `djangorestframework-simplejwt` with custom `CookieJWTAuthentication` (HttpOnly cookies). CSRF endpoint at `/api/auth/cookie/csrf/`, refresh at `/api/auth/cookie/refresh/`.

**API pattern**: DRF ViewSets + DefaultRouter, serializer-based validation, all under `/api/` prefix. REST_FRAMEWORK default permission is `IsAuthenticated`.

### Frontend (`frontend/`) -- Next.js 16, React 19, TypeScript 5, Tailwind 4

Uses App Router (`src/app/`). Key pages: dashboard, accounts, strategies, backtests, analytics, trading, admin, login, profile, charts.

`src/lib/api.ts` contains `apiFetch<T>()` -- handles CSRF injection, auto-refresh on 401, DRF error parsing, cookie-based auth with `credentials: "include"`.

Path alias: `@/*` maps to `./src/*`.

### MT5 Integration

- `mt5_worker/` -- Python worker for trade ingestion and credential validation
- Windows Agent at `http://10.50.0.2:8787` handles backtest jobs
- Guacamole provides browser-based RDP/VNC to MT5 desktop (Wine on Linux)
- Shared handoff mount at `/srv/guvfx/mt5_handoff` (backend at `/app/.guvfx_handoff`, MT5 at `/home/mt5free/.guvfx`)

### Production

- **Traefik** reverse proxy with Let's Encrypt TLS
- Domains: `guvfx.com` (frontend), `api.guvfx.com` (backend), `guac.guvfx.com/guacamole/` (MT5 remote desktop)
- VPS stacks: `/home/ubuntu/guvfx-prod/` (Traefik + app + DB) and `/home/ubuntu/guacamole-stack/` (Guacamole + MT5)
- Database: PostgreSQL 16

## Key Environment Variables (backend)

`DJANGO_SECRET_KEY` (required), `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `JWT_SECRET_KEY`, `GUAC_BASE_URL`, `GUVFX_WINDOWS_AGENT_BASE_URL`

## Formatting

- Python: 4-space indent
- JS/TS/JSON: 2-space indent
- Line endings: LF (enforced via `.gitattributes`)
