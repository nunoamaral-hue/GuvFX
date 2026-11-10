# 0091 — Hosted-observation concurrency budget + signed-transport error taxonomy

- Date: 2026-11-10
- Status: Accepted

> Renumbered from 0090 to 0091 during branch reconciliation: `main` advanced (PR #635) and filed
> `docs/architecture/ADR-0090-vantage-withdrawal-correlation.md`, so 0090 is taken programme-wide. This ADR
> (originally filed as 0090 when that number was free) was moved to the next free number, 0091, to satisfy the
> ADR-uniqueness rule. No content changed except the number.

## Context

Packet 046 (read-only diagnosis) proved why the governed `#46→#48` live copy-trading demo failed at the host-
admission `--commit` step with `attestation_failed:host_unavailable`, while SSH and the per-tenant bridge
`/health` were both healthy:

- The signed host-executor daemon (`C:\GuvFX\hosted\executor\daemon.py`, WinSW service `GuvFXHostedExecutor`,
  listening on `100.79.101.19:8790/hosted/provision`) admits at most **`max_connections` = 8** concurrent
  connections (`BoundedThreadingHTTPServer.process_request` → `self._conn_sem.acquire(blocking=False)`; over the
  cap it calls `shutdown_request(request)` — the socket is **closed with no response, no handler thread, and
  nothing logged**).
- The backend's bounded observation cycle fans out across every hosted workspace (28 at the time) through a
  worker pool whose size defaulted to **8** — exactly the daemon's cap. Each OBSERVE holds its connection for the
  multi-second observe wait, so the pool alone could occupy **every** executor permit for the whole cycle,
  leaving **zero** headroom for a one-shot signed control-plane op.
- The one-shot `ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED` arrived during a saturated window, lost the permit race,
  and was refused pre-dispatch. On the client, `urllib.urlopen` raised `RemoteDisconnected`, which
  `hosted_workspace.host_executor.SignedHostExecutor._send` caught in a single `except Exception` and collapsed
  into the opaque `host_unavailable` — the **same** label it uses for a genuinely unreachable host. That
  conflation sent the live diagnosis toward "host down" when the host was healthy but saturated.

This ADR records the **repository-only** repair (packet 047): bound observation concurrency below the executor
cap with reserved control-plane headroom, and de-conflate the transport error so operators are told the truth.

## Verified facts

- Daemon cap and refusal path: `daemon.py:194-197` (host; read read-only in packet 046). Default
  `HOSTED_EXECUTOR_MAX_CONNECTIONS` = 8 (`daemon_config.py`), `HOSTED_EXECUTOR_MAX_BODY_BYTES` = 65536.
- Observe fan-out: `backend/hosted_workspace/bounded_observation.py` — `run_bounded_observation_cycle` uses a
  `ThreadPoolExecutor(max_workers=_max_workers())`; the old default was 8 (hard ceiling 12 = Node-2 capacity).
- Transport error collapse: `backend/hosted_workspace/host_executor.py` `_send` returned
  `{"ok": False, "reason": "host_unavailable"}` for **any** raised transport exception.
- `host_unavailable` was an **opaque breadcrumb** downstream: `poll_diagnostics.record_poll_unavailable` stores
  the reason verbatim; `corroboration_diagnosis.classify_corroboration_result` routes every reason except
  `no_observer_task` to `OBSERVE_UNAVAILABLE` (a catch-all) — so no runtime consumer branches on the exact
  literal, and a more specific code is backward-compatible.
- Packet-046 final state (unchanged by this repository work): prod `7ef7ae10`; all copier/execution controls
  OFF; host admission OFF; 0 active authorizations (id=3 REVOKED); kill switch not engaged; 0 copier jobs;
  0 broker orders.

### Superseded root-cause wording

The packet-045 ledger entry attributed the failure to "the signed host-executor provisioning channel is
unavailable (infrastructure)." That wording is **superseded**: the channel is healthy; the cause is backend
observation **over-subscription** of a finite host connection pool, plus a misleading client error label.

## Assumptions

- The host daemon's `max_connections` remains 8 unless an operator changes it; the backend mirror
  (`HOSTED_EXECUTOR_CONNECTION_CAPACITY`, default 8) MUST be kept equal to it. This ADR does **not** change the
  host value (explicitly out of scope).
- There is currently a **single** host-executor endpoint (`HOSTED_EXECUTOR_BASE_URL`), so a single global
  observation budget is correct. Per-host budgets are a documented future extension, not built here (building a
  per-host budget with only one host would be untestable dead code).

## Decision drivers

Safety (never starve a time-critical signed op), reversibility (config-only, no migration, no host mutation),
truthfulness of operator evidence (RULE 5 / RULE 11), minimal blast radius, and the governance boundary (no
host-side change without a separate Sponsor deploy packet).

## Options considered

- **A — Backend observation-concurrency budget with reserved control-plane headroom (chosen first repair).**
  Bound the observe pool to `capacity - reserved`. No host mutation, config-only, reversible, testable. Con:
  slightly slower per observe cycle (more waves) — acceptable for a read-only, cron-cadence path.
- **B — Raise the host daemon's `max_connections`.** Host mutation (Sponsor-gated, supervised WinSW restart,
  RULE 1). Complementary, not a substitute: without a backend reservation a larger pool could still be sized to
  the new cap and re-saturate it. Deferred to an optional separate deploy packet.
- **C — Client-side retry of the failed op.** Rejected for consequential ops: a retry that cannot prove the
  primitive never ran risks a late or duplicate live mutation. See the retry prohibition below.

## Decision

