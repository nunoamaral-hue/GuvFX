# Monitoring Specification — Broker Connectivity Trusted Beta (WP5.4 Workstream H)

**Specification only — no monitoring is deployed by this package, and no automatic action is authorised for
any signal.** Alert *detection* signals exist in code today; **delivery** is manual until
`NOTIFICATION_DISPATCH_ENABLED` and a confirmed sink are separately approved (the estate has **no external
monitoring platform**; Grafana/Loki/Prometheus are referenced but not running — see
`docs/OPERATIONS_DASHBOARD.md` §5).

**Baselines.** No numeric monitoring baseline exists in the repository. Every threshold below is either a
config *threshold* (not a measured baseline) or absent → marked **`TO BE BASELINED DURING WP6`**. Thresholds
are **not invented**.

**Two signal planes (do not conflate).**
1. **Log-based plane** — fire-and-forget single-line JSON to container logs (`core/observability.py`);
   **no in-repo consumer, no store, no baseline**; presumes an external Grafana/Loki that is **HOST-VERIFIED
   / OUTSIDE REPOSITORY CONTROL**.
2. **DB + endpoint plane** — durable rows (`AlertEvent`, `ComponentHealth`, `BrokerAccountHealth`,
   `OperationalEvent`, `BrokerAccountValidationAttempt`, `AuditEvent`, `SoakSnapshot`) surfaced via the
   reliability/operations endpoints (`GET /api/reliability/operations-summary/` staff-only, etc.).

**Gating that changes what is observable** (flag NAMES only): `RELIABILITY_CORE_ENABLED` OFF → much of the
`ComponentHealth` matrix reads UNKNOWN; `BROKER_CONNECTIVITY_HEALTH_ENABLED` OFF → WP3 signals DARK;
`OPERATIONS_EVENTS_ENABLED` OFF → the whole operational-event plane DARK; `EXECUTION_HEALTH_ENABLED` ON →
the execution-health detectors are live.

**Common fields:** every signal below has **Owner = Operator** (review) and **Automatic action =
PROHIBITED** (this package authorises no auto-remediation and no automated external notification). Only the
per-signal specifics are listed.

---

### 1. Validation success / failure rate
- **Source:** `trading.BrokerAccountValidationAttempt` (`status` ∈ HEALTHY/NEEDS_ATTENTION/UNAVAILABLE),
  per-account surfaced at `trading/views.py:534`; DARK projection `OperationalEvent` VALIDATION.
- **Definition:** `count(status=HEALTHY) / count(*)` over the window, per trigger. **No aggregate exists** —
  compute by group-by.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`. **Window:** rolling 24h (WP6 to confirm).
- **Response:** falling success rate → check broker/agent/image health ([rollback-matrix.md](rollback-matrix.md)).

### 2. Technical-unavailable rate
- **Source:** `BrokerAccountValidationAttempt` where `status=UNAVAILABLE` (retryable platform faults).
- **Definition:** `count(status=UNAVAILABLE) / count(*)` over the window.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** a spike ⇒ platform fault (agent/image/isolation) — SEV-2; investigate, do not tell customers
  to re-enter credentials.

### 3. Validation latency
- **Source:** validation-runner diagnostics (`validation_diagnostics.write_evidence`: stage timeline,
  `first_failing_stage`, `last_error_code`); the timeout contract bounds it (mt5 login 120s, agent grace
  45s, backend op 175s).
- **Definition:** wall-clock per attempt; stage-reached on failure. **No latency counter is aggregated.**
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`. The 175/165/120s values are **contract
  bounds, not a latency baseline**.
- **Response:** latencies approaching the contract bound ⇒ investigate the runner/host.

### 4. Credential-access audit count
- **Source:** `core.audit.AuditEvent` type `CREDENTIAL_ACCESSED` (redacted: account id, masked last-4,
  broker, demo/live; never the secret), emitted at decrypt-point-of-use.
