# GuvFX Operations Dashboard

> **Operational source of truth** (Operations Mode — GFX-PKT-OPERATIONS-MODE).
> **Last verified:** 2026-07-05, read-only (host snapshot: `docker ps/inspect/stats/logs`,
> `crontab`, `/var/backups`, `df`; plus a 7-agent repo estate analysis). No production change.
> **Sole operator / owner (all services):** Nuno.
> Update this file whenever the production estate changes.

## 1. Operational architecture

Single-host, container-based algorithmic-trading platform, one Docker Compose project
(`guvfx-prod`) on one OVH VPS, plus one non-containerised Windows box for MT5.

- **Host A — app VPS (OVH):** Ubuntu 25.04, Milan. Public IP `57.131.27.145`; Tailscale
  `100.119.23.29`. **SSH is Tailscale-only** (ufw: 22 from Tailscale range; 80/443 public;
  default-deny). Runs all 11 containers + the guacamole stack. Disk 49% of 193 GB.
- **Host B — MT5 box:** Windows Server 2025, Tailscale `100.79.101.19`. MT5 `terminal64` +
  signal bridge `:8788`. **Not containerised**; autologon console-session model; bridge
  started manually via RDP; self-heals via autologon + logon tasks.
- **Edge:** Traefik (Let's Encrypt TLS) → frontend (`guvfx.com`), backend (`api.guvfx.com`),
  Guacamole (`guac.guvfx.com`).
- **Execution:** bridge-based. `trade-ingest-worker` claims `ExecutionJob`s from the API and
  calls the MT5 bridge; `shadow-worker` structurally claims **only** `PLACE_ORDER_SHADOW`
  (`order_check`, never `order_send`); `validate-worker` checks broker creds via file-handoff.
- **Acquisition:** isolated `guvfx-wayond-listener` reads Wayond Telegram → append-only
  `signal_intake` ledger; provider **UN-ARMED** → all messages `DROPPED_NOT_ARMED`, no orders.
- **Safety posture:** signal→execution boundary is import/AST-guarded; real orders (E3) held
  **RED** behind timezone verification, Blueprint-06, and Nuno's sign-off.

## 2. Production services

| Service | Image | Restart | Healthcheck | Purpose |
|---|---|---|---|---|
| guvfx-backend | guvfx-prod-guvfx-backend | unless-stopped | **none** | Django/DRF API (gunicorn :8000) |
| guvfx-frontend | guvfx-prod-guvfx-frontend | unless-stopped | **none** | Next.js 16 web app (:3000) |
| guvfx-postgres | postgres:15-alpine | unless-stopped | **none** | Primary app DB (all business state) |
| traefik | traefik:3.6.4 | unless-stopped | **none** | Reverse proxy + Let's Encrypt TLS |
| guvfx-mt5-trade-ingest-worker | guvfx-prod-guvfx-backend | unless-stopped | **none** | Claims jobs → MT5 bridge :8788 |
| guvfx-mt5-shadow-worker | guvfx-prod-guvfx-backend | unless-stopped | **none** | Shadow dry-run (order_check only) |
| guvfx-mt5-validate-worker | guvfx-prod-guvfx-mt5-validate-worker | unless-stopped | **none** | MT5 credential validation |
| guvfx-wayond-listener | guvfx-wayond-listener:latest | unless-stopped | **healthy** | Wayond Telegram acquisition (un-armed) |
| guacamole | guacamole/guacamole:1.5.5 | unless-stopped | **none** | Browser RDP/VNC to MT5 |
| guacd | guacamole/guacd:1.5.5 | unless-stopped | **healthy** | Guacamole proxy daemon |
| guac-db | postgres:15-alpine | unless-stopped | **none** | Guacamole config/auth DB |
| windows-mt5-box | (not containerised) | autologon | **none** | MT5 terminal + signal bridge :8788 |

Only **2 of 11** containers (`guvfx-wayond-listener`, `guacd`) have a real healthcheck.

**Scheduled jobs** (VPS `ubuntu` crontab, every 1–5 min): `run_h1/h4/m5_scheduler` (strategy
scheduling), `reap_stuck_launches`, `cleanup_expired_mt5_sessions`, `reliability_tick`
(`RELIABILITY_CORE_ENABLED=true` → RX-2 detection **active**). `/etc/cron.d`: `guvfx-mt5-reaper`.
**No backup job.**

## 3. Deployment model

Not a git checkout on the VPS. Local worktree is `rsync`'d into `/home/ubuntu/guvfx-prod`
(non-git); images are **baked** via `docker build -t <img> backend/` (compose has no `build:`);
**no registry, no version tags** (`:latest` only); migrations run **manually**; recreate via
`docker compose up -d --force-recreate <svc>`. `guvfx-backend` + `trade-ingest` + `shadow`
share one image. `guvfx-wayond-listener` is a **separate isolated image** run as a standalone
`docker run` (outside the compose lifecycle) — see
[DEPLOY_ISOLATED.md](../deploy/wayond-listener/DEPLOY_ISOLATED.md).

## 4. Secrets (names / locations only — values redacted)

**Locations:** `/home/ubuntu/guvfx-prod/.env` (600), inline compose `environment:` blocks,
`/home/ubuntu/guvfx-prod/wayond-listener.env` (600, a **snapshot** of DB creds).
**Names:** `DJANGO_SECRET_KEY`, `DB_{NAME,USER,PASSWORD,HOST,PORT}`, `JWT_SECRET_KEY`,
`GUVFX_WINDOWS_AGENT_TOKEN`, MT5 Fernet key, Guacamole admin + `guac-db` passwords, MT5 desktop
password, `MT5_SHADOW_WORKER_TOKEN`, `TELEGRAM_{API_ID,API_HASH,STRING_SESSION,DEVICE_MODEL,SYSTEM_VERSION,APP_VERSION}`.
**Status:** several secrets **flagged EXPOSED and un-rotated** (two prior incidents;
documented **password reuse**: Guacamole admin == `guac-db` password). Rotation is **Nuno-held**
(no LLM may rotate/approve). On DB-cred rotation, `wayond-listener.env` must be **re-captured**
or the listener silently restart-loops.

## 5. Monitoring & observability

- **Active:** `reliability_tick` (RX-2 detection) every minute; Docker healthchecks on
  `guacd` + `guvfx-wayond-listener`; file logs in `/var/log/guvfx/`; structured lifecycle logs
  (`core/observability.py`) to stdout/container logs.
- **Missing:** real healthchecks on the other 9 containers; `backend /health` is trivial (no
  DB/dependency probe — Traefik keeps routing to an unhealthy backend); no metrics
  (Prometheus/Grafana **volumes exist but containers are down**); no external uptime monitor;
  **alert delivery sink unconfirmed** (RX-2 emits, but whether anything receives is unverified).

## 6. Backups & recovery

**🔴 No automated database backup exists.** Verified 2026-07-05: no backup cron (`ubuntu` or
`root`), nothing in `/var/backups`, newest dump = `guvfx_db_backup_20260219T105320Z.sql`
(**2026-02-19, ~4.5 months stale**), no off-host copy; `guac-db` unbacked. The repo's
`OPERATIONS_RUNBOOK.md` §11 is a **recommended template only**, never deployed. **RTO/RPO
undefined; restore never tested.**

**Recovery procedures by failure mode:**
| Failure | Behaviour / action |
|---|---|
| Container crash | `restart:unless-stopped` auto-recovers |
| Container **hung** but running | **NOT auto-detected** (no healthcheck) → manual `docker restart` |
| `guvfx-wayond-listener` unhealthy | `docker rm -f` + re-run Phase 4 ([DEPLOY_ISOLATED.md](../deploy/wayond-listener/DEPLOY_ISOLATED.md)); auto-heal only if it exits |
| MT5 bridge down | RDP to Windows box → start bridge; autologon self-heals the console session |
| **DB data loss** | **Unrecoverable past 2026-02-19** — the top gap |
| Host loss | Total outage, no standby |

## 7. Risk register (evidence-based)

| Sev | Category | Finding |
|---|---|---|
| 🔴 | missing_backups | No automated DB backup for `guvfx-postgres` or `guac-db`; newest dump 4.5 months old; no off-host copy. Total data-loss SPOF. |
| 🔴 | SPOF | Entire estate on one VPS — single Postgres (no replication), single Traefik (only TLS entrypoint), single Tailscale tunnel, single Windows box + single manually-started bridge. |
| 🔴 | missing_monitoring | 9/11 containers have no healthcheck; `/health` is trivial (no DB probe); no worker healthchecks; MT5 bridge/terminal not health-polled. |
| 🔴 | missing_alerts | No alert delivery anywhere (no Slack/email/PagerDuty/Sentry). RX-2 detection runs but the sink is unconfirmed. No cert-expiry, backup-failure, bridge-down, or disk alerts. |
| 🔴 | security | Exposed secrets un-rotated + password reuse (Guac admin == guac-db). Plaintext on-disk, no secret manager. **Nuno-held.** |
| 🔴 | SPOF | MT5 execution path = one Windows box + one terminal + one bridge, manually started, autologon-dependent; worker calls one endpoint with no failover/circuit-breaker. |
| 🟡 | operational_debt | No image tags/registry; shared `:latest` backend image (patch hits shadow path too); manual migrations, no startup gate; standalone listener outside compose. |
| 🟡 | maintenance_debt | Dormant `terminal_provisioning.SessionAssignment`; `wayond-listener.env` snapshot fragility; `MT5_SHADOW_WORKER_TOKEN` recreate fragility; temp personal Telegram account pending GFX swap; Guacamole auth-JAR can be lost on restart. |

## 8. Operational maturity

**Functional but pre-operational.** Safety discipline on the dangerous paths is strong
(fail-closed boundary, structural shadow-only orders, un-armed provider, E3 RED). The
operational substrate is early: single host, **no confirmed backups**, thin healthchecks, no
confirmed alert delivery, untagged manual deploys, a manually-operated execution bridge, and
un-rotated exposed secrets. Appropriate for a **gated pilot**; **not hardened for real-money
operation.** The gap to production-ready is dominated by three non-feature items: **backups,
monitoring/alerting, secret rotation.**

## 9. Recommended next packet

**GFX-PKT-BACKUP-RECOVERY-BASELINE** — deploy a daily `pg_dump` for `guvfx-postgres` **and**
`guac-db`, add off-host replication (so backups don't die with the VPS disk), a "no backup in
24h / anomalous size" check, and **one documented, tested restore** to a throwaway container;
then define RTO/RPO. Highest-value, additive, behaviour-preserving, no live-trading authority,
and a hard prerequisite for E3. (Secret rotation is co-equal in severity but is a Nuno-held
manual action, so it is tracked separately, not agent-executable.)
