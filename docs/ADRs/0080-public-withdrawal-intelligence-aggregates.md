# ADR-0080 — Privacy-safe public broker withdrawal intelligence (aggregated evidence)

- **Status:** Accepted (Amber). Repository-only, DARK (default-OFF), SYNTHETIC-ONLY. Unmerged pending a separate
  Sponsor merge GO.
- **Decision class:** **Amber** — it adds a new PUBLIC (unauthenticated) read surface (one endpoint + one page) and a
  central **disclosure-control layer** over member-scoped withdrawal evidence, and hardens the free-text projection of
  the existing public directory/profile. It reuses the existing `broker_public` privacy engine (eligibility +
  winsorisation + k-anonymity) and changes no existing lifecycle, correlation, dedup, idempotency, owner/tenant
  isolation, evidence-immutability, erasure, or FX behaviour. **No schema change / no migration**; the foundation
  broker pages and the metric category are seeded DARK by an idempotent operator command.
- **ADR number:** 0080, chosen against BOTH `docs/ADRs/` and `docs/architecture/` (0080 is free in both; 0081 is
  reserved for Stream D), so the cross-directory ADR-uniqueness governance test passes.
- **Relates:** builds on ADR-0078 (Stream A catalogue readiness), ADR-0079 (Stream B explicit withdrawal lifecycle +
  stage timestamps), ADR-0050 (owner-attributed / account-UNRESOLVED withdrawals), and the existing `broker_public`
  cross-member `WITHDRAWAL_PROCESSING` aggregate + MULTI_ACCOUNT privacy engine. Part of
  GFX-BROKER-INTELLIGENCE-FOUNDATION-001, Stream C.

## Context
Stream B (ADR-0079) made the withdrawal lifecycle explicit (`CONFIRMATION_REQUIRED` / `CONFIRMED` / `PROCESSING` /
`COMPLETED` / terminal `REJECTED` / `CANCELLED` / `EXPIRED` / `FAILED`) with separate stage timestamps. The existing
`broker_public` app already computes a privacy-safe cross-member `WITHDRAWAL_PROCESSING` aggregate (CERTIFIED ∩
GENUINE ∩ account-resolved eligibility; per-owner winsorisation; small-bucket k-anonymity; `INSUFFICIENT_DATA` /
`PUBLISHED`). But it (a) has no reason codes (only the 2-value state), (b) measures a single `requested→completed`
duration with no per-stage breakdown, (c) maps only 7 of the 11 statuses — silently collapsing the four Stream B
states to `unresolved`, (d) emits editorial free text (`display_name` / `summary` / `website`) RAW on the public path,
(e) emits exact (`computed_at` / `window_end`) timestamps on the `PUBLISHED` projection, and (f) has no public
broker-page contract for the foundation brokers. Public withdrawal intelligence must be **aggregated evidence, not
individual testimony**: it must never expose or allow inference of any client information, and must tell the truth
(including "insufficient / unavailable") rather than fabricate a figure.

## Decision
1. **Central disclosure layer** (`broker_public/disclosure.py`): reason codes (`State` =
   PUBLISHED/PARTIAL/SUPPRESSED/UNAVAILABLE and `Reason`), a COMPLETE 11-status public lifecycle vocabulary guarded by
   an import-time completeness assertion (a future unmapped status fails the build rather than leaking as
   `unresolved`), `safe_public_text` (strip control chars → redact URLs/tokens → length-cap → HTML-escape) and
   `safe_public_url` (http/https allow-list; `javascript:`/`data:`/scheme-less fail closed), `coarsen_to_date`
   (date granularity — no sub-day precision), `stage_seconds` (the single stage-interval chokepoint, mirroring
   `Withdrawal.duration_seconds()` so an impossible/negative interval collapses to `None`, never a fabricated 0 or a
   negative), `cohort_state` (fail-closed k-anonymity gate on BOTH distinct contributors and observation count), and
   the audited `winsorise`/`suppress_small_buckets` primitives (re-exported from the existing engine, not reimplemented).
2. **Public aggregation contract** (`broker_public/withdrawal_intelligence.py`): `compute_withdrawal_intelligence`
   computes LIVE (read-only, never persisted) from the Stream B projection, reusing `_eligible_withdrawals` (CERTIFIED
   ∩ GENUINE ∩ account-resolved ∩ consenting) and per-owner winsorisation. **Permitted output:** aggregate
   counts/rates, coarsened durations and dates, methodology, limitations, broker public slug + sanitised display name.
   **Prohibited (by construction):** member identity, owner/user id, account number, withdrawal reference, message
   id/header, raw amount, exact timestamp, or unsanitised free text. Conservative eligibility: ≥ 3 distinct
   contributors, ≥ 5 observations, ≥ 5 completed observations before any speed figure; below any floor it returns an
   explicit `SUPPRESSED` / `PARTIAL` / `UNAVAILABLE` state with a reason code — never zero or fabricated data.
