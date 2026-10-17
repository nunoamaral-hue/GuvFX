# Withdrawal V1 — Production Promotion & Rollback Plan

**Status:** DARK reference plan. Nothing here is executed until (a) a genuine external-withdrawal classifier is
certified (see the authenticity spec) and (b) the Programme Director authorizes promotion. Every feature flag stays
OFF until then.

---

## 1. What "promotion" means here

The subsystem is built and DARK. Promotion flips member-facing visibility and/or continuous correlation from off to
on, in a controlled order, each step reversible image-only. It does **not** grant any execution/financial authority —
Withdrawal V1 is read-only observation.

## 2. Feature flags (all currently OFF)

| Flag | Gates | Promote when |
|---|---|---|
| `BROKER_INTELLIGENCE_INGEST_ENABLED` | the scheduled ingestion worker | already armed in prod (read-only capture) |
| `BROKER_WITHDRAWAL_CORRELATION_ENABLED` | `run_withdrawal_correlation` (EXTERNAL_WITHDRAWAL → Withdrawal) | a positive classifier is certified AND a genuine withdrawal exists |
| `BROKER_WITHDRAWAL_UX_ENABLED` | the member metrics/history endpoints + panels (404 while off) | after correlation is validated on genuine data |
| `BROKER_MAILBOX_CONNECT_ENABLED` | the Gmail connect flow | already on (owner self-connect) |

`CERTIFIED_BROKERS` (code constant, not a flag) stays empty until a broker's positive classifier is certified against
genuine evidence; adding a broker is a reviewed code change, not a runtime toggle.

## 3. Schema boundary (critical)

- Production is at `broker_intelligence` migration **0010**. Main carries **0011** (ConnectedMailbox health columns,
  additive/nullable/reversible) **unapplied in prod**.
- Keep the production schema boundary unchanged until a separately-approved migration **and** DB-recovery plan exists
  (there is currently no off-host DB recovery — see the DB-recovery foundation; production activation is NO-GO).
- The classifier-prep work in this packet is **schema-free** (no new migration). Any future proximity-signal column
  (0012) is additive/reversible and DARK until the same gate.
- The reader code is unmigrated-safe (`r1_columns_present()` + `.only()`), so applying 0011 is not a prerequisite for
  the health read model.

## 4. Promotion procedure (when authorized)

1. **Pre-flight (read-only):** confirm host, the running backend image by immutable id, schema state, disk headroom,
   ingest timer active / service idle, and that `make check` is green on the release commit.
2. **Pin rollback image by id:** `docker tag <running-id> guvfx-prod-guvfx-backend:rollback-pre-wv1-promo`.
3. **Build** the release image from the exact approved commit; verify it contains the certified classifier
   (`CERTIFIED_BROKERS` populated for the certified broker only) and the expected migration set.
4. **Apply migrations only if approved** (0011/0012) — never implicitly; the backend CMD is gunicorn-only (no
   auto-migrate). If a migration is in scope, run it explicitly and confirm reversibility first.
5. **Recreate only the backend:** `docker compose up -d --no-deps --force-recreate guvfx-backend` — **never**
   `--remove-orphans`; do not touch postgres, traefik, the MT5 workers, the wayond listener, or any other container.
6. **Flip flags incrementally:** correlation first (observe a clean pass), then UX. Each flag change is a backend
   recreate; verify `/api/health/` 200 and the endpoints behave (404→live) after each.
7. **Verify** owner-scoping, per-currency separation, sample-size disclosure, no cross-member exposure, and that only
   genuine (provenance REAL) withdrawals surface.

## 5. Rollback (image-only; no DB rewind)

- **Code/flags:** set the flag(s) back OFF and recreate the backend, or retag `rollback-pre-wv1-promo` →
  `:latest` and `docker compose up -d --no-deps --force-recreate guvfx-backend`. The panels self-hide on the 404; no
  member sees a half-promoted state.
- **No DB rewind, ever.** Evidence and events are append-only/immutable; correlation only advances
  `correlation_status` and projects idempotently. A rollback is image/flag-only. If a migration was applied, reverse
  it only via its own tested reverse op, never a manual DB edit.
- **Blast radius:** backend container only. All trading/Telegram/MT5 workloads and the DB are untouched by any step.

## 6. Abort conditions

Stop and escalate (do not promote) if: `make check` is not green on the release commit; disk headroom is
insufficient for a build+recreate; a migration is required but the DB-recovery gate is still NO-GO; the certified
classifier cannot reproduce the genuine sample byte-identically; or any adversarial §8 case regresses.
