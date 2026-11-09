# ADR-0087 — Copy Trading per-tenant mutation transport + narrow RED live-admission (DARK, default-OFF)

- **Status:** Accepted (repository-only / DARK, default-OFF). Host deployment of the bridge admission, flag
  enablement, live-authorization creation, route activation and any live use are separate Sponsor gates. No
  broker order, no live mutation, no live arming is part of this packet.
- **Packet:** GFX-COPIER-PHASE4-TRANSPORT-ADMISSION-031.
- **Date:** 2026-11-09.
- **ADR number:** 0087 — the next genuinely free number.
- **Builds on:** ADR-0083 (the live command envelope), ADR-0084 (the guard), ADR-0085 (the bridge wiring +
  recorder), ADR-0086 (the dispatcher + migration-free watermark). Nothing in those is modified except the
  three bridge money functions' demo wall (below), which stays byte-identical when the admission is OFF.

## Decision

Two DARK, default-OFF layers complete the (still-unarmed) live-mutation path for the **sole governed route**
master #46 (TradersWay / Tradersway-Live) → follower #48 (IS6FX / IS6Technologies-Live).

### 1. Real per-tenant mutation transport (`trade_copier/mutation_transport.py`)

The dispatcher's real transport (it ships INERT by default). `send_mutation(command)`:

- **Route-pins** to the governed follower: `resolve_governed_follower_route` requires the replication's follower
  to be an ENABLED copier follower and resolves its OWN per-tenant endpoint via the audited identity firewall
  (`snapshot_transport.resolve_account_snapshot_base`, which fails closed — **never a global / sibling bridge**).
- **Identity-binds**: the command's server-derived `expected_login` / `expected_server` must equal the resolved
  follower account's `account_number` / `broker_server.server_name` (`_identity_matches`). A caller supplies NO
  url / account / symbol.
- **Authenticates** with the env-resolved agent token (`monitoring_gateway_client._agent_token`) — the token
  NEVER enters the repository, command arguments, logs, Notion or chat.
- **Deadline**: refuses a command past `authorization_expires_at` (defence in depth with the guard).
- **One shot**: POSTs the typed MODIFY/CLOSE to the op-specific bridge route (`/mt5/modify-position` /
  `/mt5/close-position`) with a tight timeout, **never retried**. A timeout / transport error / non-2xx with no
  decisive body is `UNKNOWN_OUTCOME` + `need_reconcile` — **never inferred as success**. A decisive bridge body
  (carrying the guard's `lifecycle_outcome`, or a `guard_blocked` pre-send refusal) is passed through.
- **Default OFF** (`copier_mutation_transport_enabled`, ANDed with the dispatcher arm stack): INERT, contacts no
  broker. PARTIAL_CLOSE keeps its dedicated worker (ADR-0056); OPEN keeps the PLACE_ORDER path.

### 2. Narrow RED live-mutation admission (`scripts/mt5_signal_bridge.py`)

`_copier_live_mutation_admitted(account_info, command, op)` narrows the bridge `account_not_demo` wall for the
one governed route only. The money functions now use `_demo_or_admitted_ok(account_info, command, op)` in place
of `if account_info.trade_mode != 0`. It returns False (⇒ demo wall unchanged, **byte-identical**) unless **every**
condition holds, and fails closed on any miss and on any error:

1. `COPIER_LIVE_MUTATION_ADMIT_ENABLED` on (default OFF).
2. The full existing live stack admits this runtime independently: `_bridge_live_admitted()` =
   `MT5_ALLOW_LIVE` **and** a valid, non-expired `MT5_LIVE_EXPIRY` (a single flag can never enable trading).
3. A typed copier command for the SAME mutation op, with `require_identity_pin`.
4. **Exact-account triple agreement**: `MT5_EXPECTED_LOGIN` (runtime) == `account_info.login` (broker-connected)
   == `command.expected_login`, all non-blank; server likewise (case-insensitive) when configured.
5. **Symbol pin**: `command.symbol` == `COPIER_LIVE_ADMIT_SYMBOL` (the governed XAUUSD mapping).

It can authorise **no other account, route, symbol or operation**. It is read FRESH per call (env, not process
state), so a restart, expiry or identity drift fails closed. It creates no authorization.

### Dispatcher refinement

A genuine pre-send refusal (`guard_blocked`, no `lifecycle_outcome` — the transport's route/identity/deadline
block, or a bridge admit block) maps to a terminal `CONTAINED_FAILURE` (FAILED + alert): nothing was sent and it
cannot recover — never a false `RUNNING` and never a retry loop. The INERT transport (`mutation_transport_disabled`)
reverts the job to `PENDING` (arming not complete); nothing sent, nothing recorded.

## Safety / what stays OFF

No migration. The money-function demo wall is byte-identical when the admission is OFF (default). No deploy of the
bridge admission to the Windows host, no flag enablement, no live authorization, no route activation, no broker
order. The transport is INERT by default. Enabling the live path requires, together and each Sponsor-gated: the
transport flag, the dispatcher flag, the live-execute arm stack, the bridge admission flag + `MT5_ALLOW_LIVE` +
`MT5_LIVE_EXPIRY` + the exact-account env, a `CopierLiveAuthorization`, route activation and the kill switch not
engaged.

## Remaining before a live #46→#48 demo

1. **Deploy the backend transport** (this packet's DARK deploy) — flags OFF.
2. **Deploy the bridge RED admission to the Windows host** (`scripts/mt5_signal_bridge.py`) — a SEPARATE
   Sponsor-gated host operation, NOT the backend deploy; flags OFF.
3. Activate #46/#48 + provision/verify their terminals, bridges, observer and watchdog + the per-tenant endpoint.
4. A short-lived `CopierLiveAuthorization` + the minimum armed controls, Sponsor-present, for the human-operated
   demo; immediate return to OFF afterwards. No synthetic test asserts live readiness.
