# ADR-0058 — Portfolio GBP→USD conversion (Fed H.10 DEXUSUK, append-only, DARK)

- **Status:** Proposed (Amber). DARK — flag default OFF; the ingestion job is UNSCHEDULED in production and no
  external provider is contacted from production until a separate governed GO.
- **Decision class:** **Amber** — it changes *what the dashboard sums* (a GBP account's equity/floating-P&L
  would enter the USD portfolio total instead of being honestly excluded). Requires this recorded decision
  before any enablement.
- **ENABLEMENT PRECONDITION (F-8):** FX enablement is **BLOCKED until the runtime-role cutover is complete** (the
  web runtime connects as a SELECT-only non-owner identity). DB triggers make the store append-only against
  every ordinary role, but while the application still connects as a superuser/owner identity that identity can
  TRUNCATE or bypass triggers; meaningful database immutability is therefore not claimed until F-8 (or an
  equivalent authoritative boundary) is proven. See `ops/fx/grants_fx_observation.sql`.
- **Supersedes/relates:** extends `analytics.portfolio_fx` (the `NullFxRateSource` honest-PARTIAL behaviour;
  see [Portfolio honest reporting]/#550 and the R2 bounded-read fix #556). Does not change Copy Trader,
  Withdrawal Intelligence, MT5 bridges, workers or schedulers.

## Context
Every production account is effectively USD except Pepperstone (GBP, account 49). With no FX source, the USD
portfolio aggregate honestly **excludes** the GBP account and reports **PARTIAL** (#550). The Sponsor has
authorised wiring an authoritative daily rate so the GBP account can contribute to the USD total — without any
synchronous external dependency, credentials, fabricated value, or silent mixed-currency sum.

## Decision

### Why portfolio totals change
When the flag is ON and a valid rate exists, a GBP account's **current** equity and floating P&L are converted
to USD and included in the portfolio total; `equity_usd`/`balance_usd`/`open_pl_usd` therefore move from PARTIAL
(GBP excluded) toward OK (GBP included). Realized `metrics`/`daily_realized_pnl` follow the same conversion path.
When the flag is OFF (default) behaviour is **byte-identical** to today.

### Source and units
- **Primary source:** US Federal Reserve **H.10**, series **`DEXUSUK`**, obtained through the **keyless FRED CSV
  download** (`https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXUSUK`). No API key, no credentials.
- **Units / direction:** **USD per 1 GBP** (`DEXUSUK` is "U.S. dollars to one U.K. pound"), the exact
  orientation `to_usd` needs (GBP amount × rate = USD). The source's **official daily published rate** — *not*
  described or used as an intraday or volume-weighted average.
- **Secondary source:** Bank of England is permitted for **monitoring / discrepancy alerts only**. A BoE rate is
  **never** auto-substituted into a Fed-based total; switching the portfolio source is a separate governed
  decision.

### Selection rule (governed timezone + prior-day + carry-forward + staleness)
1. Determine the current calendar date in the **governed timezone `Europe/London`** (DST-correct; overridable via
   `PORTFOLIO_FX_BUSINESS_TZ` for a future platform-wide business-date policy).
2. `target_date = current_date − 1 calendar day`.
3. Select the latest **ACCEPTED** Fed H.10 observation with `effective_date <= target_date` — so an observation
   dated **today or in the future is never selected**; carry **forward** the most recent earlier accepted
   observation across weekends/holidays/gaps.
4. **Refuse** (no conversion) when the selected observation is **more than 7 calendar days older than the
   `target_date`**.
5. When refused, keep today's honest **PARTIAL/UNAVAILABLE** behaviour. Never interpolate, fabricate, or
   substitute zero.

### Revision handling (append-only)
`DailyFxObservation` is **append-only and immutable**. A source may revise a published rate; each revision is a
**new row** (`revision_number` increments per `(source, base, quote, effective_date)`), never an in-place edit.
- **Idempotency vs revision:** `observation_checksum` = sha256 of the canonical tuple
  `source|series|base|quote|effective_date|exact-rate`. An identical published value re-ingests as a **no-op**
  (unique constraint rejects the duplicate). A genuinely revised value has a different checksum ⇒ a new revision.
- **Selection:** the effective observation for a date is the **latest ACCEPTED revision**; every prior revision
  is retained; REJECTED revisions are kept for audit but never selected. The selected `observation_id` +
  `revision_number` are surfaced in provenance/audit.
- **Runtime immutability (DB-enforced, F1):** migration `0003` installs PostgreSQL **BEFORE UPDATE and BEFORE
  DELETE row triggers** that reject every UPDATE/DELETE for all ordinary roles (including the owner) — so
  `QuerySet.update()`/`delete()`, `save(force_update=True)` and raw SQL all fail, not merely the app-layer
  `save()`/`delete()` guards. A correction is INSERT-only. **TRUNCATE** is not trigger-blocked (a `BEFORE
  TRUNCATE` trigger would break Django's `TransactionTestCase` flush, which issues a multi-table TRUNCATE);
  TRUNCATE is instead owner-only in PostgreSQL, and under F-8 the SELECT-only runtime role (a non-owner without
  the TRUNCATE privilege) cannot TRUNCATE/UPDATE/DELETE/INSERT — proven by a role-based negative-control test and
  `ops/fx/grants_fx_observation.sql`. The authoritative immutability boundary is this trigger layer **plus** the
  F-8 grant model; see the enablement precondition above.
- **DB-level constraints (F4):** `CheckConstraint`s enforce supported pair (GBP/USD), `base != quote`, positive
  rate, `revision_number >= 1`, valid `source`/`acceptance_state`, and a 64-hex `observation_checksum` — at the
  database, not merely in application code.
- **Reproducibility (F3):** each row stores `source_url` and the exact `raw_line` (e.g. `2026-01-05,1.2710`) in
  addition to effective date / series / retrieved_at / exact rate — enough to reconstruct and verify the parsed
  observation later, never a checksum alone.

### Rounding
Displayed portfolio money is quantised to **2 dp with Decimal `ROUND_HALF_EVEN`** (`portfolio_fx._money_half_even`).
The **source rate is stored and used UNROUNDED** at full published precision (`DecimalField(20,10)`) for
calculation and audit. (For USD-only totals the result is identical to the prior rounding — flag-OFF unchanged.)

### Availability behaviour
- No usable rate (empty store / all stale / unsupported pair) ⇒ `get_rate` returns `None` ⇒ the account is
  honestly excluded (PARTIAL/UNAVAILABLE), never a measured 0.
- Point-in-time correctness: the **historical equity curve is NOT converted** with a single spot rate — it stays
  on the USD-only source regardless of the flag (a non-USD account's history remains honestly excluded).

### Disclosure wording (UI)
Under the Equity (USD) card, when conversion is ON: `GBP→USD <rate> · <effective_date> · Fed H.10` (tooltip adds
"(USD per GBP) · rate <date> · Fed H.10 DEXUSUK"); when ON but no valid rate: `GBP→USD rate unavailable`. Absent
entirely when OFF.

### Immutability boundary — precise statement (F11)
- The table is **NOT absolutely immutable against its owner.** The owner/migration identity (and a superuser) can
  ultimately bypass or change the protections — disable a trigger, `SET session_replication_role='replica'`, or
  TRUNCATE (TRUNCATE is owner-only).
- The **ordinary web runtime and the FX ingestion writer cannot TRUNCATE** (nor UPDATE/DELETE): they are
  non-owner roles with, respectively, SELECT-only and SELECT+INSERT-only privileges, proven on PostgreSQL 15.
- **FX enablement remains BLOCKED until F-8** separates the application runtime identity from table ownership
  (SELECT-only runtime). Until then the app connects as owner/superuser and the above owner-bypass applies.
- The trusted migration/owner-identity residual risk is **the same governed boundary already accepted for the
  protected evidence stores** (e.g. the erasure audit carve-out / RuntimeEvent immutability) — not a new or
  weaker position.

## Threat model
| Threat | Mitigation |
|---|---|
| **Source outage** | Request path is **DB-only** (never calls FRED). An outage only stops the *daily job*; the store serves the last valid rate until it exceeds the 7-day staleness limit, then conversion refuses honestly. |
| **Malformed data** | Pure parser validates header + **series identity** (wrong-series ⇒ hard refuse) and rejects nonnumeric/zero/negative/implausible values (plausibility band 0.80–3.50 USD/GBP). Rejected values are counted, never written as accepted. |
| **Revised data** | Stored as a new immutable revision; selection uses the latest ACCEPTED revision; priors retained. |
| **Compromised / wrong-direction payload** | Series-id + direction (`USD_PER_GBP`) + plausibility band reject a decimal-shift or an inverted (GBP-per-USD ≈ 0.7) value; raw payload hashed; **no cross-source substitution**. |
| **Duplicate ingestion** | `observation_checksum` unique constraint makes re-ingest a no-op (idempotent). |
| **Oversized / slow response** | Fetch job imposes a response-size ceiling (5 MiB) and request-time ceiling (15 s); HTTPS enforced. |
| **Secret/PII leakage** | No credentials anywhere; bounded alerts log counts + a checksum prefix, never the payload body. |
| **Stale silent conversion** | 7-day staleness refusal → honest unavailable rather than a stale number. |
| **Accidental enablement** | Flag default OFF; job unscheduled; enabling + scheduling + first production fetch each require a separate governed GO. |

## Rollback and flag behaviour
- **Flag OFF (default):** `NullFxRateSource` — byte-identical to today; payload carries no `fx` key; no disclosure.
- **Rollback:** set `PORTFOLIO_FX_CONVERSION_ENABLED` unset/OFF (instant, no deploy) — the dashboard reverts to
  honest-PARTIAL. Code rollback = redeploy the prior image. The append-only store is inert while the flag is OFF.

## Consequences
- GBP equity can contribute to the USD total with disclosed, auditable provenance; honesty is preserved when no
  rate is available. A new append-only table + an out-of-band ingestion job are added. Point-in-time historical
  conversion and automated source-switching are explicitly **out of scope** (future governed decisions).
