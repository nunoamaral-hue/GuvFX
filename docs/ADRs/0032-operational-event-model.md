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
1. ~~Wire the existing broker sources to call `record_event`~~ — **done in WP5.2 (below).**
2. Ops/support tooling + a monitoring surface consuming the query/summary API (WP5.x / WP6).
3. Optional retention/compaction for the projection (it is rebuildable, so it may be pruned).

---

# WP5.2 — Operational Event Source Wiring

- **Status:** Accepted (engineering), ships DARK. **Date:** 2026-08-04. Additive; behind
  `OPERATIONS_EVENTS_ENABLED` (default OFF); no deployment; no runtime/validation/execution behaviour
  change; Customer Zero + production untouched.

## Decision
Connect the existing, authoritative broker-connectivity emit points to the WP5.1 projection through ONE
central mapping module, `operational_events/broker_projection.py`. Call sites pass authoritative FACTS
only; the module owns category / event_type / severity / customer-visibility / summary / metadata
allow-list / dedup-key / source. This is a **projection of an already-existing authoritative moment** —
it creates no new business event, moves no logic, and never becomes a prerequisite for any operation.

### Projection-only invariant (load-bearing)
`OperationalEvent` never drives a decision, permits/blocks execution, or changes validation / health /
pause / credential / lifecycle state. Existing behaviour is correct if no event is written, the table is
empty, the table is deleted and rebuilt, or the recorder fails. **No business logic reads
`OperationalEvent`** (it is a read model for humans/tooling, not a control input).

### Fail-open + transaction policy (the safety core)
Every projection is registered via **`transaction.on_commit(lambda: record_event(...))`** at the
**durable** emission point — **never an inline `record_event` inside an authoritative `atomic()`**. Reason
(verified): a raised INSERT inside a Postgres transaction aborts the whole transaction even when the
Python exception is caught, so an inline recorder call could roll back the authoritative operation.
`on_commit` defers the write past COMMIT (discarded on rollback → **no phantom event**) and runs
**immediately** when there is no active transaction (durable autocommit re-emit). Each `project_*`:
(1) early-returns when `operations_events_enabled()` is False (zero extra work when DARK); (2) wraps the
`on_commit` registration fail-open; (3) binds only pre-computed scalar facts into the closure (never
re-reads a mutable ORM instance post-commit; the `account` instance is captured for the FK only). The
recorder is independently fail-open and DARK-gated, and **never writes `core.audit`** — audit remains the
authoritative, immutable ledger; the operational event is a separate mirror of the same moment.

### Wired sources (each hooked at its durable point)
| Source | Hook site | tx context | dedup key |
|--------|-----------|-----------|-----------|
| Validation | `broker_connectivity.run_broker_validation` (before return) | autocommit | `broker_validation:attempt:{id}` |
| Health transition | `broker_health._emit_signals` (once per `changed`) | in-atomic → on_commit | `broker_health:{acct}:{state_version}` |
| Health credential-invalidation | `broker_health.invalidate_for_credential_replacement` | in-atomic → on_commit | `broker_health_invalidated:{acct}:{sv}` |
| Credential replacement | `broker_connectivity.replace_credentials` (post-commit) | after atomic | `broker_credential_replaced:{acct}:{updated_at}` |
| Disconnect | `broker_connectivity.disconnect_account` (post-commit) | after atomic | `broker_disconnect:{acct}` |
| Runtime pause | `runtime_pause._audit` (choke point) | in-atomic → on_commit | `runtime_pause:{rec}:{sv}:{kind}` |
| Controlled resume | `runtime_pause._resume_audit` (choke point) | in-atomic → on_commit | `runtime_resume:{acct}:{ver}:{kind}` |
| Execution creation refusal | `broker_gate._audit_refusal` + h1/m5 scheduler re-emit | autocommit / discarded-on-rollback | job/none (mutually exclusive) |
| Execution dispatch refusal | `broker_gate._audit_dispatch_refusal` | autocommit | `exec:dispatch:{job_id}` |
| Promotion (broker-gate) rejection | `signal_promotion` (`broker_gate_*` only) | autocommit | `exec:promotion:{plan_id}` |

