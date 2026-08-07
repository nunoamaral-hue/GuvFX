# Execution Readiness — Two-Provider Model & Hardened Order-Time Gate (ADR-0033, Increment 2)

- Status: **Repository engineering, DARK.** No production deploy, no execution enablement.
- ADR: [0033](../ADRs/0033-hosted-persistent-mt5-workspace.md) (Accepted with conditions).

Increment 2 makes execution safety **stronger than before** while introducing a second readiness model.
Everything here is additive and DARK: the legacy backend gate path is byte-behaviourally identical, and
the persistent path is triple-gated (`BROKER_CONNECTIVITY_EXECUTION_GATE` **and**
`HOSTED_PERSISTENT_MT5_ENABLED` **and** `readiness_provider == persistent_workspace`).

## 1. Provider model (`backend/execution/readiness.py`)

One central gate (`broker_gate.evaluate_execution_gate`) delegates the *eligibility layer* to a
`ReadinessProvider` selected per account by `TradingAccount.readiness_provider`:

| | Provider A — `temporary_validation` (default, all existing accounts) | Provider B — `persistent_workspace` |
|---|---|---|
| Requires | `password_enc` present + `validation_status == VALIDATED` | attach-verified `HostedMt5Workspace`: connected + trade_allowed + active-account **match** + execution-capable state + observation **fresh** |
| GuvFX holds the broker password | yes (sealed, ADR-0027) | **no** |
| Lifecycle checks (`is_active`, `disconnected_at`) | enforced | **enforced (ANDed, never dropped)** |
| At dispatch: `BrokerAccountHealth` + `BrokerRuntimePause` | enforced (unchanged) | enforced (unchanged) |

Provider A reproduces the pre-ADR checks with **identical reason codes** (regression-proven by the
existing `execution/tests_broker_gate.py` + `tests_dispatch_gate.py`, unchanged). Provider B replaces
**only** `password_enc` + `VALIDATED`; it is ANDed with every other check (red-team finding #1). Its
check order reports the **most specific** failure — a wrong active account yields `active_account_mismatch`
(the central attach failure mode), never a generic not-ready — and each Provider-B code maps to the
shared dispatch vocabulary in `broker_gate._ELIGIBILITY_TO_SHARED`.

**Cache is not authority.** `HostedMt5Workspace.observed_*` / `is_execution_ready` are a cache; they gate
*eligibility* here (as `VALIDATED` is a historical eligibility fact), with a freshness bound
(`WORKSPACE_OBSERVATION_FRESH_SECONDS`). The **authority** is the live order-time gate below.

## 2. Order-time authority — hardened (`scripts/mt5_signal_bridge.py`)

The live broker-truth gate before every `order_send` remains `evaluate_binding` (pure, mutation-tested,
**unchanged**). Two additive hardenings:

- **Mandatory identity pin** (`verify_execution_binding`): the pin is enforced when the **job** declares
  it (`payload["require_identity_pin"]`) **or** when the **bridge/terminal** is configured to require it
  (`MT5_REQUIRE_IDENTITY_PIN` — a deployment property of a persistent-workspace bridge, so identity
  binding does not depend on a self-declared payload flag — review MEDIUM). In that case the expected
  `(login, server)` come from the **payload** (never the process env), are mandatory for **both demo and
  live**, and a missing/half pin fails closed (`identity_pin_required`). Legacy jobs/bridges (neither set)
  keep the exact prior behaviour.
  - **Producer contract (next increment, Tension 2):** the backend job producer MUST set
    `require_identity_pin` + `expected_login`/`expected_server` for every `persistent_workspace` job,
    derived server-side from the bound account — never left to payload discretion — and a persistent
    bridge runs with `MT5_REQUIRE_IDENTITY_PIN` set as belt-and-braces.
- **TOCTOU narrowing**: each opening path (`execute_mt5_trade`, `execute_demo_order`) re-verifies the
  binding **immediately before `order_send`**, after the last `order_check`, with no account-changing MT5
  call in between, and **rejects** on failure. This narrows the check→send window to the minimal
  in-process gap (it cannot be fully eliminated — MT5 is a separate process — but an active-account switch
  during the pre-flight→send window is now caught). Proven by `execution/tests_bridge_pin.py`.

**Invariant:** polling / workspace cache can never authorise an order. The pre-send live gate is the sole
authority, and (on the workspace path) its pin is mandatory and per-job.

## 3. Observer contract (advisory only)

The workspace observer (1–5 s, later increment) drives UI, pause/resume, notifications, connection state,
ops visibility — **never** execution. A detected mismatch pauses the strategy (reuse `signal_copy` pause)
with a customer-safe message and auto-resumes only when the expected `(login, server)` returns *and* all
safety + lifecycle checks pass (reuse `request_broker_runtime_resume`). Default: **do not queue** stale
signals.

## 4. Domain / migration (`trading` 0015)

- `TradingAccount.readiness_provider` — default `temporary_validation` (the migration sets every existing
  row to it — **never** auto-converted to persistent). Additive, reversible.
- `TradingAccount.workspace_confirmed_at` — durable onboarding acknowledgement; **NOT** an execution
  authority.

## 5. Workspace routing contract (Tension 2 — contract only, wiring deferred)

Every execution request must be attributable to **exactly one** workspace, with no shared/global MT5
state. Durable identity chain (authorises): `GuvFX user → HostedMt5Workspace (workspace_uuid, owner-bound)
→ TradingAccount → expected (login, server)`. Runtime observations (confirm only): observed login/server,
process/host. Required of the next increment: (a) the order-time gate derives the expected `(login,
server)` from the **job's bound account** (Increment 2 accepts the payload pin); (b) a server-side
slot↔account authorization at claim (wire `mt5.TerminalBinding.mt5_account_login`); (c) each Model-B
account routes to a **dedicated non-NULL node/process** (NULL/shared nodes forbidden for Model B); (d) the
host attach observation is authenticated and bound to `workspace_uuid` + owner. This increment does **not**
implement routing.

## 6. Feature flags

No new flag. The persistent execution path is triple-dark (`BROKER_CONNECTIVITY_EXECUTION_GATE` +
`HOSTED_PERSISTENT_MT5_ENABLED` + `readiness_provider == persistent_workspace`). Provider B additionally
fail-closes to `workspace_subsystem_disabled` whenever the hosted flag is OFF. `MT5_REQUIRE_IDENTITY_PIN`
is a bridge-deployment env (default off = legacy behaviour).

## 7. Known future work (deferred; do NOT enable)

Switch-observer → pause/resume **wiring**; read-only workspace-readiness **API**; workspace/connection
**observability** projection; the **routing** implementation + producer pin-plumbing (Tension 2); the host
attach probe + authenticated observation; and — Sponsor-gated — the disposable-host pilot (16 checks) +
RULE-11 NTFS-ACL certification + the commercial RDS/licensing decision. Also pre-existing and out of scope:
`close_position` / `modify_position` are not yet identity-pinned (documented follow-up). Nothing here is
enabled in production.
