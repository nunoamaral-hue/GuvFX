# ADR-0109 — Dashboard full coverage: truthful equity curve + live-positions reason & fan-out

- **Status:** Accepted (repository-only; additive; migration-free; no execution/Copy-Trading authority; no credential
  handling; no manual snapshot; no production/host mutation by this packet).
- **Packet:** GFX-DASHBOARD-EQUITY-POSITIONS-FULL-COVERAGE-133.
- **Date:** 2026-11-15.
- **Builds on (never replaces):** ADR-0100 (per-account live-positions reason codes + structural/transient taxonomy
  in `portfolio_fx`), ADR-0103/0105/0106 monitoring-enrolment projection (`evaluate_enrolment_for_accounts`), ADR-0058
  (Fed H.10 / BoE `DailyFxObservation` store + governed `select_observation` selection), and the existing
  `portfolio.open_positions` / `equity_snapshots.portfolio_equity_curve` aggregation. The owner-scoped account universe
  (`portfolio.resolve_scope` / `owned_accounts`: owner + non-tombstoned only) is **unchanged** — both the equity and
  positions paths already resolve the identical set; this ADR does not add, remove, or allow-list any account.

## Context — the defect

The authenticated production dashboard (post-`b97d1f56`, 12 owner accounts) showed the equity curve at **"Partial
coverage · latest 9 of 12 accounts"** and Open Trades at **"6 accounts: live positions temporarily unavailable"**.
Read-only production + bridge diagnosis (owner 29, no mutation) established:

- **Equity 3 absent = #49, #52, #56 — all GBP.** `PortfolioEquityCurveView` converts the ALL-scope curve with
  `FX.NullFxRateSource()` (USD-only — a deliberate guard against applying one of *today's* spot rates to *historical*
  points). The consequence was that **every non-USD account's `to_usd` failed and the account was dropped from every
  bucket**, permanently excluding 25% of the fleet from the USD curve (`present=9/12`, `stale_accounts=[49,52,56]`).
