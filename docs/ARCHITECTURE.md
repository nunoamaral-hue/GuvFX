# Architecture

> Current-state architecture of the implemented GuvFX platform. Each item carries
> an explicit status label: **Implemented**, **Partial**, **Proposed**, or
> **Unknown**. "Implemented" means present and exercised in the repository;
> "Partial" means present but incomplete or not fully wired; "Proposed" means
> described/desired but not built here; "Unknown" means not yet confirmed from
> code. Proposed future concepts are isolated in *Target evolution* at the end.

## Monorepo topology — Implemented

A single repository holding the full stack:

- `backend/` — Django 5.1 + Django REST Framework, Python 3.11+. Project package
  `guvfx_backend/`; local apps listed below.
- `frontend/` — Next.js 16 / React 19 / TypeScript, App Router under `src/app/`.
  `src/lib/api.ts` centralises CSRF injection, 401 auto-refresh, and cookie auth.
- **PostgreSQL** — primary datastore. Repository CI tests against **PostgreSQL
  16**; the **production** database version is **Unknown** in this repository
  unless a concrete deployment source is cited.
- `mt5_worker/` — Python worker(s) for MT5 trade ingestion and credential
  validation; the `mt5` app and `execution` app coordinate handoff.
- `docs/`, `evidence/`, `packets/`, `.claude/rules/`, `scripts/`, `tests/` —
  operational documentation and the governance/evidence layer (see below).

## Backend app boundaries — Implemented

From `backend/guvfx_backend/settings.py` `INSTALLED_APPS` (local apps) and the
repository structure:

| App | Responsibility |
| --- | --- |
| `users` | Custom email-login User, JWT cookie auth |
| `core` | Health checks, shared utilities |
| `trading` | `BrokerServer`, `TradingAccount`, `Trade` records |
| `strategies` | Strategy definitions, `StrategyAssignment`, change log |
| `backtests` | `BacktestConfig`, `BacktestRun`, Windows backtest jobs |
| `analytics` | Performance metrics, trade history, dashboards |
| `ai_helper` | AI-assisted strategy suggestions (research/assist only) |
| `execution` | `ExecutionJob` orchestration (async trade actions) |
| `hosting` | Infrastructure/instance management |
| `mt5` | `Mt5Credential`, `Mt5Instance`, Guacamole/handoff integration |
| `wims` | Educational content workflow — **consumes** intelligence |
| `intelligence` | GuvFX intelligence **producer** (Phase 7A/7B) |

Separation of responsibilities (data, research/backtesting, execution, AI
assistance, intelligence) is kept in distinct apps per `.claude/rules/architecture.md`.

## GuvFX / WIMS boundary — Implemented

GuvFX **produces** trading intelligence; WIMS **consumes** it through a
documented one-directional interface (ADR-009). The two are conceptually
distinct and must not be merged:

- `intelligence` packages authoritative inputs (a Wayond signal, or a closed
  `trading.Trade`) into a **transient, immutable envelope**
  (`backend/intelligence/envelope.py`) and delivers it
  (`backend/intelligence/delivery.py`).
- `wims` consumes via `wims.services.create_contract`, persisting a
  `ConsumptionContract` (`backend/wims/models.py`) — descriptive received
  intelligence, never a Signal/Trade/Position/Execution object.
- Dependency direction is enforced: `intelligence` → `wims`; **WIMS never imports
  `intelligence`**. `intelligence` persists no models of its own. See
  `backend/intelligence/README.md` and `backend/wims/README.md`.

## Execution & MT5 integration — Partial

- `execution.ExecutionJob` models async account actions (`TEST_CONNECTION`,
  `OPEN_TRADE`, `CLOSE_TRADE`, `SYNC_POSITIONS`) with a `PENDING → RUNNING →
  SUCCESS|FAILED` lifecycle and JSON `payload`/`result` fields.
- MT5 connectivity uses a **file-based handoff**: the backend writes ephemeral
  JSON request/credential files into a shared mount; an MT5-side worker consumes
  and deletes them. Field-level detail and paths are in `docs/DATA_CONTRACTS.md`.
- A browser-based MT5 desktop is served through Guacamole. Operational topology
  is in `docs/RUNBOOK.md`.
- Marked **Partial** because the end-to-end automated execution path is gated and
  not a fully unattended pipeline; MT5 click reliability is a known issue
  (`docs/KNOWN_ISSUES.md`).

## Production topology — Partial (documented, live state not verified by this packet)

`docs/RUNBOOK.md` documents the **intended / last-recorded** production topology:
a VPS behind **Traefik** with **Let's Encrypt** TLS, serving the frontend,
backend API, and Guacamole MT5 desktop on separate hostnames; the GuvFX app stack
and the Guacamole + MT5 stack operated independently, with a shared handoff mount
between backend and MT5 containers. Host paths, restart, and verification commands
are **not** duplicated here — see `docs/RUNBOOK.md`. No private addresses or
credentials are recorded in this document. This packet reviewed documentation and
repository configuration only: current **uptime**, **deployed image versions**,
and **configuration drift** against the runbook were **not checked**.