- **Definition:** `count(type=CREDENTIAL_ACCESSED)` per account per window; **stream, not a rate**.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`. A *rate anomaly* (far more accesses than
  validations) is the signal of interest.
- **Response:** repeated anomalous access ⇒ SEV-2; confirmed exposure ⇒ SEV-1 (stop, do not commit, rotate).

### 5. Broker-health state distribution
- **Source:** `BrokerAccountHealth.state` (DB-indexed `brokerhealth_state_idx`); per-account `get_contract`.
  **No endpoint returns the distribution** — one group-by away. DARK unless `BROKER_CONNECTIVITY_HEALTH_ENABLED`.
- **Definition:** `count(*) group by state` (UNKNOWN/HEALTHY/DEGRADED/STALE/DISCONNECTED/TOMBSTONED).
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** unexpected mass DEGRADED/DISCONNECTED ⇒ SEV-2; disarm the gate first if armed.

### 6. Health-convergence delay
- **Source:** `BrokerAccountHealth.state_version` transitions vs the `BrokerAccountValidationAttempt` evidence
  time (`last_success_at` = attempt `created_at`).
- **Definition:** time between a validation attempt and the health net transition it should cause. **Absent
  as a metric.**
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6` (must be *added*, not just baselined).
- **Response:** growing convergence delay ⇒ investigate the fold path.

### 7. Paused-runtime count
- **Source:** `execution.BrokerRuntimePause` (`is_broker_paused`); DARK projection RUNTIME
  `broker_runtime_paused`.
- **Definition:** `count(paused=true)`. **No metric** — count rows/events.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** a pause surge ⇒ correlated broker/health incident (SEV-2).

### 8. Controlled-resume success / refusal rate
- **Source:** DARK projection RUNTIME `broker_resume_completed` / `broker_resume_idempotent` /
  `broker_resume_refused`; audit `BROKER_RUNTIME_RESUMED`.
- **Definition:** `count(completed) / count(completed+refused)`. Refusals use empty dedup so distinct reasons
  are distinct rows.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** high refusal rate ⇒ resume being requested for ineligible accounts; verify the contract.

### 9. Creation-gate refusal count
- **Source:** `EXECUTION_GATE_REFUSED` audit (`stage=creation` / `creation_paused`); DARK projection
  EXECUTION `broker_execution_gate_refused`. DARK unless `BROKER_CONNECTIVITY_EXECUTION_GATE`.
- **Definition:** `count(EXECUTION_GATE_REFUSED)` per window/reason.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** refusals for **eligible** accounts ⇒ SEV-2 refusal spike → disarm the gate.

### 10. Claim-boundary refusal count
- **Source:** `EXECUTION_DISPATCH_REFUSED` emitted at the `next_job` claim gate (`execution/views.py:359-364`).
- **Definition:** `count` of jobs failed at claim with `broker dispatch gate refused at claim: <code>`.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** as (9); a claim-boundary spike indicates account eligibility changed between enqueue and claim.

### 11. Final-dispatch refusal count
- **Source:** `EXECUTION_DISPATCH_REFUSED` at the worker final-dispatch recheck
  (`mt5_trade_ingest_worker.py:807-819`).
- **Definition:** `count` of jobs failed at dispatch before `order_send`.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`.
- **Response:** as (9)/(10); this is the last TOCTOU backstop — a spike here with a clean claim gate is
  notable.

### 12. Operational-event recorder failures
- **Source:** logger **`guvfx.operational_events`** — `record_event` / `mark_resolved`
  `logger.exception(...)`; projection `logger.warning(... exc_info=True)`. **Log-only, no counter.**
- **Definition:** `count` of those log lines per window. **Blind spot:** the log-metric plane
  (`observability.py:48-53`) swallows exceptions with **no log at all** — recorder failures there are silent.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6` (a recorder-failure **counter must be added**).
- **Response:** any recorder failure ⇒ investigate; the projection is a cache so authoritative state is safe,
  but "events are recording" cannot be assumed from an absent caller error.