- **Positions 6 unavailable = #48, #53, #54, #56, #57, #59.** Their live `/mt5/positions` read returns **HTTP 400 with
  body `{"ok":false,"error":"account_not_demo"}`** (the bridge demo-gate on a LIVE terminal). `_fetch_mt5_open_positions`
  read positions with `urllib.request.urlopen`, which **raises `HTTPError` on a 4xx**; the generic `except` mapped that
  to `bridge_unreachable` (a **transient** "reconnecting") and **never read the 4xx body**, so the structured
  `{ok:false,error:…}` parse (which ran only for a 2xx) never saw the real reason. A **structural** exclusion was
  therefore mislabelled transient — the exact "temporarily unavailable" wording the dashboard showed. (The demo-gate is
  also *inconsistent* across terminals — 6 LIVE terminals serve positions and 6 do not; that host/bridge inconsistency
  is **operational**, reported truthfully, and is not changed by this repository PR, which makes the dashboard *honest*
  about each account's actual bridge response.)
- **Latent head-of-line blocking.** `open_positions` read accounts **serially** under one shared ~10s wall-clock
  budget; once early slow accounts exhausted it, later accounts were marked `read_timeout_budget` **without being read**.
  In the observed case all reads were fast (2.0s total), so this did not cause the current 6/12 — but a single hung
  bridge *would* starve the fleet, and coverage was implicitly capped at a small fixed fleet.

Both paths resolve the **same** account universe; the gap is not eligibility. It is (a) FX conversion on the equity
curve and (b) reason classification + read concurrency on the positions path. All 12 accounts are enrolment `READY`,
`monitored`, with ~1-minute-fresh `account_read` equity snapshots and allocated per-tenant endpoints.

## Decision

1. **Live-positions reason truthfulness (the core positions fix).** `_fetch_mt5_open_positions` now catches
   `urllib.error.HTTPError` separately on the `/mt5/positions` read, **reads the 4xx body**, and classifies it exactly
   as the 2xx `{ok:false}` path: `account_not_demo` → the **structural** `positions_unsupported_live`; any other refusal
   → the fixed **transient** `positions_refused`. The bridge-controlled `error` string is never echoed verbatim (no
   identifier leak, no minting of a structural key). Only a connection-level failure (`URLError`/timeout) remains the
   transient `bridge_unreachable`. An unparseable 4xx body is the safe transient `positions_refused`, **never** a
   successful/"reachable" read. The whole downstream taxonomy (`portfolio_fx` category + structural flag, the API's
   `unavailable_accounts`, and the frontend's existing structural/transient split) was already correct and is unchanged
   — the single defect was that the demo-gate reason never reached it.

2. **Equity curve point-in-time coverage.** `portfolio_equity_curve` gains an optional `rate_for_date(date) →
   FxRateSource`. When supplied it converts each bucket with the rate **effective on that bucket's date**;
   `PortfolioEquityCurveView` supplies it, gated by `fx_conversion_enabled_for(user)`, via the new
   `fx_source.historical_rate_for_date` — which loads every accepted observation in **one bulk query** (N+1-safe) and
   selects per bucket date with the **same** governed rules as `select_observation` (previous-day target, carry-forward
   to the most recent accepted earlier date, 7-day staleness, source priority, latest accepted revision). A non-USD
   (e.g. GBP) account thus **joins** the USD curve at its historically-correct rate instead of being structurally
   excluded. When FX is not enabled for the caller the curve stays on `NullFxRateSource` (byte-identical to today). A
   single *today* spot rate is **never** applied to history either way (data.md point-in-time).

3. **Independent, bounded positions reads (no head-of-line blocking).** `open_positions` fans the per-account live reads
   out **concurrently** over a bounded worker pool (`_OPEN_POSITIONS_MAX_WORKERS`). Each read keeps its **own** per-read
   socket timeout (never a shared depleting budget), so one slow/hung account can no longer starve the accounts after
   it; the overall collection is still bounded by `deadline`, and an account not collected in time is named
   `read_timeout_budget` (transient), never dropped. Results are assembled in **account order** for a stable row list,
   a distinct sentinel separates "not collected" from a fetcher that returned `None`, and each worker closes its own DB
   connection. Coverage scales to an arbitrary fleet, not a fixed 12.

## Non-goals / deliberate boundaries

- **No account allow-list, no `is_active`/`is_demo`/Copy-Trading gate on monitoring.** `is_active` remains an
  *execution* gate only; monitoring enrolment is unchanged. Copy-Trading participation grants no dashboard authority and
  leaks no tenant data (positions are never fabricated or borrowed from a snapshot or another tenant).
- **The bridge demo-gate itself is not bypassed.** Where a LIVE terminal structurally refuses `/mt5/positions`, the
  dashboard reports it honestly as structural; lifting that gate (or reconciling the per-terminal inconsistency) is an
  **operational**, separately-gated change, not part of this repository PR.
- **No production deploy, migration, host/MT5 mutation, manual snapshot, broker/credential action** by this packet.

## Consequences

- The equity curve covers every monitoring-eligible account when FX is enabled for the caller (GBP accounts join at
  their point-in-time rate); non-convertible points stay honestly PARTIAL, never fabricated.
- A demo-gated LIVE account reads as **structurally** unavailable ("not available for live accounts"), not a misleading
  "reconnecting"; the per-account reason/category/structural data the API already exposes is now correct for 4xx refusals.
- A single hung bridge can no longer suppress healthy accounts; positions coverage reflects what is actually readable,
  bounded in time, and scales with the fleet.
- Newly added / recovered accounts enter both paths automatically (no packet, no hard-coded id): equity once a fresh
  snapshot exists, positions once a live read succeeds — exactly as before, now without the FX and reason-classification
  gaps that hid them.