1. **Observation concurrency budget (preferred first repair).** `bounded_observation.observation_budget()` is the
   single source of truth. Defaults: observation concurrency **4** (`HOSTED_OBSERVATION_MAX_WORKERS`), executor
   capacity mirror **8** (`HOSTED_EXECUTOR_CONNECTION_CAPACITY`), reserved control-plane slots **2**
   (`HOSTED_CONTROL_PLANE_RESERVED_SLOTS`). Invariant: `1 <= effective <= capacity - reserved` and
   `reserved >= 1`; also capped by the Node-2 ceiling (`_HARD_MAX_WORKERS` = 12). Excess observations are
   **queued** by the pool (never dropped); every workspace is still observed within the cycle. The budget **fails
   closed against saturation**: any invalid, zero, negative or `>= capacity` setting, or a reserved value that
   would leave no headroom, is clamped into the safe range and recorded in `notes` + a WARNING — observation
   keeps running (freezing login→CONNECTED detection would be the worse failure) but can never occupy the
   reserved headroom.

2. **Transport error taxonomy.** `hosted_workspace.transport_errors.classify_transport_exception` replaces the
   single `host_unavailable` with truthful, secret-free codes: `host_unreachable` (never reached the host),
   `host_connection_dropped_pre_response` (accepted then closed before any HTTP response — what the cap refusal
   looks like, labelled by what was observed, never *inferred* as capacity), `host_http_error` (explicit non-2xx,
   integer status only), and a generic `host_transport_error` fallback. `host_observation_capacity_deferred`
   labels the backend-side deferral metric, never a transport outcome. `_send` now returns the specific code;
   `is_host_unavailable_reason()` groups all of them (incl. legacy) for any consumer that needs the old "did not
   reach a usable response" semantics.

3. **Retry prohibition for ambiguous consequential outcomes.** No automatic retry is added to any consequential
   signed op (ACTIVATE / MODIFY / CLOSE / decommission): they remain single-attempt, fail-closed — exactly one
   POST per step. A `host_connection_dropped_pre_response` *is* strong evidence the primitive never ran (the
   daemon only closes-without-response at `process_request`, before dispatch; every dispatched request writes a
   full response), so a bounded safe retry for that specific pre-dispatch case is viable — but it is recorded as
   a **separate follow-up**, not implemented here, so safety is never weakened to chase convenience.

## Consequences

- A one-shot signed control-plane op always finds at least `reserved` (default 2) free executor permits while
  observation runs at its maximum, so the packet-046 starvation cannot recur under default config.
- Operators and evidence get the true failure mode (`host_unreachable` vs `host_connection_dropped_pre_response`
  vs `host_http_error`) instead of a blanket `host_unavailable`.
- No change to tenant isolation, route identity pins, broker-order authority, or any copier/execution flag. No
  new model/migration. All copier and execution controls remain OFF and DARK; this is a repository-only repair.
- Follow-ups (separate, not in this PR): optional host-side `HOSTED_EXECUTOR_MAX_CONNECTIONS` increase (Sponsor
  deploy packet); a safe bounded retry limited to `host_connection_dropped_pre_response`; applying the taxonomy
  to `broker_catalogue/host_catalogue.py` (a different transport that still returns `host_unavailable`);
  per-host observation budgets if a second host-executor endpoint is introduced.

## Risks and controls

- **Risk:** backend capacity mirror drifts from the host cap (observation throttled unnecessarily, or — if the
  host cap were ever *lowered* below the mirror — headroom under-reserved). **Control:** the mirror default (8)
  equals the host default; the invariant and docstring require the two to be changed together; the budget still
  fails closed to `capacity - reserved`. Amber: a host cap change is a separate Sponsor-gated deploy.
- **Risk:** a transport code leaks secrets. **Control:** the classifier is type-only and returns at most
  `{reason, http_status:int}`; tested that an HTTP-error body/token never appears in the result.
- **Red boundary unchanged:** no live/paper authority, no host mutation, no flag armed, no order.

## Evidence / validation

Evidence manifest: `evidence/manifests/GFX-EVD-COPIER-HOST-EXECUTOR-CAPACITY-REPAIR-047.json`.
New/updated tests: `tests_transport_errors.py` (taxonomy mapping, grouping-compat, fail-closed `_send` with no
secrets, single-attempt no-retry on a consequential op), `tests_bounded_observation.py` (default headroom,
clamp-to-`capacity-reserved`, fail-closed on invalid/zero/negative/`>=capacity` and on `reserved>=capacity`,
peak-in-flight never exceeds `effective` across 28 workspaces, excess queued/deferred not dropped), plus three
updated transport-failure assertions (`host_unavailable` → `host_unreachable`). Full `hosted_workspace`,
`execution`, `trade_copier`, `broker_catalogue` suites run green; `makemigrations --check` shows no change.
Not covered: any host-side behaviour (no host contact in this packet); per-host budgets (single-host model).

## Reversal path

Image-tag rollback only — no migration, so no reverse migration. Reverting the three config defaults (or setting
`HOSTED_OBSERVATION_MAX_WORKERS` back to 8 and removing the capacity/reserved settings) restores the prior
behaviour. The taxonomy change is backward-compatible (grouping helper preserves old semantics).

## Revisit trigger

A change to the host daemon's `HOSTED_EXECUTOR_MAX_CONNECTIONS`, the introduction of a second host-executor
endpoint, or any recurrence of control-plane starvation under load.

## Approval

Packet GFX-COPIER-HOST-EXECUTOR-CAPACITY-REPAIR-047 (repository-only implementation, APPROVED). PM owns the
lifecycle status in Notion. Deployment and the Sponsor-present demo retry remain separate, NOT-authorised gates.