## Governance & evidence layer — Implemented

Added by PR #31 (merge `c17b7b8`):

- `.claude/rules/*.md` — scoped rules (architecture, data, research, security,
  evidence, handoff, notion).
- `scripts/check_no_secrets.py` + `tests/test_no_secrets.py` — standard-library
  secret scanner, wired into `make governance-check` and CI.
- `evidence/` — machine-readable evidence manifests + schema.
- `packets/ACTIVE.md` — the bounded active-work pointer; Notion is authoritative
  for full packet lifecycle.

## Current component flow (implemented)

```
Browser (Next.js, frontend/)
  → /api/* (DRF, cookie JWT auth)
    → trading / strategies / backtests / analytics / execution apps
    → PostgreSQL
  execution.ExecutionJob → file handoff → MT5 worker (mt5_worker/) → MT5 (Guacamole desktop)

Wayond signal (external) ─┐
closed trading.Trade ─────┴→ intelligence (envelope) → wims.ConsumptionContract → WIMS content workflow
```

## Flow A (shadow) — Partial

`flow-a-shadow` branch only (not on `main`): an execution-**suppressed** signal
pipeline (`backend/flow_a/`). It is research/validation in shadow mode; it does
not place, size, or approve orders, and is not promoted to paper or live. See
`docs/STATUS.md`.

## Security posture — Partial

The repository implements some controls; others are **policy** (defined in rules
but not enforced by repository code) or are **operational facts this packet did
not verify**. These are kept distinct:

- **Implemented controls (evidenced in this repository):**
  - Auth required by default (DRF `IsAuthenticated`); cookie-based JWT with CSRF
    (`backend/guvfx_backend/settings.py`, `backend/users/auth_cookie*`).
  - Per-account scoping on trading data; staff/superuser bypass is intentional.
  - Secret scanning over **Git tracked/staged text only**
    (`scripts/check_no_secrets.py`); it does **not** read Notion, runtime logs,
    untracked files, or network state, and cannot enforce them.
- **Policy controls (defined in `.claude/rules/security.md`, not enforced by the
  scanner):** no secrets in Notion, prompts, or logs; least privilege; no public
  admin exposure.
- **Unknown operational facts (not checked by this packet):** current
  runtime-log compliance (whether deployed services actually keep secrets out of
  logs) and the current network / public-exposure state of admin and management
  surfaces.

## Market-data synthetic foundation — Implemented (synthetic) / Proposed (agent host)

GFX-PKT-006C adds a canonical-repository, **synthetic-only** market-data
client/storage/orchestration foundation under `research/market_data/`, kept
strictly separate from the external agent host:

- **In-repository (Implemented, synthetic):** request/response/manifest/timezone
  contracts (`research/contracts/agent_history_export_*`, `raw_market_data_manifest_v1`,
  `broker_timezone_evidence_v1`); a transport-injected read-only client that is
  **network-inert by default**; deterministic monthly chunk planning; immutable
  atomic raw landing with SHA-256, idempotency and quarantine; a `VERIFIED`
  timezone gate; and synthetic M1 bid-OHLC normalisation into
  `market_observation_v1`. `GUVFX_DATA_ROOT` is wired with no default and fails
  closed. Gated by the `market-data-foundation` CI job.
- **External / unimplemented (Proposed):** the Windows Agent read-only export
  endpoint `POST /mt5/history/rates/export` is **not** present in this repository
  (only an HTTP client boundary is defined). Real acquisition, the `GuvFXData` NAS
  share/mount, and broker timezone/identity/cost evidence are not built. The agent
  remains a **read-only source boundary**; execution endpoints are not reused, and
  no component gains order/risk/promotion authority.

## Target evolution — Proposed

The following are **Proposed** concepts for future direction, **not implemented**
in this repository. They are recorded here only to separate aspiration from
current state; none should be read as built or promised:

- **Point-in-time market data** store with strict observed-vs-ingested time
  semantics and no look-ahead (see candidate contract in
  `docs/DATA_CONTRACTS.md`). Status: **Proposed**.
- **Research factory** — systematic hypothesis → baseline → chronological
  validation pipeline per `.claude/rules/research.md`. Status: **Proposed**.
- **Validation / promotion gate** — a governed path from backtest to paper to
  live. Status: **Proposed**.
- **Portfolio / risk** layer — cross-strategy sizing and risk limits as a
  distinct module. Status: **Proposed**.
- **Ledger** — an immutable record of intelligence, decisions, and execution
  outcomes. Status: **Proposed**.
- **Hermes control-plane** — a higher-level orchestration/control concept.
  Status: **Proposed** / **Unknown** (not defined in this repository).

> Any move from Proposed to Implemented requires an approved decision (ADR /
> Notion record), not an in-passing edit (`.claude/rules/architecture.md`).
