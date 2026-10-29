# ADR-0064 — Copy Trading UI truthfulness: flag-derived execution status + overview sizing readiness

- **Status:** Accepted (repository-only; DARK). No production deploy; execution OFF.
- **Date:** 2026-10-29
- **Workstream:** Copy Trading (UI acceptance)
- **Supersedes / relates to:** ADR-0051 (copier observe/shadow + PR C read-only UI), ADR-0062 (reviewed
  mapping controls), the per-follower sizing validation in `trade_copier/config_api.py`.

## Context

The read-only Copy Trading UI (PR C) must let a founder/operator read readiness **honestly** — it can never
present the subsystem as more ready than it is. An independent audit of the read APIs + components found the
config and instrument-matrix surfaces genuinely careful (verification tied strictly to a verified
`CopierSymbolMap`; honest catalogue-coverage denominators; per-follower sizing validated against each
follower's OWN broker contract and fail-closed with precise reasons), but surfaced **two** places that could
mislead:

1. **Execution-OFF status was a hardcoded constant.** `views._SHADOW_BANNER`, the `"execution": "off"`
   literals, and the `_EXECUTION_STATUS` dicts in `config_api` / `matrix_api` asserted "EXECUTION OFF" /
   "Observation mode" on **every** response regardless of the live execute flags
   (`copier_execute_enabled()` / `copier_execute_demo_armed()`), which the read APIs never consulted. It was
   accurate only because all flags are OFF today; the instant a Sponsor armed the DEMO producer the UI would
   keep asserting "EXECUTION OFF" while real DEMO orders were being produced — directly contradicted by the
   replication table.

2. **The monitoring overview showed follower sizing with no validity signal.** The overview
   (`CopyTradingOverviewView` → `CopierFollowerSerializer`) rendered e.g. "Sizing: Fixed 0.01 · Enabled" with
   no indication that 0.01 is below the broker's `volume_min` (the Pepperstone `XAUUSD_SB` case, vmin 1.0) or
   that constraints were unsynced. The fail-closed sizing-readiness verdict lived only on the separate config
   page, so the dashboard could read as set-up-and-ready when the configured lot was unexecutable.

## Decision

Make both surfaces derive their truth from live state, additively and behaviour-preservingly.

1. **Flag-derived execution status/banner** — `trade_copier/flags.py` gains `execution_status(*, note)` and
   `execution_banner()`, DERIVED LIVE from `copier_execute_enabled()` / `copier_execute_demo_armed()`:
   - producer OFF (default) ⇒ `mode=observation`, `execution=off`, label "Observation mode", banner
     "SHADOW — EXECUTION OFF" — **byte-identical to the previous constants**, so nothing changes today;
   - producer ON but not armed ⇒ `execution=shadow_jobs`, banner "SHADOW JOBS — NO ORDER SENT";
   - DEMO armed ⇒ `execution=demo_armed`, banner "DEMO EXECUTION ARMED — DEMO ONLY".
   `views.py`, `config_api.py` and `matrix_api.py` call these helpers instead of the constants.
   `live_follower_execution` is always reported `blocked` (LIVE is never reachable from the copier; the
   execution LIVE wall governs it).

2. **Overview carries the same fail-closed per-follower readiness as the config page.** `config_api` factors
   `follower_config_readiness(master, follower)` (verified map + follower's OWN observed `BrokerInstrument`
   constraints + configured-lot validation), reused by both `_pair_view` (unchanged output) and
   `CopyTradingOverviewView`, which now adds `config_status` / `config_blocked_reason` / `follower_current`
   to each overview follower. The monitoring component renders a "⚠ Not execution-ready — &lt;reason&gt;"
   marker (keeping the backend's precise `sizing_invalid: …` tail) when a follower is blocked.

The UI only ever asserts the **NEGATIVE** ("not execution-ready — &lt;reason&gt;"); it never renders a
positive "execution-ready" claim (a ready *config* is still observation-only until a separate explicit
production authorisation + the LIVE safety gate).

## Scope / non-goals

Read-only, additive, DARK. No new model/field/migration. No flag, worker, kill switch, credential, bridge,
port or schedule is touched. No execution path is imported or reachable. The capability is repository-only and
is **not deployed** by this ADR; production remains unchanged and execution OFF.

## Consequences

- The UI can no longer assert a stale "EXECUTION OFF" once the producer is armed — the banner/status track the
  real flags (regression-guarded by an adversarial test that arms DEMO in settings and asserts the banner is
  not "EXECUTION OFF").
- The monitoring dashboard shows a follower whose configured lot its broker cannot execute as not
  execution-ready, with the precise reason — the same truth the config page tells.
- OFF-state output is unchanged, so every existing API contract/test stays green.