Creation-refusal correctness: the in-`save()` `_audit_refusal` and the scheduler re-emit are **mutually
exclusive per logical refusal** (a scheduler wraps the create in `atomic()`, so its in-tx audit + the
in-tx projection are rolled back and only the autocommit re-emit is durable; a view/service caller is
autocommit and the in-save projection is the durable one). Result: exactly one event per logical refusal.
`EXECUTION_GATE_REFUSED` flowing through `runtime_pause._audit` is deliberately **not** projected there
(it is an execution refusal, covered by the gate/scheduler durable points — avoids a double event).

### Categories / severity / customer-visibility mapping
- **VALIDATION** (customer-visible): HEALTHY→INFO, NEEDS_ATTENTION→WARNING, UNAVAILABLE→ERROR.
- **HEALTH** (customer-visible): HEALTHY/RECOVERED→INFO, DEGRADED/STALE→WARNING, **DISCONNECTED→ERROR**
  (deliberately not inflated to CRITICAL per the severity policy, even though the internal audit severity
  is CRITICAL — the operational classification is distinct from the audit severity), TOMBSTONED→INFO. One
  event per net transition; `pause_required`/`resume_eligible` are folded into metadata, not separate events.
- **CREDENTIAL**: replacement→INFO customer-visible; the health-engine reset (`invalidated`)→WARNING
  **operator-only** (the customer sees the "replaced" event, not the internal reset).
- **CONNECTIVITY** (customer-visible): disconnect→INFO; credential-destroyed folded into metadata.
- **RUNTIME**: paused/pause_requested→WARNING customer-visible; resumed→INFO customer-visible;
  recovery-detected / idempotent / stale-version / resume-refused→**operator-only**.
- **EXECUTION** (operator-only, WARNING): creation/dispatch/promotion refusals — the customer-facing cause
  is already surfaced by VALIDATION/HEALTH/CONNECTIVITY.

### Metadata allow-lists (secret-safety)
Each projection builds its own fixed, non-secret metadata (status, reason_code, retryable, is_demo,
trigger, from/to state, state_version, pause flags, credential_destroyed, job_id, phase, plan_id). **No**
passwords, ciphertext, tokens, keyring/envelope fields, host paths, PIDs/sessions/IPC/TCP endpoints, or
raw exception strings ever enter the projection. The WP5.1 key-denylist sanitiser remains a backstop, not
the primary mechanism.

### Deduplication
Deterministic keys from durable source identifiers (validation attempt id, health state_version, pause
record id + version, resume version, execution job id, plan id, disconnect-per-account). Where no durable
id exists (view/service creation refusal, per-bar scheduler refusal) an empty key is used so distinct
API/bar attempts remain distinct rows. Retries/replays of a keyed event collapse to one row via the
partial-unique `dedup_key`.

### Out of scope (documented, not synthesized)
Refusal paths with **no existing durable audit moment to mirror** are NOT wired here (the packet wires
existing authoritative moments only): the **h4 scheduler** refusal path (no durable refusal audit — this
is the open WP1B/WP2 Workstream E "h4 parity" item), the **PLACE_TEST_ORDER** demo early-return (emits no
durable audit), and the **pause-creation-block refusal under a view** (rare; covered under schedulers via
the re-emit). Adding a durable audit + projection to those belongs to Workstream E / a follow-up, not to a
projection packet.

---

# WP5.3 — Operations & Support Surface (frontend)

**Status:** Accepted (repository engineering only; DARK / OFF by default; not deployed, not armed).
**Amends:** ADR-0032 (adds the read-only frontend consumer of the WP5.1/5.2 Operational Event API).
**Also governed by:** ADR-0031 (WP4.1 frontend parity guard) — the new routes/components/env var are
registered in the parity manifests and the guard must stay green.

## Context
WP5.1 built the operational-event model + query/summary/DTO and the merged read API
(`GET /api/operations/account-events/`); WP5.2 wired the durable event sources. There was **no operator
UI** — an operator inspecting an account's validation/health/pause/credential/connection state or its
event timeline had to read the raw API. WP5.3 adds the first internal Operations & Support surface: a
read-only viewer over that existing API. No new backend, no new endpoint, no write path.

## Decision

