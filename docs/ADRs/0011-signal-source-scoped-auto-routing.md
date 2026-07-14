# 0011 — Signal-source-scoped auto-routing

- Date: 2026-07-14
- Status: Accepted

## Context

We are adding a second Telegram-signal auto-copy strategy ("Wayond WIM Strategy",
sourced from the "TI Signals" channel, provider slug `ti_signals`) alongside the existing
Wayond auto-demo. The execution auto-router resolves the target account/strategy for an
acquired signal via `execution/auto_router.py::_resolve_target`, which returned **the single
globally-unique** active `AUTO_DEMO`/`AUTO_SHADOW` `StrategyAssignment` (stage LIVE, demo
account) — with **no provider/source filter**. A naive second `AUTO_DEMO` assignment would
make that lookup ambiguous (`len(hits) == 2`) and fail closed to `None`, silently stopping
**both** strategies from auto-executing — including the currently-live Wayond demo. We need
more than one auto-copy strategy to coexist and route independently, without regressing the
live path.

## Verified facts

- `_resolve_target(assignment_mode)` selected a target with no provider/source scoping and
  returned `hits[0] if len(hits)==1 else None` (`backend/execution/auto_router.py`, pre-change).
- A signal's source is `PendingSignalApproval.source`, set to the provider slug in
  `signal_intake/services.py::intake_parsed` (e.g. `"wayond"`, `"ti_signals"`).
- The live Wayond auto-demo assignment carries no source binding (the field did not exist).
- The auto path is still globally gated by `ExecutionControl` (`auto_execution_enabled`,
  `signal_execution_mode`, kill switch), `SignalSourceConfig(source=…)`, provider ARMED, and
  parser certification ≥ MEDIUM — none of which this change alters.

## Assumptions

- Exactly one `AUTO_DEMO` assignment per signal source in a correct prod config (enforced by
  fail-closed `None` on >1, not by a DB constraint).
- TI Signals messages differ in format from Wayond (confirmed by sample) and use their own
  parser profile `ti_signals_v1`.

## Decision drivers

Safety (must not regress the live Wayond demo), reversibility, minimal blast radius,
fail-closed default, and the governance boundary (this touches the execution gate → Amber).

## Options considered

- **A — `StrategyAssignment.signal_source` binding + source-scoped resolver (chosen).**
  Add a nullable/blank field binding an `AUTO_*` assignment to one source; resolve the target
  by matching the signal's source, with a legacy fallback for unbound assignments. Additive,
  back-compatible, self-contained in strategies + execution.
- **B — FK `SignalProvider.strategy`/`StrategyAssignment`.** Couples signal_intake to
  strategies and inverts the existing one-way dependency; larger blast radius.
- **C — mapping on `SignalSourceConfig`.** Splits the source→target link across two apps and
  overloads a config row that is about arming, not routing.

## Decision

Option A. Add `StrategyAssignment.signal_source` (CharField, blank, default `""`, indexed) and
make `_resolve_target(assignment_mode, source)` source-scoped and fail-closed:

1. If the source is **claimed** (any assignment — active or paused — is bound to it), stay
   within that claim: return the unique **active** bound assignment, else `None`. A paused
   bound assignment yields `None` and never falls back — disabling one signal-copy strategy
   stops it, it does not re-route its signals to another strategy.
2. If the source is unclaimed, fall back to the legacy globally-unique **unbound** assignment —
   but ONLY while at most one auto-source is enabled. Once more than one `SignalSourceConfig`
   has `auto_demo_execution_enabled=True`, the unbound fallback returns `None` (fail closed), so a
   second enabled-but-not-yet-bound source can never be misrouted onto the legacy Wayond
   assignment. This preserves today's single-Wayond routing exactly while `signal_source` is unset,
   and forces each live source to be bound before two can run.

**Phase B binding requirement.** When arming the second source, the live Wayond assignment MUST be
bound (`signal_source="wayond"`) as the first step, so no unbound assignment remains. This retires
the fallback for real sources and closes both a transitional misroute (a source enabled before its
assignment is bound) and a transitional silent-disarm (a stray non-routable `wayond` binding while
Wayond is still unbound). Both were surfaced by adversarial re-review and are covered by tests
(`tests_source_scoped_routing.py::test_second_enabled_source_does_not_borrow_unbound_assignment`,
`::test_bound_wayond_not_disarmed_by_stray_nonroutable_binding`).

## Consequences

- Two (or more) auto-copy strategies can coexist, each routing to its own account/strategy.
- The Wayond path is unchanged until its assignment is deliberately bound (fallback covers it).
- A per-strategy enable/disable (marketplace `signal-copy/toggle`) can gate a source by
  flipping its bound assignment's `is_active` without affecting other sources.
- Migration `strategies/0012_strategyassignment_signal_source` is additive (new nullable field).

## Risks and controls

- **Risk:** mis-binding could route a source to the wrong account. **Control:** fail-closed on
  0/>1 matches; ambiguity never auto-executes; arming remains a separate human-gated step (RED).
- **Risk:** regressing the live Wayond demo. **Control:** back-compat fallback + explicit tests
  (`execution/tests_source_scoped_routing.py`) proving the single-unbound path still returns
  `AUTO_DEMO`/`armed`; full execution+strategies+signal_intake suite green (486 tests).
- **Boundary:** Amber (touches the execution gate). Deploying it to prod, and any arming of
  `ti_signals`, stays behind Nuno's explicit RED gate — this ADR authorises the code, not the
  prod arming.

## Evidence / validation

- `cd backend && ../.venv/bin/python manage.py test execution strategies signal_intake` →
  **501 passed** (2026-07-14), including the new `tests_source_scoped_routing`,
  `tests_signal_copy_toggle`, and `signal_intake.tests_ti_signals`.
- **Adversarial multi-agent review** (20-agent sweep) found 11 confirmed defects across the router
  gate, TI parser, toggle endpoint, and marketplace guard — ALL fixed + regression-tested. A focused
  re-verify confirmed the fixes and surfaced 2 residual unbound-fallback holes, now closed by the
  multi-source fail-closed guard + the Phase B binding requirement above.
- `manage.py makemigrations strategies --dry-run` → only pre-existing unrelated index-rename
  drift remains (deliberately not bundled). Not covered: no live/prod run (arming is Phase B).

## Reversal path

Revert the `auto_router._resolve_target` change and drop the migration (or leave the unused
column). With `signal_source` blank everywhere, behaviour is already identical to pre-change.

## Revisit trigger

If a third+ auto-copy source is added, or if per-source lot sizing / per-source accounts are
needed (today the account is chosen by the resolved assignment, not the source config).

## Approval

Code direction approved by Nuno in-session (chose "auto-trade on demo, like the live Wayond
copy"). Prod deploy + arming of `ti_signals` remain Nuno's explicit RED gate (not yet given).
