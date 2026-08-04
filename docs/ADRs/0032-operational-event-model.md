# ADR-0032 — Operational Event Model (WP5.1)

- **Status:** Accepted (engineering), ships DARK
- **Date:** 2026-08-04
- **Programme:** Broker Connectivity Capability – Trusted-Beta Integration (WP5 — Operational Readiness)
- **Builds on:** ADR-0028 (WP1A account lifecycle + `BrokerAccountValidationAttempt`), ADR-0030 (WP3
  continuous broker health), ADR-0029 (WP1B/WP2 execution gate, pause, resume)
- **Decision class:** Amber — it stands up a new Django app (touches `INSTALLED_APPS`, root `urls.py`,
  a new migration lineage). Additive, reversible, DARK by default. No functionality, deployment, or
  runtime/validation/execution behaviour change.

## Context
Trusted Beta needs an operational layer — dashboards, support tooling, monitoring — that can answer, per
customer account, "what has been happening operationally, and what is the current posture?". Today that
information is scattered: the immutable `core.audit` security ledger, `BrokerAccountValidationAttempt`
history, `BrokerAccountHealth` state, `BrokerRuntimePause` records, and `reliability.AlertEvent`. None
of these is a single, query-optimised, **owner-scoped**, **non-secret** operational timeline. WP5.1
builds that foundation once — the reusable layer later packets consume — **without** building any UI,
scheduler, notification, or background job, and without wiring or changing any existing subsystem.

Key constraint (`.claude/rules/architecture.md`, security, data): do **not** duplicate the immutable
audit system, keep concerns distinct, and add no secrets to a new store.

## Decision

### 1. A new app `operational_events`, not an extension of `reliability`
A genuinely new authoritative model + recording/query/summary layer maps to the repo's "new bounded
concern → new app" precedent (`signal_intake`, `intelligence`, `terminal_provisioning`, `reliability`).
Folding it into `reliability` would overload an app whose charter is "RX-2 detection/visibility/
alerting" and whose only ops endpoint (`/api/reliability/operations-summary/`) is an **admin, estate-
wide, real-time aggregator** — semantically the opposite of WP5.1's **owner-scoped, per-account,
persisted event timeline**. The app is named `operational_events` (not `operations`) precisely to avoid
being confused with that existing admin `operations-summary` surface. Cost: four additive registration
points (`INSTALLED_APPS`, root `urls.py` include, an `AppConfig`, a fresh migration lineage).

### 2. `OperationalEvent` is a derived, rebuildable projection — NOT a second audit log
`core.audit.AuditEvent` remains the immutable, append-only security/compliance ledger, and the
WP1A/WP3/WP2 models remain the authoritative operational **state**. Every operational signal WP5.1 cares
about is *already* written to audit today as a free-form `event_type`. `OperationalEvent` is therefore
an **additive, query-optimised read model** (a cache in the `data.md` sense — rebuildable from the
authoritative sources), never a re-emission of the same semantics into the same immutable log. Because
it is a projection and not evidence, it is *mutable* (`resolved` may flip) — the one deliberate contrast
with audit.

Why audit cannot serve the need directly (verified): audit has no `category` column (would be derived
from `event_type` prefixes); `reason_code`/`correlation_id`/`actor` live only inside its JSON metadata
(and broker events do not even populate `correlation_id`); and `user` is `NULL` for every system-emitted
broker event, so it cannot be owner-scoped or timeline-ordered per account efficiently. `OperationalEvent`
promotes exactly those to first-class, indexed columns with a real `account` FK.

### 3. Model — `operational_events.OperationalEvent`
Non-secret columns only: `account` (FK, nullable for estate-wide SYSTEM events), `runtime_uuid` (a soft
string reference to `AccountRuntime.runtime_uuid` — not a FK, to avoid coupling to the provisioning
lifecycle), `category`, `event_type` (free-form, like audit — a new type never needs a migration),
`severity`, `status`, `reason_code`, `summary`, `source`, `correlation_id`, `state_version`, `actor`,
`customer_visible`, `resolved`/`resolved_at`, `dedup_key`, `metadata` (JSON), `created_at`. **No
credentials, ciphertext, host paths, or operator diagnostics** — enforced structurally (allow-listed
fields) and defensively (a metadata key-denylist sanitiser mirroring `core.audit._sanitize_metadata`).
Query-optimised via composite indexes on `(account, -created_at)`,
`(account, customer_visible, -created_at)` (the hottest path — every non-staff timeline read),
`(account, category, -created_at)`, `(account, severity, -created_at)`, `(account, resolved)`,
`(correlation_id)`. A **partial unique constraint** on a non-empty `dedup_key` enforces idempotency
(event-duplication protection) at the DB layer.

### 4. Categories & severities
- **Categories** (fixed, future-safe): `VALIDATION, HEALTH, EXECUTION, RUNTIME, CREDENTIAL,
  CONNECTIVITY, SYSTEM`. Live-source mapping: VALIDATION←validation_status/attempts;
  HEALTH←`broker_health.get_contract`; EXECUTION←execution-gate/`SR_*`; RUNTIME←`runtime_pause`;
  CREDENTIAL←credential lifecycle; CONNECTIVITY←`disconnected_at`/tombstone; SYSTEM←flags/no-account.
