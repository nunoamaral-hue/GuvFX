# 0072 — LIVE read-only open-positions monitoring gate (default-OFF)

- Date: 2026-11-04
- Status: Proposed
- Packet: GFX-LIVE-POSITIONS-READONLY-001 (Governed DARK Implementation)
- Refines: `docs/LIVE_READONLY_MONITORING_SAFETY_DESIGN.md` (PR #472). Does **not** supersede its execution-safety proof.

## Context

Production evidence (GFX-TAILSCALE-POLICY-AUTOMATION-001 acceptance, 2026-11-04): with bridge reachability
restored, `/mt5/snapshots/account` serves LIVE terminals, but `/mt5/positions` returns
`HTTP 400 {"ok":false,"error":"account_not_demo"}` on LIVE accounts — confirmed on new account #58 and
established control #48. The dashboard therefore withholds individual live-position rows (equity still reflects
floating P/L via the ungated account snapshot). We need a **strictly read-only, account-scoped** way to read
LIVE open positions for monitoring, that can **never** be, or become, execution authority — and that is OFF by
default so no redeploy can silently turn it on.

## Verified facts

- PR #472 (`feat(live-monitoring)…`, merged, DARK) removed the `trade_mode != 0` refusal from the bridge read
  handlers **unconditionally** — see `scripts/mt5_signal_bridge.py` `fetch_positions` (pre-change comment: "the
  old `trade_mode != 0` refusal is removed HERE ONLY"). There is no runtime flag gating it in-code.
- The de-gate **was** activated in production in October 2026 — but as **out-of-band per-slot host edits**, not a
  durable deployment: a hand-installed `C:\GuvFX\tenants\<id>\mt5_signal_bridge.py` = the shared bridge **minus the
  two refusal lines**, with `start_tenant_bridge.bat` repointed to it (slot 46 on 2026-10-08, slots 48+49 on
  2026-10-23). `/mt5/positions` returned `200` on 46/48/49 then (this session's own prior evidence).
- **As of 2026-11-04 the de-gate is no longer in effect**: `/mt5/positions` returns `400 account_not_demo` on #48
  (and on new account #58). So the October per-slot edit **was reverted / did not persist**, not "never deployed".
  Confirmed live by the prior packet's reads; the exact regression vector requires a read-only host inspection
  (RED/out of scope). Two confirmed candidate vectors regenerate a slot from the **canonical, still-re-gated**
  shared bridge: the host **reboot-recovery gap** (per-tenant bridges/terminals do not auto-recover; the watchdog
  relaunches via `start_tenant_bridge.bat`, which a reset restores to the shared script) and the **2026-11-03
  #603/#606 node2 capacity re-provisioning** ("re-adding an account creates a brand-new instance — new
  runtime/provisioning/endpoint"). #58 is a newer account provisioned from the canonical path and was never
  de-gated.
- `mt5.order_send` has exactly five call sites on `origin/main` (`execute_mt5_trade`, `execute_demo_order`,
  `close_position` ×2 for the partial-close lifecycle, `modify_position`); **none** is in a read handler.
  `fetch_positions` calls only `account_info` / `positions_get` / `guarded_initialize` / `shutdown`.
- HTTP dispatch is disjoint: `OHLCRequestHandler.do_GET` maps `/mt5/positions`; `do_POST` maps only the
  order/close/modify routes; no `do_PUT`/`do_PATCH`/`do_DELETE` exists (→ `501`). Token auth (`_validate_token`,
  constant-time, fail-closed) precedes every MT5 touch on both verbs.
- The backend read firewall `execution.snapshot_transport.verify_snapshot_identity(require_environment=True)` is
  UNCHANGED: login EXACT + server (casefold) + observed DEMO/LIVE environment, fail-closed on any mismatch.
- The config renderer `execution.bridge_config.render_bridge_env` has no production caller (tests only), so a new
  defaulted parameter cannot change any current behaviour.

## Assumptions

- The current per-slot bundle hashes on #48/#58 were **not read on the host** in this packet (production access is
  RED / out of the repo+test scope). That the October per-slot de-gate reverted (vs the slot being re-gated some
  other way) and *which* vector caused it (reboot-recovery vs #603/#606 re-provisioning) are inferred from this
  session's prior evidence + the observed `400`; a read-only host hash/task inspection (separate gate) would
  confirm it. Recorded as a limitation, not a fact.
- The regression is *why the fix matters*, but the fix does not depend on resolving it: a durable, default-OFF
  env flag consumed by the canonical shared bridge survives re-provisioning; a hand-edited per-slot script copy
  demonstrably did not.

## Decision drivers

Safety (a read must never trade), default-OFF (a redeploy must not activate it), reversibility, minimal scope
(positions only), execution-independence, honest reporting (unavailable ≠ empty), governance (no deploy here).

## Options considered

- **A — leave #472's unconditional de-gate (activated by out-of-band per-slot host edits).** Rejected: this is
  exactly the broken boundary. The capability has **no durable, default-safe, version-controlled gate**, so it
  flips silently and invisibly in BOTH directions — (a) an out-of-band per-slot edit **reverts** on reboot /
  re-provision (observed: #48 live reads regressed to `account_not_demo`), and (b) deploying the shared de-gated
  bridge would **turn LIVE reads ON** for every live account with no explicit, revocable decision. Either flip is
  invisible because the read fails to "stale", not loud.
- **B — flag-gate the positions read, default-OFF (chosen).** A purpose-specific, read-only env flag
  `MT5_LIVE_READONLY_POSITIONS_ENABLED`. Absent ⇒ a LIVE terminal is refused `account_not_demo` exactly as the
  pre-#472 bridge; DEMO is unaffected. Activation is an explicit per-tenant host-config decision, independent of
  every execution gate.
- **C — new dedicated read-only endpoint.** Rejected as scope creep: duplicates auth/dispatch/firewall and
  widens surface; the existing GET + firewall already satisfy the contract.

## Decision

Adopt **B**. The live open-positions read is gated by `MT5_LIVE_READONLY_POSITIONS_ENABLED`, default-OFF, read
fresh per call (env-driven; cleared on restart). With it OFF, deploying the refined bridge reproduces **exactly
current production behaviour** (live positions withheld; equity shown). The backend emission gate
(`readonly_positions_for_account` + the default-OFF `LIVE_READONLY_POSITIONS_MONITORING_ENABLED` authority) is
separate from `MT5_ALLOW_LIVE` / `live_execution_permitted` / `copier_live_execution_authorized` /
`LiveExecutionAuthorization` / `HOSTED_LIVE_EXECUTION_ENABLED`.

**Execution boundary — preserved verbatim.** This decision changes ONE read path. Every
`order/close/modify/order_check` demo gate and the whole LIVE-execution-authorization chain are byte-for-byte
unchanged; `order_send` remains unreachable from any read; the read flag can never place, size, modify, close,
cancel or authorise an order, and can never arm execution. `fetch_deals_snapshot` / `fetch_account_snapshot`
remain ungated for LIVE (out of scope; #472 unchanged).

## Consequences

- DARK, additive. No migration. No production deploy or flag enablement in this packet.
- Response gains provenance (`account_login`/`account_server`/`trade_mode`) + observation time (`observed_at`) +
  bounded size (`total_count`/`truncated`, cap `_READONLY_POSITIONS_MAX=1000`, truncation disclosed).
- Audit: bridge logs served (`info`) and refused (`warning`) LIVE reads, secret-free; the backend firewall logs
  identity refusals and the client logs transport/timeout failures.
- The existing #472 bridge tests are updated to the new default-OFF contract (called out in the PR).

## Risks and controls

- *A redeploy turns monitoring on* → controlled: default-OFF in-code; only an explicit per-tenant env var enables
  it. **(amber→green)**
- *Read becomes write* → controlled: structural dispatch (GET-only), no `order_send` in reads, execute gates
  untouched; adversarial tests pin it. **(green)**
- *Cross-tenant / wrong-environment read* → controlled by the unchanged firewall (login EXACT + server + env).
- *Fake-green empty* → controlled: `positions_get() is None` ⇒ `positions_get_failed` (unavailable, never empty).

## Evidence / validation

`backend/execution/tests_bridge_live_readonly.py` (updated) + `backend/execution/tests_live_readonly_positions_flag.py`
(new, maps matrix 1–18) + `tests_live_readonly_firewall.py` + `tests_d4b_router_allowlive.py` (renderer). Focused
run: 39 passed. Full `execution` app regression + secret scan: recorded in the packet report. Deployed host-bundle
hash: NOT read (RED/out of scope) — stated as a limitation.

## Reversal path

Code: revert this PR (restores #472's unconditional de-gate). Runtime: the flag is never set in this packet;
if later set, clearing `MT5_LIVE_READONLY_POSITIONS_ENABLED` and restarting the bridge removes the capability.

## Revisit trigger

The separately-gated decision to deploy the refined bridge bundle and/or arm monitoring for a specific LIVE
account; or any proposal to extend read-only monitoring to `deals`/other endpoints.

## Approval

PM owns lifecycle status; this ADR is **Proposed**. Repo/test implementation authorised by the packet; deploy and
flag-enablement are a separate Sponsor/operator gate (RED).
