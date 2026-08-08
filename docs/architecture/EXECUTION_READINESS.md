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
RULE-11 NTFS-ACL certification + the commercial RDS/licensing decision. Nothing here is enabled in
production.

## 8. Increment 3 — complete trade-operation identity safety (CLOSE / MODIFY)

Increment 2 identity-hardened opening orders. Increment 3 extends the **same invariant to every
account-mutating operation**, closing the gap the ADR red-team flagged (close/modify were ungated).

**MT5 mutation call-site inventory (E4)** — every `order_send` in `scripts/mt5_signal_bridge.py`:

| Function | Purpose | Pre-send identity check | Status |
|---|---|---|---|
| `execute_mt5_trade` | PLACE (poller) | `verify_execution_binding` | ✅ Inc2 |
| `execute_demo_order` | PLACE (HTTP `/mt5/order`) | `verify_execution_binding` | ✅ Inc2 |
| `close_position` | CLOSE (`/mt5/close-position`) | `verify_mutation_identity` | ✅ Inc3 |
| `modify_position` | MODIFY SL/TP (`/mt5/modify-position`) | `verify_mutation_identity` | ✅ Inc3 |
| shadow dry-run | validation only | n/a (no `order_send`) | n/a |

No other MT5 state-mutating primitive exists (grep-verified: `order_send` is the only mutation call; no
`positions_modify`/`Close`). Every customer-account mutation now carries the identity invariant.

**Mutation identity gate** (`evaluate_mutation_identity` / `verify_mutation_identity`): connected + active
`(login, server)` match, pin **mandatory on the persistent-workspace path** (payload `require_identity_pin`
or terminal `MT5_REQUIRE_IDENTITY_PIN`), env-optional for legacy. It deliberately does **not** require
`trade_allowed` (a risk-reducing close/modify must not be blocked by a transient trading halt — packet E2
"where appropriate") and does not re-check demo/live (close/modify are demo-guarded upstream). Identity is
threaded from the request body to `close_position(ticket, identity)` / `modify_position(ticket, sl, tp,
identity)` and re-verified **immediately before** each `order_send`. Legacy account-#1 demo close/modify
are unchanged (no pin → connected check only). Proven by `execution/tests_bridge_mutation_identity.py`
(oracle + AST mutation adequacy + enforcement structural guard).

**Increment 3 scope note:** this increment delivers the complete trade-operation identity safety (E/F/O)
+ the E4 inventory. The remaining pilot-plumbing — durable routing wiring + server-side producer
pin-derivation (B/C/D), observer pause/resume (G), host attach probe (I), read-only API (J), staff
observability (K) — is **deferred** to a follow-up increment; the repository is **not** yet full-pilot-ready.

## 9. ADR-0034 WS3 (M1) — Guarded Attach primitive (DARK)

The persistent-workspace attach must never manufacture a broker connection. Experiment H proved
`mt5.initialize(path=)` is **dual-mode**: it attaches to an already-running terminal, but if the terminal
is **down** it *launches* it and the launched terminal *auto-logs-in from cached `config\accounts.dat`* —
silently replaying the customer's saved credentials. The Guarded Attach primitive
(`scripts/mt5_signal_bridge.py`) makes the **never-launch** invariant explicit and is the M1 foundation of
the ADR-0034 workspace operating model.

- **`guarded_initialize(mt5, init_kwargs, *, probe=None)`** wraps every `mt5.initialize(**init_kwargs)` site
  in the bridge (all 10). **DARK by default:** when `MT5_GUARDED_ATTACH` is unset it is **byte-identical** to
  `mt5.initialize(**init_kwargs)` — the legacy/production bridge behaviour (which may launch) is unchanged.
  When enabled it: probes the process **before** calling `initialize` (so a down terminal is **never
  launched**), requires the terminal to report **connected** with an **account identity** after attach, reads
  and records the **masked** identity, **never** calls `mt5.login()`, and **releases** (shutdown) any attach
  it opened on failure — else fail-closed.
- **`evaluate_guarded_attach(...)`** is the pure, fail-closed decision (ordering: `no_path` →
  `terminal_not_running` → `initialize_failed` → `not_connected` → `no_account` → `ok`) — no MT5, no I/O.
- Proven by `execution/tests_bridge_guarded_attach.py`: oracle truth-table + **AST mutation adequacy** (every
  mutant killed) + wrapper fail-closed + **never-launch** (probe False ⇒ `initialize` never called) +
  **behaviour-preserving passthrough** (flag off ⇒ single passthrough call) + a **structural** invariant
  (probe precedes attach, attach guarded by `running`, no `mt5.login` in the guarded path, all 10 sites
  routed). No execution enablement, no host mutation; the order-time `evaluate_binding` authority is unchanged.

**Adversarial-review hardening (6-lens; 3 HIGH + 1 MEDIUM folded in before merge):**
- **Attach-only (never authenticate):** `initialize(login,password,server)` performs a broker login, so the
  guarded path **rejects credential-bearing `init_kwargs`** (`login`/`password`/`server`) and fails closed —
  `initialize` may only ATTACH by path. This fails closed at the legacy `/mt5/login-and-validate` site (a
  temporary-validation path, retired under ADR-0034), not a workspace attach. Legacy (flag-off) is unchanged.
- **Path-scoped probe:** `_terminal_process_running` matches strictly by the target **install directory**
  (`_running_terminal_dirs` via psutil, then wmic `ExecutablePath`), never image-name alone — a foreign
  `terminal64.exe` on a multi-install host can no longer green-light launching a down target; unresolvable ⇒
  False (fail-closed).
- **Fail-closed on raise:** the post-attach `terminal_info()`/`account_info()` IPC calls are wrapped; a raise
  (broken pipe — the degraded state the guard exists for) returns False (no propagation) and **releases** the
  attach via `shutdown()`.
- **All 11 attach sites routed:** the 10 in-file sites plus the injected `_rx2_supervision_snapshot` in
  `mt5_worker/bridge_supervision_patch.py` route through `guarded_initialize` (defensive `globals()` lookup so
  it degrades to raw `initialize` on a pre-primitive bridge; DARK-safe).

**Accepted caveat (documented, not a defect):** a sub-millisecond **probe→attach TOCTOU** window remains — if
the pre-existing terminal exits in the gap, `initialize(path=)` could launch it. This is acceptable because
its worst outcome **equals the legacy path** (launch + cached-cred auto-login) and is never worse; a later
hardening can capture the pre-existing PID set and verify the connected terminal is a member post-attach.
