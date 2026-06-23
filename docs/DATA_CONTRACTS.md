# Data Contracts

> This document separates **current** data contracts (evidenced by code or ADRs)
> from **proposed** future requirements. It is a controlled document: every
> *current* claim cites a repository path or ADR. Nothing here asserts a
> capability that is not in the repository.

## Contract status model

| Status | Meaning |
| --- | --- |
| **Current** | Implemented and evidenced by code or an accepted ADR in this repo. |
| **Candidate** | A concrete shape proposed for near-term adoption, not yet implemented. |
| **Proposed** | A desired future requirement, not designed in detail or built. |
| **Unknown** | Referenced but not confirmed from code; needs investigation. |
| **Deprecated** | Previously used, retained for reference, no longer authoritative. |

---

## Current repository contracts

Each item is **Current** and cites its source.

### Identity fields and primary keys — Current

- `users.User`: Django default integer PK; **email is the unique login
  identifier** (`backend/users/models.py`).
- `trading.BrokerServer`: **UUID** primary key (`backend/trading/models.py`).
- `trading.TradingAccount`: integer PK; uniqueness on
  `(user, broker, account_number)` (`backend/trading/models.py`).
- `trading.Trade`: integer PK with **natural key `ticket`** (the MT5 ticket);
  uniqueness on `(account, ticket)` (`backend/trading/models.py`).
- `strategies.Strategy`: integer PK; `magic_number` unique per owner when set
  (`backend/strategies/models.py`).
- `backtests.*`, `execution.ExecutionJob`: Django default integer PKs
  (`backend/backtests/models.py`, `backend/execution/models.py`).

### UTC / timezone conventions — Current

- `USE_TZ = True`, `TIME_ZONE = 'UTC'` in
  `backend/guvfx_backend/settings.py`. Datetimes are stored timezone-aware in
  UTC; conversion to local time is a presentation concern.

### Backend API / auth boundary — Current

- All app endpoints are under the `/api/` prefix (DRF ViewSets + routers).
- Auth: `users.auth_cookie.CookieJWTAuthentication` (HttpOnly cookie JWT) with a
  SimpleJWT fallback; DRF default permission `IsAuthenticated`
  (`backend/guvfx_backend/settings.py`).
- CSRF and refresh endpoints exist under `/api/auth/`
  (`backend/users/auth_cookie_views.py`, `backend/users/urls.py`). The frontend
  contract for CSRF/refresh/credentials is in `frontend/src/lib/api.ts`.

### Strategy / backtest / trade / execution object boundaries — Current

- `strategies.Strategy` carries declarative JSON config blocks (entry/sl/tp
  rules, filters, risk limits, etc.) plus owner and identity fields
  (`backend/strategies/models.py`).
- `backtests.BacktestRun` holds `symbol`, `timeframe`, `date_from`/`date_to`,
  `initial_balance`, and JSON `metrics` + `equity_curve` results, with a
  `PENDING → RUNNING → COMPLETED|FAILED` lifecycle (`backend/backtests/models.py`).
- `trading.Trade` records realised trade facts: `ticket`, `symbol`, `side`,
  `volume`, open/close time and price, `profit`, `commission`, `swap`,
  `magic_number`, `comment` (`backend/trading/models.py`).
- `execution.ExecutionJob`: `job_type` ∈ {`TEST_CONNECTION`, `OPEN_TRADE`,
  `CLOSE_TRADE`, `SYNC_POSITIONS`}, status `PENDING → RUNNING → SUCCESS|FAILED`,
  JSON `payload`/`result`, FK to account/strategy/assignment
  (`backend/execution/models.py`, `backend/execution/services.py`).

### MT5 handoff / config conventions — Current

- The backend communicates with MT5 through **ephemeral JSON files** on a shared
  mount, written by the backend and consumed/deleted by the MT5-side worker
  (`backend/mt5/views.py`, `mt5_worker/`).
- Launch handoff fields (names only): a credential file carrying `login`,
  `password`, `server`, and a request file carrying a timestamp and optional
  user identifier. The credential file is **ephemeral and must be deleted after
  use**; its contents are secrets and are never recorded in docs or evidence
  (`.claude/rules/security.md`).
- Handoff/pool locations are **configuration**, not hard-coded personal paths;
  see `docs/RUNBOOK.md` for the operational mount description.