3. **Three SEPARATE stage durations** — request→confirmation, confirmation→completion, and total request→completion —
   each aggregated over its OWN both-anchor subset and **never combined**; a stage with too few both-anchor pairs is
   reported `UNAVAILABLE`/`PARTIAL` with no number.
4. **Lifecycle outcomes with an explicit denominator:** completion / failed / expired rates over
   `completed + rejected + cancelled + expired + failed` (stated in the payload); in-flight items are excluded from the
   denominator; `EXPIRED` and `FAILED` are kept distinct; confirmation-required incidence is a k-anonymised two-bucket
   split. `broker_reported_completed` is NEVER reported as "funds received".
5. **Suppression rules:** a cohort below the floors returns a number-free stub (no counts/window/timestamps) so
   repeated/rolling or SUPPRESSED-vs-PUBLISHED responses cannot be differenced; small-bucket k-anonymity on
   lifecycle/currency reuses `_suppress_small_buckets` gated by the existing `BROKER_PUBLIC_MINORITY_SUPPRESSION_ENABLED`;
   per-owner winsorisation is always on.
6. **Provenance & freshness:** genuine-only (`Provenance.REAL`); the observation window and freshness are coarsened to
   the date; no exact timestamp is emitted.
7. **Broker identity:** an unambiguous authoritative `source_slug` is required (else `UNAVAILABLE`/
   `AMBIGUOUS_BROKER_IDENTITY`); an uncertified broker is `UNAVAILABLE`/`NOT_CERTIFIED` (`CERTIFIED_BROKERS` is empty in
   production, so nothing publishes until a broker is certified through the governance path).
8. **Public broker-page contract** — a DARK endpoint `GET /api/public/brokers/<slug>/withdrawal-intelligence/`
   (new flag `BROKER_PUBLIC_WITHDRAWAL_INTELLIGENCE_ENABLED`, default OFF; also requires `is_published`) + a
   DARK-graceful Next.js page — for Pepperstone, Dominion Markets, Vantage and TradersWay, seeded `is_published=False`
   by the idempotent `seed_public_broker_pages` management command (NOT a migration, NOT auto-run, certifies/publishes
   nothing). The contract carries broker identity, evidence status, the speed/outcome summaries, sample sufficiency,
   methodology, limitations, and truthful `Insufficient`/`Unavailable` states.
9. **Indexing prevention:** a `robots.ts` disallows the `/brokers` tree (and `/api/`) from indexing while the surface
   is DARK (a DARK page shell still returns HTTP 200); the authoritative gate remains the backend 404.
10. **Free-text hardening of the EXISTING surface:** `_broker_json` now routes `display_name`/`summary` through
    `safe_public_text` and `website` through `safe_public_url` — behaviour-preserving for clean data, closing the raw
    free-text / injected-markup gap on the directory and profile endpoints too.
11. **DARK/default-OFF release & deployment controls:** every new flag defaults OFF; no certification, publication,
    deployment, service restart, persistence, or production/dev-DB mutation is performed by this packet.

### Deviations / decisions flagged (Amber)
- **Live compute-on-read** for the new endpoint (vs the stored-`BrokerMetricAggregate` pattern of
  `WITHDRAWAL_PROCESSING`): chosen to avoid any write path or recompute scheduler and keep the surface strictly
  read-only and always-fresh; it is throttled (`public` scope, 60/min) and acceptable while DARK/unpopulated.
- **NOT changed (recommended follow-ups):** the EXISTING `WITHDRAWAL_PROCESSING` `_aggregate_json` still emits exact
  `last_update`/`window_end` timestamps — left unchanged to preserve its behaviour/tests; coarsening it is a
  behaviour-affecting follow-up. Arming `BROKER_PUBLIC_MINORITY_SUPPRESSION_ENABLED` by default is a PM/Sponsor (Amber)
  flag decision and is **not** flipped here.

## Consequences
- A single, reviewable disclosure-control layer centralises the privacy decisions; the public contract is reason-coded
  and fail-closed; the four foundation broker pages exist in the repository, DARK.
- The surface is deliberately **UNAVAILABLE** until a broker is certified via governance (RED) and published
  (editorial) — an armed-but-uncertified broker truthfully reports `NOT_CERTIFIED`, never member data.
- No automated recompute/caching exists (live compute); acceptable while DARK, a future optimisation if armed at scale.
- Streams C and D stay separate; Stream D (MT5 broker-quality telemetry + Wayond export) is ADR-0081 and is not begun.
