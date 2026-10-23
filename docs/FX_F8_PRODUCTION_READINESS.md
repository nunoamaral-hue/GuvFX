# FX GBP→USD — F-8 Production Readiness Packet (ADR-0058)

**Status: DARK. No production mutation performed. Read-only inspection + isolated PG15 rehearsal + design only.**
Canonical main `4b98e4e637f6b376f90c172a1e4be7a8015275ac` (PR #560 squash, post-merge CI 5/5). Prepared 2026-10-23.

## Phase 1 — Current canonical + production state (read-only evidence)
| Item | Finding |
|---|---|
| main SHA / #560 | `4b98e4e6…` is the #560 squash on main; post-merge CI **5/5 success** |
| `PORTFOLIO_FX_CONVERSION_ENABLED` | defined in `analytics/fx_source.py` (settings→env→`""`), **default OFF**; call sites `analytics/views_portfolio.py:44` (`_rate_source`) + `:87` (provenance) — 2 sites |
| analytics migrations applied in prod | **`0001_initial` only** — `0002`/`0003` NOT applied; `analytics_dailyfxobservation` does **not exist** in prod |
| prod DB runtime identity | `current_user = session_user = guvfx`; DB `guvfx` owner = `guvfx`; `analytics_*` tables owned by `guvfx` |
| prod roles | **only `guvfx`** (+ built-in `pg_*`); `guvfx` is **SUPERUSER**, CREATEROLE, CREATEDB, LOGIN |
| F-8 separation in prod | **NOT present** — single `guvfx` superuser/owner identity (verified, not inferred) |
| scheduler | **host crontab** → `docker compose exec -T guvfx-backend python manage.py <cmd>` (per-min + `*/5`); **no FX task**; only systemd timer is `guvfx-broker-ingest` (WI, separate) |
| deployed images | backend `sha256:3decfc8a` / frontend `sha256:53108f5f` — the **R2/#556** images; **#560 FX code is not deployed** |
| dashboard health / R2 behavior | ALL settles ~6.7s, `equity_usd.state=PARTIAL` total $200 (48 USD included; 49 GBP excluded; 46 stale); **no `fx` key** — the accepted R2 honest-partial, unchanged |

## Phase 2 — F-8 current-vs-required matrix
| Control (required by ADR-0058) | Current production | Gap |
|---|---|---|
| Web runtime = SELECT-only on FX table | `guvfx` superuser+owner (all privileges) | **full** |
| Ingestion writer = SELECT+INSERT, distinct identity | same `guvfx` (no distinct identity) | **full** |
| Neither identity owns the table | `guvfx` owns DB + all tables | **full** |
| Runtime/ingestion denied UPDATE/DELETE/TRUNCATE | `guvfx` superuser can do all + bypass triggers | **full** |
| Migration/owner identity separately governed | `guvfx` is also migration/owner (same role) | **full** |
| Runtime cannot assume ingestion/owner | single identity — N/A until separated | **full** |
| Distinct credential custody/injection | one `guvfx` credential | **full** |

**→ F-8 is entirely unbuilt in production: one `guvfx` superuser does everything. FX enablement is correctly BLOCKED on it.**

### Interaction with PR #554 (F-8 runtime-role separation, merged DARK, not applied)
#554 provides the production cutover (`ops/erasure/separate_runtime_role.sql`, `demote_app_role.sql`, `cutover.sh`):
`guvfx_schema_owner` (NOLOGIN, owns objects), `guvfx_migrate` (LOGIN NOINHERIT, acts-as owner for migrations),
`guvfx_app` (LOGIN runtime, owns nothing, per-table grants, **no DELETE/TRUNCATE**). **FX reuses this framework** — it
does **not** need a parallel architecture. Mapping:
- The FX table is owned by `guvfx_schema_owner` (created by `guvfx_migrate` acting-as owner). Triggers are owner-created;
  only the governed NOLOGIN owner (reachable via `guvfx_migrate`'s explicit `SET role`) can disable them.
- `guvfx_app` (web runtime): the FX grants **override** #554's default to **SELECT-only** on the FX table (REVOKE
  INSERT/UPDATE for this table; it is read-only on the dashboard path).
- **DESIGN REQUIREMENT / GAP:** the cron FX ingestion (`fetch_fx_daily`) currently runs through the backend's default
  DB connection = the **same** role as the web runtime (`guvfx_app`). A *distinct* SELECT+INSERT ingestion writer
  (`guvfx_fx_ingest`) therefore requires a **dedicated DB connection alias** for `fetch_fx_daily` (mirroring the
  existing `ERASURE_EXECUTOR` alias pattern). This alias is **not** in merged #560 — it is a small additive DARK
  follow-up required before the web runtime can be SELECT-only AND ingestion can still write. Do **not** conflate FX
  ingestion grants with the erasure/protected-evidence permissions (#554 governs the erasure executor separately).

## Phase 3 — Isolated PostgreSQL 15 rehearsal (synthetic data only)
Disposable `postgres:15` containers (server **15.19**, production major). Evidence:
- **Migrations:** forward `0001→0002→0003` OK; reverse-to-0002 (drops triggers, **rows preserved**); re-forward-to-0003
  restores triggers; `makemigrations --check` = no drift.
- **Constraints installed:** `fx_obs_base_ne_quote`, `fx_obs_supported_pair`, `fx_obs_rate_positive`,
  `fx_obs_revision_positive`, `fx_obs_source_valid`, `fx_obs_state_valid`, `fx_obs_checksum_format` (+ the
  `revision_number >= 0` field check).
- **Triggers installed + enforce:** `fx_obs_no_update`, `fx_obs_no_delete` reject UPDATE/DELETE; INSERT (new revision)
  allowed.
- **Roles / ownership (production-equivalent):** table owner `guvfx_schema_owner` (NOLOGIN). Privilege matrix —
  `guvfx_app` = **SELECT**; `guvfx_fx_ingest` = **INSERT+SELECT**.
- **Positive/negative privilege:** runtime SELECT ok; runtime INSERT/UPDATE/DELETE/TRUNCATE → *permission denied*;
  ingest SELECT/INSERT ok; ingest UPDATE/DELETE/TRUNCATE → *permission denied*.
- **Escalation resistance:** neither non-owner role can `DISABLE TRIGGER` / `DROP TRIGGER` / `ALTER … OWNER TO` (all
  *"must be owner of table"*) nor `SET ROLE guvfx_schema_owner` (not a member).
- **Append-only + idempotency + concurrency:** real two-thread + barrier tests — identical first ingestions → one row;
  two corrections → distinct sequential revisions; dup racing a correction → 2 rows; rollback releases the advisory
  lock, leaves no partial row; no unhandled IntegrityError, no duplicate revision, no lost correction.
- **Flag OFF** → byte-identical, **zero FX-store queries**, no `fx` key. **Flag ON (fixture only, no provider call)** →
  provenance surfaced; valid previous-day / weekend-holiday fallback / stale-refuse / missing / correction / mixed
  GBP+USD / one-account-unavailable all render **honest partial** — never a fabricated zero or incorrect total.
- The role/grant test **executes (not skips)** on PG15 as superuser; it skips only on a dev DB user lacking CREATE ROLE.

## Phase 4 — Provider + scheduler design review
- **Parser:** FRED `DEXUSUK` validated against the committed fixture; series-identity + wrong-series refusal; unit
  direction **USD per GBP** asserted; `.`/blank = non-publication day (absent, not rejected); reject
  nonnumeric/zero/negative/implausible (0.80–3.50).
- **Target date:** governed **Europe/London** (DST-correct), `target = today−1`, never today/future.
- **Schedule (proposed, NOT created):** one daily `fetch_fx_daily` crontab line mirroring the equity-snapshots pattern,
  run **after the Fed H.10 publication** (~mid/late-afternoon US Eastern; a ~22:00–23:00 Europe/London window is safe
  and well clear of the next London business day). HTTPS-only; 5 MiB / 15 s ceilings.
- **Retry/backoff + provider-unavailable:** on fetch/parse failure the job leaves the store unchanged and emits a
  bounded alert (no payload body); the next scheduled run retries. Idempotent, so a missed day is covered by the next
  run's full-history backfill + carry-forward; no bounded in-job retry loop needed (and none hammers the provider).
- **Idempotency:** per-observation checksum + advisory-lock serialisation ⇒ re-ingest/overlap cannot create duplicate
  logical observations; a revised value is a new revision.
- **Monitoring signals (to wire at enablement):** missing previous-day publication; fallback effective-date in use;
  approaching 7-day staleness; stale/refused conversion; ingestion failure; unexpected revision; scheduler non-run —
  all derivable from `fx_ingestion_health` + the job summary JSON.
- **BoE:** monitoring / cross-check only; never an automatic unreviewed substitute (source-switch = separate governed
  decision).

## Phase 5 — Proposed staged production rollout + rollback matrix (NOT executed; STOP/GO before every mutation)
| # | Stage (proposed action) | Expected | Verify | Stop condition | Rollback | Rollback class |
|---|---|---|---|---|---|---|
| 0 | Confirm a fresh **certified recovery point** (DB backup) | recovery point verified | restore-cert evidence | no fresh cert | n/a (precondition) | — |
| 1 | Record baseline (images, schema, roles, grants, flag, dashboard health) | baseline captured | diff vs this packet | drift from baseline | n/a | — |
| 2 | **F-8 cutover** (PR #554 `cutover.sh`): create `guvfx_schema_owner`/`guvfx_migrate`/`guvfx_app` + distinct creds; transfer ownership; demote `guvfx` | app runs as `guvfx_app` | privilege matrix; app health 200 | any role/owner/priv mismatch; app cannot start | #554 `demote_app_role_rollback.sql` / restore creds | credential/grant + ownership |
| 3 | Add **FX ingestion DB alias** + role `guvfx_fx_ingest` (SELECT+INSERT); deploy the DARK follow-up code that routes `fetch_fx_daily` through it | alias present; web runtime SELECT-only | grants matrix; `fetch_fx_daily --dry-run` | alias/role missing | revert code (flag OFF) + drop role | app + credential/grant |
| 4 | Apply analytics **migrations 0002 + 0003** (as `guvfx_migrate`) | table + constraints + triggers | `\d`, `pg_trigger`, `pg_constraint` | migrate fails | `migrate analytics 0001` (destructive — only post-export) | schema |
| 5 | Apply + independently verify **grants** (`ops/fx/grants_fx_observation.sql`) | runtime SELECT; ingest SELECT+INSERT; both deny U/D/T | privilege matrix query | any extra privilege | re-REVOKE | credential/grant |
| 6 | **Deploy code with flag OFF** (backend+frontend, `--no-deps`, no `--remove-orphans`) | dashboard byte-identical; no `fx` key | chunk fingerprint; dashboard probe | any dashboard change | retag prior image + recreate | application-only |
| 7 | Smoke tests, **no FX conversion active** | R2 behavior intact | ALL/per-account probe | regression | (as #6) | application-only |
| 8 | **One governed ingestion/backfill** (`fetch_fx_daily`, flag OFF) | observations stored | source/target/effective/revision + health HEALTHY | fetch/parse error; wrong direction | data is append-only; a bad revision is superseded by a new accepted revision (never deleted) | **data-affecting** |
| 9 | **Enable the flag** for a bounded canary | `fx` provenance; GBP converts w/ disclosure | account 49 + All Accounts visual | wrong rate/date; silent combine | **unset flag (instant, app-only)** | application-only |
| 10 | **Enable the daily schedule** | daily refresh | `fx_ingestion_health` daily | overlap/dupe | remove crontab line | application-only |
| 11 | **Soak** (defined period) | stable, honest | monitoring signals | any adverse signal | unset flag / remove schedule | application-only |

Key rollback property: **everything up to Stage 8 is reversible without data loss** (flag OFF is instant/app-only;
schema reverse preserves rows). Once Stage 8 writes the first observation, rows are append-only — a wrong value is
corrected by a **new accepted revision**, never by deletion.

## Phase 6 — Adversarial review (findings + dispositions)
| # | Concern | Disposition |
|---|---|---|
| A1 | Owner/superuser bypass of triggers | **Documented, accepted, F-8-gated.** Owner (NOLOGIN) reachable only via governed `guvfx_migrate` SET role; same boundary already accepted for protected-evidence stores. Not claimed as absolute immutability. |
| A2 | Runtime/ingestion privilege escalation | **Resolved/proven** on PG15 — cannot disable/drop triggers, change ownership, or assume owner. |
| A3 | **Distinct ingestion writer not in merged code** | **Unresolved (code follow-up).** `fetch_fx_daily` uses the web-runtime connection; a dedicated `fx_ingest` DB alias is required for true SELECT-only runtime (Stage 3). Flagged; do not resolve by weakening the runtime to INSERT. |
| A4 | Wrong GBP/USD direction / decimal shift | Resolved — `DEXUSUK`=USD per GBP asserted; plausibility band rejects an inverted/shifted value. |
| A5 | Date-boundary / DST / weekend-holiday / stale | Resolved — Europe/London target−1, carry-forward, 7-day-from-target refusal; all tested incl. DST. |
| A6 | Duplicate/conflicting revisions, scheduler overlap | Resolved — advisory-lock serialisation + per-observation checksum + idempotent command. |
| A7 | Partial total presented as complete | Resolved — PARTIAL/UNAVAILABLE honest; never a fabricated zero; disclosure in UI + provenance. |
| A8 | Provider failure / secret leakage | Resolved — DB-only request path; fetch job HTTPS-only, bounded alerts (no payload), **no credentials** (keyless CSV). |
| A9 | Rollback after first observation exists | Documented — append-only; correction-by-new-revision; schema reverse preserves rows (export before any table drop). |
| A10 | Interaction with live R2 dashboard | Resolved — flag OFF byte-identical + zero FX queries; curve stays point-in-time USD-only. |

## Percentages
- FX engineering: **100%** (merged, hardened F1–F12, rehearsed).
- F-8 readiness: **plan + PG15 rehearsal complete (~70%)**; **production cutover 0% applied**; one code follow-up (A3 ingestion alias) outstanding.
- Deployment readiness: **~75%** (code + runbook + rollback + rehearsal ready; gated on F-8, A3, recovery point, Sponsor GO).
- Production activation: **0%**.
- Overall FX increment: **~70%**.

## Smallest explicit Sponsor GOs required next
1. **GO** to build the DARK follow-up: a dedicated FX-ingestion DB connection alias + `guvfx_fx_ingest` routing for `fetch_fx_daily` (code-only, isolated PR, no prod) — closes A3.
2. **GO** to execute the **F-8 production runtime-role cutover** (PR #554) — a production DB-infra change (RED), separately governed, certified recovery point first.
3. **GO** for the staged FX production rollout (Stages 4–11), each stage its own STOP/GO.

`PRODUCTION_FX_REMAINS_OFF — NO PRODUCTION MUTATION PERFORMED`