### GuvFX intelligence objects & WIMS consumption boundary — Current

- Intelligence envelope header (`backend/intelligence/envelope.py`):
  `intelligence_id`, `intelligence_type` (`SIGNAL` | `TRADE_RESULT`),
  `version` (`"1.0"`), `source`, `timestamp`, `confidence`, `summary`,
  `structured_payload`. The envelope is **transient** — never persisted.
- WIMS `ConsumptionContract` (`backend/wims/models.py`): `source_type`
  (`WAYOND` | `MANUAL` | `TRADE_RESULT`), descriptive fields (`symbol`,
  `direction`, `entry_price`, `stop_loss`, `take_profit`, `confidence`,
  `commentary`, `tags`, `raw_signal`) plus WP-3 trade-result fields
  (`exit_price`, `result_type`, `profit_loss`, `pips`, `close_time`); status
  `RECEIVED → PROCESSED → ARCHIVED`. It is **not** a Signal/Trade/Position/
  Execution object (ADR-009).

---

## Candidate market-data contract — Candidate

> **Candidate, not implemented.** No market-data ingestion store of this shape
> exists in the repository today. This defines the *minimum* fields a future
> contract should carry; it makes no claim of current implementation.

- **Identity:** instrument/symbol, source (provider), broker, account (where
  account-scoped), and feed/version identifier.
- **Timestamps (keep distinct, do not collapse):**
  - `observation_time` — the moment the datum refers to;
  - `source_time` — the timestamp the source assigns;
  - `received_time` — when GuvFX received it;
  - `ingestion_time` — when it was written to store;
  - `availability_time` — when it became usable for research/decisions.
- **Quote / bar fields with units & frequency:** `bid`, `ask` (and size where
  available); OHLC bar fields with explicit `frequency`/timeframe and price
  units; volume with its unit.
- **Quality & lineage:** quality flags (e.g. suspect/quarantined), and a
  reference to the immutable **raw object** the record was derived from.
- **Versioned broker costs/specifications:** spread/commission/swap model,
  contract size, tick value/size — each **versioned** with effective dates so a
  point-in-time lookup is possible.

---

## Point-in-time requirements — Proposed

> **Proposed.** Requirements for future point-in-time correctness; not built.

- Economic releases, **revisions**, and **consensus** must each retain the time
  they became known, so research sees only knowable-at-the-moment values.
- Broker specifications and costs must be queryable **as of** a historical date.
- **Feature availability** must be modelled: a derived feature carries the time
  it could first have been computed, preventing look-ahead.
- No survivorship leakage; chronological, out-of-sample validation only
  (`.claude/rules/research.md`).

---

## Storage and evidence rules — Current (governance) / Proposed (data store)

These follow `.claude/rules/data.md` and `.claude/rules/evidence.md`:

- **Immutable raw.** Captured raw data is written once, never edited in place;
  corrections are new records (Proposed for a future market-data store).
- **Rebuildable derivatives.** Any derived/aggregated dataset must be
  reproducible from raw inputs plus recorded config; treat derived data as cache.
- **Quarantine, don't destroy.** Suspect data is quarantined with a reason, not
  silently deleted.
- **Configurable paths.** Data locations are configuration, not hard-coded or
  personal/home paths.
- **No large/raw data in Git.** Git holds code, small fixtures, and concise
  evidence; bulk/binary data lives outside the repo.
- **Versioned manifests + provenance.** Evidence is captured in versioned,
  machine-readable manifests (`evidence/schema/evidence-manifest.schema.json`);
  data carries licensing/provenance.

---

## Open questions — Unknown

Unresolved decisions, recorded rather than invented:

- Market-data **provider(s)** and licensing terms — Unknown.
- Which **broker(s)** and **account** tiers (research / paper / live) the
  contract must span — Unknown.
- Required **granularity** (tick vs. bar) and retention windows — Unknown.
- **Storage backend** for raw/point-in-time data (the repo uses PostgreSQL for
  application data; a raw market-data store is undecided) — Unknown.
- Versioning cadence for broker cost/spec changes — Unknown.

> Every **Current** claim above cites a repository path or ADR. **Candidate**,
> **Proposed**, and **Unknown** items are explicitly not implemented and must not
> be read as commitments.