- **Severities:** `INFO, WARNING, ERROR, CRITICAL` (the WP5.1 spec). **Severity mapping (documented
  reconciliation):** upstream vocabularies differ — `core.audit` uses `DEBUG/INFO/WARN/ERROR/CRITICAL`
  and `reliability.AlertEvent` uses `INFO/WARN/CRITICAL`. `constants.normalize_severity` maps
  `DEBUG→INFO`, `WARN→WARNING`, `FATAL→CRITICAL`, passes `INFO/ERROR/CRITICAL` through, and defaults
  **unknown → INFO**. This is a deliberate, single-place reconciliation (not a silent alias).

### 5. Recording, query, summary, DTO, API
- **Recorder** (`events.record_event`): the ONE write entry point. DARK by default (no-op → `None`
  unless `operations_events_enabled()`), **fail-open** (never raises into a caller, like
  `core.audit.log_event`), **idempotent** on a non-empty `dedup_key` (`get_or_create` + the partial
  unique constraint — safe under concurrency), **secret-safe** (metadata sanitised). `mark_resolved`
  closes matching open events; it refuses a scope-less (global) resolve.
- **Query** (`query.OperationalQueryService`): `timeline / recent / latest_in_category / latest_of_type
  / open_events / customer_visible / operator_visible / summary`. All ORM access lives here; every
  method returns DTOs, never model instances. Pagination is explicit (`limit` clamped to a hard max,
  `offset`) because the project sets no DRF pagination default. Ownership is enforced by the caller.
- **Summary** (`summary.build_operational_summary`): deterministic, hybrid, **read-only**. Current
  *state* (validation / health / runtime pause / credential / disconnect) is read LIVE from the
  authoritative WP1A/WP3/WP2 sources (flag-gated ones return "not observed" when DARK); *history*
  (latest error/warning/validation, event counts) aggregates the OperationalEvent timeline (DB-side).
  Each cross-source live read is defensive (fail-open) so a failing dependency cannot raise into the
  summary. **Visibility scoping:** it accepts `customer_only` and, when set, scopes *every* event
  aggregate to `customer_visible=True` — so a non-staff owner's summary never discloses operator-only
  event content (the same boundary the timeline enforces). The API passes `customer_only=not is_staff`.
- **DTOs** (`dto`): `@dataclass(frozen=True)` value objects with `as_dict()` (the house idiom — cf.
  `GateDecision`/`ResumeResult`); `as_dict()` copies nested dicts so a caller cannot mutate DTO state.
- **API** (`views.OperationalAccountEventsView`, `GET /api/operations/account-events/?account_id=`):
  read-only, `IsAuthenticated`, **owner-scoped** (a non-staff user resolves only their own account →
  404 otherwise, IDOR-safe; staff may read any account), returns `{summary, timeline}`. Non-staff
  receive customer-visible events only; staff receive operator-visible (all). **404 while DARK** (the
  endpoint does not exist until the flag is armed). No privilege expansion beyond the existing
  `is_staff` bypass convention.

### 6. DARK flag
`OPERATIONS_EVENTS_ENABLED` (default OFF), read LIVE via `constants.operations_events_enabled()` with
the tolerant `1/true/yes/on` parser shared by `broker_health_enabled` / `execution_gate_enabled`.

## Scope boundary — what WP5.1 deliberately does NOT do
- **No source wiring.** This packet builds the recorder as the single sanctioned entry point; it does
  **not** modify `broker_health` / `runtime_pause` / `broker_gate` / `broker_connectivity` to emit into
  it. Wiring those call sites is a separate, later increment — kept out here to honour the packet's hard
  boundary (no runtime/validation/execution behaviour change). Consequence: in production (DARK) the
  timeline is empty until wiring; the summary is still fully truthful because its *state* fields read
  the authoritative live sources.
- No UI, scheduler, notifications, dashboards, or background jobs (later packets).

## Consequences
- **Positive:** one authoritative, query-optimised, owner-scoped, non-secret operational read model the
  ops/support/monitoring layers can build on; clean separation from audit and from reliability's admin
  summary; DARK and fully reversible; the summary is useful the moment the flag is armed even before
  source wiring.
- **Negative / trade-offs:** a new app (four registration points); the event timeline is inert until a
  future wiring increment; `OperationalEvent` and `core.audit` will, once wired, both record the same
  operational moments — acceptable because they serve different purposes (immutable legal record vs
  mutable query model) and the recorder never writes into audit.

## Future work (separate, gated increments)
1. Wire the existing broker sources to call `record_event` at their emit points (mirroring their audit
   emissions, secret-free), behind this flag.
2. Ops/support tooling + a monitoring surface consuming the query/summary API (WP5.x / WP6).
3. Optional retention/compaction for the projection (it is rebuildable, so it may be pruned).
