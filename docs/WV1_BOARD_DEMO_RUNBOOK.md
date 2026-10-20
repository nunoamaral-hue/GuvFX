# WV1 Board Demo — Runbook (isolated demo environment)

How to stand up the Withdrawal Intelligence board demo on an **isolated** database, with **labelled synthetic**
data. This never touches production: a separate DB, the same `settings.py` driven by a demo `.env`, and the
demo-only flags armed only in that environment. Production stays at schema 0010 with all WV1 flags OFF.

## What the demo shows (WV1 §5)
- **A — Broker Accounts** (`/accounts`): ten MT5/broker accounts for the demo member, **including two at the same
  broker** (Pepperstone ×2), distinguished by name / server / masked number / Demo badge.
- **B — Connected Email** (`/profile`): the connected-mailbox panel (the owner/mailbox side of attribution).
- **C/D — Withdrawal Monitoring + History** (`/dashboard`): the withdrawal panel + per-withdrawal history,
  including the **owner-attributed, account-UNRESOLVED** reconstruction (external Ethereum, request→completion
  **33m01s** apart) rendered with the distinct **"Account unresolved"** label and a **DEMONSTRATION DATA** banner.
- **D — Genuine evidence case study** (`/brokers/case-study`): the ONE real pilot withdrawal (TradersWay, Ethereum,
  **$24.12**, request→completion **33m01s**, owner-attributed/account-UNRESOLVED, DKIM+SPF authenticated,
  broker-reported completion only) — redacted, authenticated-on-production, clearly SEPARATE from the synthetic data.
- **F — WAYOND Trader Match (WTM) preview** (`/brokers/wtm`): the broker-performance metrics page (authentic
  `INSUFFICIENT_VERIFIED_DATA` state + a clearly-labelled SIMULATED scorecard — processing-time median/avg/p90,
  histogram, lifecycle, broker filter — the GuvFX-evidence→aggregate→WTM flow, and source/methodology/privacy
  badges). Reuses the DARK `broker_public` API; NOT a deployed WAYOND integration and publishes NO public rating.
  (The `Wayond WIM Strategy` marketplace card at `/strategies/marketplace` is a separate, unrelated surface.)

All withdrawal data is **`provenance=SYNTHETIC`** and only visible because `BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC`
is armed in the demo env (it labels every row "DEMONSTRATION"). The genuine pilot evidence itself is **not** copied
into the demo — the account-UNRESOLVED row is a **redacted reconstruction of its structure** (placeholder reference,
no real token / address / account), so no real financial email leaves production (WV1 §2/§6).

## 1. Isolated database
```bash
# From a shell with the demo DB credentials (NOT production). Uses the same Postgres as dev here; a hosted demo
# uses its own instance.
createdb guvfx_wv1_demo    # or: psql -d postgres -c 'CREATE DATABASE guvfx_wv1_demo;'
```

## 2. Demo environment variables
A demo `.env` (or exported vars) — distinct `DB_NAME`, demo host origins, WV1 flags ON. Production `.env` leaves all
of these unset.
```
DJANGO_DEBUG=true
DB_NAME=guvfx_wv1_demo
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
# browser demo (frontend :3000 -> backend :8000): additive CORS/CSRF + host-only CSRF cookie
CORS_ALLOWED_ORIGINS=http://localhost:3000
CSRF_TRUSTED_ORIGINS=http://localhost:3000
CSRF_COOKIE_DOMAIN=
# auth (JWT) cookie — host-only + non-Secure so it is sent over http://localhost (prod default is Secure/.guvfx.com)
AUTH_COOKIE_SECURE=False
AUTH_COOKIE_DOMAIN=
AUTH_COOKIE_SAMESITE=Lax
# WV1 demo surface (DARK in prod)
BROKER_WITHDRAWAL_UX_ENABLED=1
BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC=1
BROKER_MAILBOX_STATUS_ENABLED=1          # screen B — connected email panel
BROKER_PUBLIC_WTM_PREVIEW_ENABLED=1      # screen F — WAYOND Trader Match preview (/brokers/wtm)
```
Frontend (`frontend/.env.local`): `NEXT_PUBLIC_API_BASE=http://localhost:8000`.

## 3. Migrate + seed
```bash
cd backend
.venv/bin/python manage.py migrate --noinput               # applies broker_intelligence through 0016 on the demo DB
.venv/bin/python manage.py seed_wv1_board_demo --confirm-demo-db
```
The seeder is **idempotent** and **refuses** any DB that does not look like a demo DB (no "demo" in the name, DEBUG
off, demo flag off) — so it can never run against production. It creates the demo member, 10 accounts, 8 synthetic
withdrawals, and runs the real correlation engine to project the owner-attributed/account-UNRESOLVED reconstruction
(`correlation={'created': 1, 'completion_advanced': 1}`).

## 4. Run
```bash
cd backend && .venv/bin/python manage.py runserver 0.0.0.0:8000      # demo env from step 2
cd frontend && npm run dev                                           # :3000, NEXT_PUBLIC_API_BASE from .env.local
```
Set the demo member's password once (demo login): `manage.py shell` →
`from django.contrib.auth import get_user_model as g; u=g().objects.get(username="demo-board"); u.set_password("<demo-pw>"); u.save()`.

## 5. Teardown
`dropdb guvfx_wv1_demo` (and remove `frontend/.env.local`). Nothing in production changed.

## Guarantees
- **Production untouched**: separate DB; prod `.env` leaves every WV1 flag + the CORS/CSRF demo vars unset, so
  `settings.py` is byte-identical to today (prod CORS/CSRF origins only) and the member API stays `404`.
- **No real evidence leaves prod**: the demo reconstructs the genuine withdrawal's structure from placeholders; the
  real RFC822 bytes remain only in the production evidence store.
- **Truthful**: every demo row is SYNTHETIC and labelled "DEMONSTRATION"; the 33m01s figure is the email-to-email
  interval (request→completion), not a system latency.
- **Migration gate**: applying `0016` (and 0011–0015) in **production** remains NO-GO until the off-host DB-recovery
  verified-restore gate clears (`DATABASE_RECOVERY_READINESS`). The demo runs entirely on its own DB and does not
  depend on that gate.
