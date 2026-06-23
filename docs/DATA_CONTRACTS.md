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

- Application API endpoints are under the `/api/` prefix (DRF ViewSets +
  routers). The Django **admin** is at `/admin/` and the health check is at
  `/health/` — these are explicit **exceptions** to the `/api/` prefix, routed in
  `backend/guvfx_backend/urls.py`.
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

- The backend communicates with MT5 through **ephemeral JSON files** on shared
  container/host mounts (`backend/mt5/views.py`, `mt5_worker/`).
- **Fixed current mount paths** — `backend/mt5/views.py` defines these as fixed
  `Path(...)` module constants (not configuration, not personal paths, and **not
  currently configurable in that module**):
  - `/app/.guvfx_handoff_validate` (`HANDOFF_VALIDATE`);
  - `/app/.guvfx_handoff` (`HANDOFF`);
  - `/srv/guvfx/mt5_pool` (`POOL_ROOT`);
  - `/app/.guvfx_pool` (`HANDOFF_POOL`).
- Launch handoff fields (names only): a credential file carrying `login`,
  `password`, `server`, and a request file carrying a timestamp and optional
  user identifier. Credential contents are secrets and are never recorded in docs
  or evidence (`.claude/rules/security.md`).
- **Deletion (as evidenced):** the MT5 validate worker
  (`mt5_worker/mt5_validate_worker.py`) deletes `validate_request.json` and
  `validate_cred.json` after processing (EPHEMERAL cleanup). Other ephemeral
  launch/validate files are marked EPHEMERAL by the contract/comments in
  `backend/mt5/views.py`, with the consumer expected to delete them; that
  consumer-side deletion is not independently verified in this document.

> **Proposed:** make the MT5 handoff/pool path locations
> **configuration-driven** (e.g. settings/environment), rather than fixed module
> constants, where future portability or multi-host deployment requires it. Not
> implemented today.

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

### Market-data schema foundation — Current

> **Current (schema + harness only).** Established by **GFX-PKT-005B** and
> **ADR 0010** (`docs/ADRs/0010-market-data-research-foundation.md`). Versioned
> JSON Schemas and a synthetic round-trip harness now exist in the repository.
> **No provider, real dataset, or ingestion pipeline exists** — only synthetic
> data marked `synthetic_test_only`.

- **Versioned contract schemas** (`research/contracts/`): `market_observation_v1`
  (quote/bar observations with source and time lineage), `broker_cost_v1`
  (point-in-time broker cost/contract specification), `dataset_manifest_v1`
  (reproducible dataset manifest). Each is a JSON Schema document with a stable
  `$id`, `version: "1.0"`, and `additionalProperties: false`.
- **Synthetic round-trip harness** (`tools/research_smoke.py`): proves a
  deterministic quote/bar → DuckDB → Parquet → DuckDB loop using only the standard
  library and DuckDB (no pandas/PyArrow/Polars). The round trip preserves the
  **full** versioned observation contract — every required common field, all five
  point-in-time timestamps, the raw-lineage fields, `quality_flags`, and the
  populated quote/bar variant fields — and re-validates and field-compares each
  reconstructed record against its source. Timestamp ordering is validated as a
  **semantic UTC datetime instant comparison** (canonical `Z` strings parsed into
  timezone-aware UTC datetimes, rejecting impossible calendar/time values and
  ordering fractional seconds correctly) while preserving the original strings;
  **nullable/omitted optional fields** (source/received times, quote sizes, bar
  volume/unit) are proven to round-trip as null. It emits **separate** dataset
  manifests (quotes `interval: event`, bars `interval: M1`), each carrying a
  required `record_type` and referencing only its own raw objects, checksum and row
  counts. It writes to a temporary directory and deletes all artefacts on exit;
  nothing persistent is produced.
- **Isolated research runtime**: a local `.venv-research` (Python 3.14) with
  exactly `duckdb==1.5.4` pinned in `requirements-research.txt`; exercised by
  `make research-check` and the `research-foundation` CI job. See
  `research/README.md`.
- **Future real-data root** is the `GUVFX_DATA_ROOT` environment variable with **no
  repository-path default**; this foundation adds **no** real data, NAS path,
  broker identity, account, or ingestion code.

The shape below remains **Candidate** for source-specific EURUSD ingestion, and
the broader point-in-time platform remains **Proposed**; this section does not
promote either.

### Synthetic acquisition foundation — Current (GFX-PKT-006C)

> **Current (synthetic-only).** Established by **GFX-PKT-006C**. A fail-closed,
> synthetic-only client/storage/orchestration foundation exists in the canonical
> repository under `research/market_data/`. It performs **no** live MT5, broker,
> NAS or real-data action.

Implemented and exercised by `tools/market_data_synthetic_smoke.py` +
`tests/test_market_data_foundation.py`:

- **Four versioned contracts** (`research/contracts/`): `agent_history_export_request_v1`,
  `agent_history_export_response_v1`, `raw_market_data_manifest_v1`,
  `broker_timezone_evidence_v1` (draft-07, `additionalProperties: false`).
- **Deterministic request fingerprints** (SHA-256), **monthly half-open chunk
  planning**, a **transport-injected** read-only client (network-inert by default;
  a network guard proves zero egress), **immutable atomic raw landing** with
  SHA-256, **idempotent reruns**, and **conflict/malformed/credential quarantine**
  that never mutates accepted raw.
- A **timezone verification gate**: normalisation requires a `VERIFIED`
  `broker_timezone_evidence_v1` assessment; there is **no default offset**.
- **Synthetic M1 bid-OHLC normalisation** into the existing `market_observation_v1`
  bar variant (bid OHLC only — no ask/spread/tick), proven through a temporary
  Parquet/DuckDB round trip and a `dataset_manifest_v1`.
- **`GUVFX_DATA_ROOT`** is now wired into backend settings with **no default**;
  real operation fails closed when it is unset/blank or resolves inside the repo.

> **GFX-PKT-006C-R1 update:** the in-repo transport is now an actual gated
> standard-library HTTP client (inert unless explicitly enabled), response
> decoding is strict (no NaN/Infinity; finite prices; schema type/length
> bounds enforced), and raw landing is exactly idempotent with deterministic
> quarantine. Still synthetic-only; the agent endpoint and real acquisition
> remain Proposed/Partial.

**Proposed / Partial (not implemented here):** the Windows Agent
`POST /mt5/history/rates/export` endpoint (agent-host code is **not** in this
repository), real acquisition, the `GuvFXData` NAS share/mount, broker
timezone/server/legal identity, and broker-cost/specification capture all remain
**Proposed/Partial**. No real EURUSD data exists.

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
- **Configurable paths (Proposed target vs. Current implementation).** The rule's
  intent is that data locations be configuration, not personal/home paths. This is
  a **Proposed** target for the MT5 handoff/pool paths, which are **currently**
  fixed `Path(...)` module constants in `backend/mt5/views.py` (see the MT5
  handoff section above) — fixed container/host mounts, not personal paths, but
  not yet configuration-driven.
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