### 1. A build-time DARK flag, separate from the backend gate
`NEXT_PUBLIC_OPERATIONS_ENABLED` (default OFF) gates the entire surface, mirroring the WP4.2
`NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` pattern. When OFF: **no nav entry, the routes `notFound()`
before any hook/API call, no preload, and the existing UI is byte-identical**. It is *independent* of the
backend `OPERATIONS_EVENTS_ENABLED` gate; both default OFF/DARK, and arming either is a separate,
Sponsor-gated step (a rebuild for the frontend flag). Registered in `parity/env-allowlist.json`.

### 2. Two gates, backend is the authority
The surface is operator-only in the UI via `useAdminRole()` (flag OFF → `notFound`; flag ON + non-operator
→ a "Restricted" empty state that **makes no API call**; operator → content). This is *defence in depth,
not enforcement*: the WP5.1 API independently enforces owner-scoping and operator-visibility (non-staff see
only their own accounts and only `customer_visible` events; staff see all). The frontend never bypasses,
weakens, or reconstructs that scoping.

### 3. Routes (nested under the existing `operations/` dir — no architecture replacement)
`/operations/accounts` (list) and `/operations/accounts/[id]` (overview + timeline + detail). The existing
`/operations` reliability dashboard is **untouched** — WP5.3 adds sibling routes only, it does not replace
or re-point the established route. Registered in `parity/routes.json`.

### 4. Read-only; server-side vs client-side filtering
One GET via a thin `operations-api.ts` wrapper (`getAccountEvents`) — no writes, no new URLs elsewhere.
`category` is the **only** server-side filter the WP5.1 API supports and is the only one sent to the
backend (a category change also resets pagination). Severity / open-resolved / visibility / date-range /
free-text search are applied **client-side over the fetched page** by a pure `applyClientFilters` — the API
is *not* redesigned. Pagination uses the API's own `limit`/`offset` (page size 50); it peeks `PAGE+1` rows
and shows only `PAGE`, so "Next" is enabled only when a further row genuinely exists (an exactly-full final
page never lands the operator on an empty page). Client-side date-range boundaries are parsed at **local**
midnight to match the locally-rendered timestamps (`toLocaleString`).

### 5. One vocabulary→view mapping (`operations-status.ts`), no duplicated colour logic
Every severity/category/resolution/health/pause/credential/disconnect value is mapped to a `StatusView`
`{label, color}` in one module, reusing the WP4.2 5-colour `BadgeColor` palette and rendered through the
shared `StatusBadge`. Components **never render a raw backend enum**; colour lives in exactly one place. A
unit test asserts every mapper output stays on the 5-colour palette.

### 6. Secret-safety at the view boundary (belt-and-braces with WP5.1/5.2)
The event-detail view displays only the non-secret projection — summary, reason_code, source, event_type,
timestamps, resolution, and correlation id. For `metadata` it does **not** dump the dict: it renders only a
strict, fail-closed **frontend allow-list** of human-facing scalar keys (`is_demo`, `retryable`, `trigger`,
`pause_required`, `resume_eligible`, `validation_invalidated`, `credential_destroyed`, `disconnected_at`),
each with a label and boolean/timestamp formatting. Every other key is dropped — in particular raw backend
state enums (`from_state`/`to_state`/`status`/`resulting_status`/`phase`), internal version counters
(`state_version`/`requested_state_version`/`current_state_version`) and internal identifiers
(`job_id`/`plan_id`/`pause_record_id`/`runtime_uuid`). Because it is an allow-list, a *new* backend metadata
key can never leak through this surface. It **never** renders host paths, credentials, ciphertext, stack
traces or internal exception text. The WP5.1/5.2 backend metadata allow-lists remain the primary guarantee;
this view is a second, independent boundary. Error copy is the DRF customer-safe `detail` (via
`toCustomerError`) — never operator internals.

*(Adversarial-review fix, WP5.3-D: an earlier revision dumped the entire `metadata` dict verbatim, which
leaked `state_version`/`from_state`/`to_state`/`job_id`/`plan_id` etc. into the DOM — the strict allow-list
above replaced it; the EventDetailDialog test now asserts a realistic projection-shaped dict drops all
forbidden keys.)*

