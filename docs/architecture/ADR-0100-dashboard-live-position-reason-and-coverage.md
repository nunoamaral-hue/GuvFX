# ADR-0100 — Truthful dashboard: per-account live-position exclusion reasons + equity-curve coverage provenance

Status: Accepted (repository-only; additive read-only projection + UI; NO migration; NO feature flag — default-safe)
Date: 2026-11-12
Workstream: Dashboard Integrity (GFX-WITHDRAWAL-AND-DASHBOARD-PROGRAMME-ACCELERATION-073, Lane D)
Relates to: ADR-0066 (FX point-in-time), the `analytics.portfolio` / `portfolio_fx` honesty invariants (UsdAggregate
OK/PARTIAL/UNAVAILABLE/EMPTY, null-never-zero), the equity-ledger snapshot curve.

## Context

The multi-account dashboard already refuses to fabricate: a non-USD figure with no vetted rate is excluded (never
summed as USD), an all-excluded aggregate reads `UNAVAILABLE` (never a measured `$0`), and the equity curve is built
only from durable, identity-verified snapshots (never interpolated). Two residual defects made the *presentation*
less truthful than the data underneath:

1. **Live open positions flattened every cause into one bare "stale".** `analytics.views_trade_history.
   _fetch_mt5_open_positions` returned a naked `None` for *all* failure modes — a missing agent token, an unresolved
   per-tenant endpoint, an identity-firewall refusal, a transient connection error, **and the bridge's
   `account_not_demo` refusal** (the demo-gated `/mt5/positions` returns `ok:false` for a LIVE terminal). The panel
   then showed "N accounts: live positions unavailable" and appended a transient "· reconnecting" indicator. For the
   production estate this means ~8 LIVE terminals — whose positions this read path *structurally cannot* serve — were
   presented identically to a terminal that is briefly disconnected, implying a self-heal that will never happen.

2. **The equity-curve frontend discarded the backend's coverage provenance.** `analytics.equity_snapshots.
   portfolio_equity_curve` already emits, per point, `present`/`total`/`basis`/`stale_accounts` and a top-level
   `state`. The dashboard plotted only `equity_usd`, so a portfolio line stitched from a *varying* subset of accounts
   read as a single smooth whole-portfolio trend.

(The packet's "15s refresh" does not exist in code: the summary and curve are fetched once per scope change; only the
open-trades panel polls, at 30s. No refresh-cadence change is made here.)

## Decision

Additive, read-only, default-safe. No migration, no model, no feature flag.

1. **A per-account live-position read reason.** `_fetch_mt5_open_positions` returns the position `list` on success,
   or a reason dict `{"unavailable": True, "reason": <code>}` on failure (`None` is still accepted from legacy/
   injected fetchers and treated as a transient unknown, so prior behaviour is preserved). `analytics.portfolio_fx`
   is the single source of truth that maps a reason code to a safe, non-identifying UI category and a
   **structural/transient** flag:
   - `account_not_demo → positions_unsupported_live` (STRUCTURAL — a LIVE terminal cannot serve this read path),
   - `identity_refused → read_refused`, `endpoint_unavailable`/`config_missing → endpoint_unavailable` (STRUCTURAL),
   - `bridge_unreachable`/`read_timeout_budget` → `stale_observation` (TRANSIENT),
   - **any unrecognised reason defaults to TRANSIENT** — we never over-claim a permanent cause we cannot confirm.
   `open_positions` records `unavailable_reason`/`unavailable_category`/`structural` per account; `build_summary`
   and the open-trades endpoint expose `unavailable_accounts: [{account_id, reason, reason_category, structural}]`
   plus coverage counts. The UI renders a structural exclusion as "live positions not available for live accounts"
   (never "reconnecting") and a transient one as "temporarily unavailable".

2. **Daily net-P&L coverage is explicit.** The summary names the day window (`daily_realized_pnl_window =
   "broker_server_calendar_day"`, the stored `close_time` calendar date ≈ UTC) and exposes the USD-eligible
   included/excluded account split the figure was summed over. The realized figure itself is unchanged (closed-today
   net = profit+commission+swap, deposits/withdrawals excluded, non-USD/mixed withheld as PARTIAL, never a measured
   zero). The broker-server-day vs true-UTC/member-local boundary remains a documented platform follow-up, not
   changed here.

3. **The equity-curve UI consumes the existing per-point provenance.** For the ALL scope the dashboard now detects a
   partial-coverage curve (any bucket `PARTIAL` or `present < total`) and discloses "Partial coverage · latest N of M
   accounts", so a line stitched from a varying subset is read honestly. `BUILDING` stays distinct from a real
   short/flat trend. No fabricated or forward-filled points are introduced.

## Consequences

- The dashboard tells the truth about *why* an account's live positions are excluded, and never implies a structural
  exclusion will reconnect. Partial portfolio totals and curves are disclosed, not smoothed over.
- Invariants preserved: never fabricate a figure; never substitute zero for unavailable; never combine one tenant's
  data with another (reads stay per-account, identity-firewalled); never show a structural exclusion as transient; an
  unknown reason is treated as transient (fail-safe, never over-claiming permanence).
- No schema change, no migration, no feature flag, no new network behaviour, no change to Copy Trading state or any
  execution path. The underlying operational gap (live terminals cannot serve `/mt5/positions`; per-tenant bridge
  reachability) is diagnosed and surfaced, not repaired here — terminal/bridge connectivity remains a separate
  backlog item.

## Evidence

`evidence/manifests/GFX-EVD-DASHBOARD-INTEGRITY-LANE-D.json`. Backend: `analytics` suite (269) OK incl. the new
`PositionReasonTaxonomyTests`, `OpenPositionsReasonTests`, `OpenTradesViewReasonTests`, `BuildSummaryReasonTests`,
`FetcherReasonMappingTests`. Frontend: `OpenTradesPanel.test.tsx` (24) OK incl. the ADR-0100 structural/transient
cases; `npm run build` OK; eslint clean on the changed files. Synthetic fixtures only; no PII.