### 13. Operational-event lag
- **Source:** `OperationalEvent.created_at` vs the authoritative moment. **No dedicated lag signal exists.**
- **Definition:** time between the authoritative action (`on_commit`) and the projected row.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6` (must be **added**). Nearest existing
  freshness signals: heartbeat age, `SNAPSHOT_FEED` tick age (`SNAPSHOT_STALE_SECONDS=300`), broker-registry
  `synced_at`.
- **Response:** growing lag ⇒ projection backlog; check recorder health.

### 14. Operator API errors
- **Source:** **NOT FOUND IN REPO** — there is no 5xx / request-error counter for the reliability/operations
  endpoints. Endpoints are individually fail-safe (return `{"status":"UNKNOWN"}` on exception), so failures
  degrade in place and are **not counted**.
- **Definition:** `count(5xx)` on `/api/operations/*` and `/api/reliability/*` — **a new signal**.
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6` (must be **added**).
- **Response:** operator-facing errors ⇒ SEV-2/3 depending on scope.

### 15. Account isolation violations
- **Source:** owner-scoping is enforced in code (`operational_events/views.py:39-46`, non-staff own-only →
  404). **No violation counter exists** — a violation would be a defect, detected by test/report.
- **Definition:** any cross-owner read served = **0 tolerated**.
- **Baseline / warn / critical:** target **0**; any occurrence is **critical** (SEV-1). Not a baseline — an
  invariant.
- **Response:** any violation ⇒ **SEV-1** cross-account leakage; disarm `OPERATIONS_EVENTS_ENABLED`.

### 16. Duplicate-event rate
- **Source:** `OperationalEvent` partial-unique `opev_uniq_dedup_key` + `get_or_create`. Dedup is
  **enforced, not measured** — no collision counter.
- **Definition:** `count` of `get_or_create` hits (would require instrumentation).
- **Baseline / warn / critical:** `TO BE BASELINED DURING WP6`. For keyed events a true duplicate should be
  impossible.
- **Response:** unexpected duplicates ⇒ a dedup-key derivation bug; investigate.

### 17. Missing-event reconciliation count
- **Source:** existing live detectors emit deduped `AlertEvent`s:
  `execution_health.detect_unplanned_tradeable_signals`, `detect_stuck_promotions`,
  `detect_stuck_pending_orders`, `reconcile_orphaned_place_orders`, `detect_saturated_concurrency_gates`,
  `detect_protection_watcher_health`; operations-summary `signal_dispositions.silent_loss_total` (target 0),
  `execution_jobs.orphaned_running`.
- **Definition:** `silent_loss_total` and reconciliation counts — some exist as **targets**, not baselines.
- **Baseline / warn / critical:** `silent_loss_total` target **0** (invariant); collision/missing counts
  `TO BE BASELINED DURING WP6`.
- **Response:** `silent_loss_total > 0` ⇒ SEV-1/2 (a signal was lost); use the sanctioned reconciler, never
  re-run a job.

---

## Endpoints / sources a future monitoring build can consume

- `GET /api/reliability/operations-summary/` — staff-only master rollup (`build_operations_summary`).
- `GET /api/reliability/health/` (component matrix), `/trading-health/`, `/alerts/`, `/recovery-status/`.
- `GET /api/operations/account-events/` — owner-scoped operational timeline + summary (DARK until armed).
- `reliability.AlertEvent` (INFO/WARN/CRITICAL; delivery best-effort Telegram/webhook, **no external
  platform**); `reliability.SoakSnapshot` (daily); `core.audit.AuditEvent`; loggers
  `guvfx.execution.lifecycle` / `guvfx.execution.metrics` / `guvfx.operational_events`.

## Explicit limitations for WP6

1. The log-metric plane has **no in-repo consumer** — aggregation/baselining presumes an external stack that
   is HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL.
2. **Recorder-failure counting, operational-event lag, and operator-API errors do not exist as first-class
   metrics** and must be **added**, not merely baselined, in WP6.
3. **No numeric baseline** exists for any signal; every "expected value" is `TO BE BASELINED DURING WP6`.
4. **No automatic action** is authorised for any signal in this package.