## Scope boundary — what WP5.3 deliberately does NOT do
- No writes, no acknowledge/resolve actions, no new backend or endpoint (read-only surface only).
- No new server-side filter (severity/visibility/date/search stay client-side over the page).
- No change to owner-scoping/visibility — enforced by the backend, mirrored (not re-derived) in the UI.
- Not deployed, not armed: the flag ships OFF; arming is a Sponsor-gated rebuild.

## Consequences
- Operators get a read-only console the moment the flag is armed, with zero customer-visible change while
  OFF (proven by the flag-gate test: OFF → `notFound` + zero API calls).
- Client-side filtering is bounded to one page; filtering across the whole history requires paging or a
  future server-side filter (documented, not hidden).
- The parity guard now covers the new routes/components/env var; drift fails CI.

## Tests (frontend, vitest under the `prelint`/`make check` hook)
Flag gate + operator gate + no-API-bypass (list & detail); `operationsEnabled` truthy/fail-safe; the
vocabulary mappers + palette invariant; the read-only API client (URL/params, single-arg GET); badges
(mapped label, never the enum); timeline (render/empty/click/keyboard); `applyClientFilters` per dimension
+ "category is NOT client-side" + TZ-agnostic local date-range (incl. late-evening regression); event
detail (customer-safe projection, allow-listed metadata, **forbidden internal keys dropped**, close);
pagination boundary (peek `PAGE+1`, Next disabled on an exactly-full final page); account card (link +
masked number).

## Adversarial review (WP5.3-D) — outcome
A five-lens adversarial review (security/gating, secret-leak, correctness, react-perf, a11y/test-quality)
with per-finding refutation confirmed **4 findings**: 2×HIGH (same root — the event-detail metadata dump)
and 2×MEDIUM (date-range TZ boundary, pagination Next-on-full-final-page). All four are fixed above and
covered by new/updated tests. No HIGH/MEDIUM remained.

---

## WP5.4 — Operational-events & operator-UI arming order, visibility, recorder monitoring (2026-08-04)

Amends this ADR with the **operational** arming contract for the operational-event plane and the operator
UI. Repository documentation only; nothing is armed. Full runbook: `docs/operations/broker-connectivity/`.

- **Arming order (two independent flags).** Operator observability is arming **stage 3** — the first
  DARK-safe capability, armed *before* customers so onboarding is observable from the first customer. Order:
  set backend `OPERATIONS_EVENTS_ENABLED` ON (runtime), **then** rebuild + deploy the frontend with
  `NEXT_PUBLIC_OPERATIONS_ENABLED`. The two are **independent** and **both** must be ON for the viewer to
  show data; the frontend flag is build-time (arming = a rebuild). Avoid the `operator_frontend_only` /
  `operational_events_only` mismatch states except as a deliberate transient (`rollback-matrix.md` §3–4).
- **Visibility verification.** At arming, verify owner-scoping (IDOR-safe): a non-staff owner sees only their
  own `customer_visible` events; a non-owner gets **404** (not 403); staff see all. The **summary** is scoped
  by the same `customer_only` flag. A cross-owner read is **SEV-1** cross-account leakage → disarm
  `OPERATIONS_EVENTS_ENABLED` (`incident-response.md`, `monitoring-spec.md` §15). The operator UI is
  additionally `useAdminRole`-gated and read-only.
- **Recorder-failure monitoring.** The recorder + projections are **fail-open and silent to callers** — an
  operator must confirm "events are recording" from the logger **`guvfx.operational_events`** (record/mark
  `logger.exception`, projection `logger.warning`) and durable `dedup_key` rows, never from the absence of a
  caller-side error. The log-metric plane swallows some failures with no log at all — a recorder-failure
  **counter must be added and baselined in WP6** (`monitoring-spec.md` §12).
- **Projection rebuildability during incidents.** `OperationalEvent` is a **rebuildable projection (a cache)**,
  not a second audit log; no business logic reads it. During any operational-event incident, the safe action
  is set `OPERATIONS_EVENTS_ENABLED` OFF and, if needed, **truncate and rebuild** the projection from the
  authoritative sources (`AuditEvent` + the WP1A/WP3/WP2 models) — authoritative state is unaffected
  (`rollback-matrix.md` §5, `incident-response.md`).
