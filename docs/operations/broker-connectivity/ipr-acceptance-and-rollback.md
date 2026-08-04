# IPR DARK Deployment — Rollback Readiness & Product-Acceptance Preparation

Companion to [ipr-deploy-package.md](ipr-deploy-package.md). Records the **post-deployment** rollback
assets (verified, not executed) and prepares the exact assets for the *next* packet
(**Staged Arming & Disposable-User Browser Acceptance**). Nothing here arms anything.

Deployed state (2026-08-04): main `ead6fc3`; backend image `99bdc5bd`, frontend image `f3086f94`,
both provenance `ead6fc3fe125279227e72390c4581a380b157eeb`. All nine capability flags per §Rollback.

---

## Part A — Rollback readiness (verified ready; NOT executed)

### Rollback images (tagged before the rebuild)
- Backend: `guvfx-prod-guvfx-backend:rollback-preIPR-20260804T225449Z` → `af0275db2fb2`
- Frontend: `guvfx-prod-guvfx-frontend:rollback-preIPR-20260804T225449Z` → `b267655ff6dd`

### Database backup (verified)
- `/home/ubuntu/backups/pre-ipr-dark-deploy-20260804T225107Z.sql.gz` — SHA-256 `2180ceab3ea9f7df73681b909341c0b4e196571ca74e2f7058c8f1fed62456e3`, gzip OK, **restore-verified** into a scratch DB (103 tables, 451 trades, 30778 execjobs) then dropped.

### Rollback procedures
**Image-only rollback (preferred — safe; the IPR migrations are additive/backward-compatible, so the old
image runs fine against the migrated schema):**
```bash
cd /home/ubuntu/guvfx-prod
docker tag guvfx-prod-guvfx-backend:rollback-preIPR-20260804T225449Z  guvfx-prod-guvfx-backend:latest
docker tag guvfx-prod-guvfx-frontend:rollback-preIPR-20260804T225449Z guvfx-prod-guvfx-frontend:latest
docker compose up -d --no-deps guvfx-backend guvfx-frontend
```
**Full rollback (only if the additive schema must also be reverted):** restore the backup into `guvfx`
after stopping the backend, then image-rollback. This drops the 5 additive broker-connectivity tables.

### Config rollback
- **No production config was changed.** `beta.env` and all env files are byte-identical to pre-deploy.
- Original flag values captured (see Part C). Nothing to revert.

### Post-rollback checks (run all)
health `api/health`→200 · `/api/version`→prior/absent (404 if rolled to pre-provenance image) · routes
`/`,`/login`,`/accounts`→200 · CZ #12 runtime `BETA/RUNNING` · terminals/bridge/agent unchanged · audit +
Trade/ExecutionJob counts sane · trade-ingest still syncing.

---

## Part B — Staged arming sequence (for the NEXT packet; requires separate Sponsor authorisation)

Never "enable all". Each stage is one Sponsor-authorised flag change via the WP5.4
[arming-runbook.md](arming-runbook.md), verified before the next.

| Stage | Flag(s) to arm | Scope | Purpose | STOP condition (roll back the flag) |
|------:|----------------|-------|---------|-------------------------------------|
| 0 | *(none)* | — | Confirm DARK baseline via `/api/version` + `/build-info.json` | any flag already true unexpectedly |
| 1 | `OPERATIONS_EVENTS_ENABLED` (backend) | observe | Operational-event recording (projection-only, fail-open) | recorder errors in `guvfx.operational_events`; API not 404→200 owner-scoped |
| 2 | `NEXT_PUBLIC_OPERATIONS_ENABLED` (frontend rebuild) | observe | Operator-only read-only Operations UI | any non-operator sees data; any customer-visible change |
| 3 | `BROKER_CONNECTIVITY_ENABLED` (backend) | onboard | Customer broker-account journey backend live | validation writes wrong state; any exec path touched |
| 4 | `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` (frontend rebuild) | onboard | `/broker-accounts` journey + **marketplace arm UI appears** (still gated on runtime-readiness + backend arm flag) | arm reachable before intended; `/accounts` redirect loops |
| 5 | `BROKER_CONNECTIVITY_HEALTH_ENABLED` (backend) | converge | WP3 health state machine | health not converging; spurious pauses/alerts |
| 6 | `BROKER_CONNECTIVITY_EXECUTION_GATE` (backend) | enforce | Execution refused unless VALIDATED (WP6-gated) | any legitimate order blocked; refusal spike |

`BETA_RUNTIMES_ENABLED` / `BETA_SELF_SERVE_ARM_ENABLED` are already ON (see Part C) — no arming action;
review only. An Internal Pilot does **not** require Stage 6 (execution gate MAY stay OFF).

## Part C — Effective production flag state (post-deploy, authoritative `/api/version`)

| Flag | Value | Note |
|------|:-----:|------|
| BROKER_CONNECTIVITY_ENABLED | **false** | DARK |
| BROKER_CONNECTIVITY_EXECUTION_GATE | **false** | DARK |
| BROKER_CONNECTIVITY_HEALTH_ENABLED | **false** | DARK |
| OPERATIONS_EVENTS_ENABLED | **false** | DARK |
| NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED | **false** | DARK (frontend build-info) — also gates the marketplace arm UI |
| NEXT_PUBLIC_OPERATIONS_ENABLED | **false** | DARK (frontend build-info) |
| BETA_ONBOARDING_ENABLED | **false** | DARK (legacy gate; superseded by ADR-0021 predicates) |
| BETA_RUNTIMES_ENABLED | **true** | **Depended on** — Customer Zero (acct #12) has a `BETA/RUNNING` runtime; control-16 kill switch. Keep ON. |
| BETA_SELF_SERVE_ARM_ENABLED | **true** | Gates `signal_copy_arm`/`toggle` endpoints. Acct #1 `ti_signals`+`wayond` AUTO_DEMO/LIVE assignments execute independently of it. Now **inert for exposure** (frontend gates the arm UI). No urgency to change; if disabled later, confirm no operator toggle-endpoint workflow first. |

## Part D — Product-acceptance assets (for the next packet)

**Disposable user (operator/Nuno creates):** a fresh allow-listed beta account (never Customer Zero,
never a live-money account), plus a **demo** MT5 broker account (login/password/server) to add + validate.

**Browser acceptance checklist** (only after the staged flags above are armed under authorisation):
```
Sign up → Log in → Broker Accounts visible → Add demo account → Validate → Runtime ready →
Marketplace → Select account → Arm Telegram strategy → Enable → Backend-confirmed RUNNING →
Events visible → Operations timeline updated → Disable → Disconnect → History preserved
```

**Evidence template (capture per step):** screenshot · API request/response · `/api/version` provenance ·
account status (`/broker/status`) · runtime status (AccountRuntime) · strategy assignment ·
health state (BrokerAccountHealth) · operational events · audit rows · Trade/ExecutionJob counts.

**Constraints for acceptance:** no SSH / DB / Claude intervention during the customer journey; disposable
user only; demo only; Customer Zero excluded; capture the whole run as evidence.
