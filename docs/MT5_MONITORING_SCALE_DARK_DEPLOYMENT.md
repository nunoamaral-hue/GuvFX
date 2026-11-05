# MT5 Monitoring Gateway — rollback-first DARK deployment + migration packet (Phase 12)

GFX-MT5-BRIDGE-SCALE-001 / ADR-0075. **PREPARED, NOT EXECUTED.** No deployment, migration, Tailscale
change, host-gateway start, or account mutation is performed by this packet. Every step below is
Sponsor-gated and sequenced so the default production posture is unchanged until each flag is armed.

Canonical merge SHA: `<FILLED AT MERGE>`. Prod backend baseline: `2009a8a7`.

## 0. Safety posture (what stays true through every step below)
Execution OFF; Copy Trading OFF; no order; no new live account; no account deletion; agent token never
rotated/read/printed. All four monitoring flags DEFAULT OFF, so a plain deploy of the merge SHA is
behaviour-identical to today (the model tables exist but nothing reads them).

## 1. Rollback-first preparation (before any mutation)
1. Record rollback image tag `rollback-pre-bridgescale-<ts>` for the current prod backend (`2009a8a7`).
2. Take a DB dump (as for prior deploys) and record its gzip integrity + sha256.
3. Confirm the merge SHA's migrations are additive: `0037_monitoring_gateway` only `CreateModel`/`AddIndex`/
   `AddConstraint` (auto-reversible; reverse = `migrate execution 0036`).

## 2. Migrate (additive, reversible)
`docker compose run --rm --no-deps -T guvfx-backend python manage.py migrate execution 0037`
(one-off container on the new image; the running container is untouched — the proven-safe order). Verify
the three new tables exist and are empty. Rollback: `migrate execution 0036` (drops the three tables; no
data loss — they were empty and DARK).

## 3. Build + recreate backend on the merge SHA (flags still OFF)
`docker build -t guvfx-prod-guvfx-backend:<sha> --build-arg GIT_COMMIT=<sha> /home/ubuntu/guvfx-app/backend`
then `docker compose up -d --no-deps --force-recreate guvfx-backend` (NEVER `--remove-orphans`). Verify
`/api/` healthy (401 on unauth = healthy) and `GUVFX_GIT_COMMIT=<sha>`. At this point the subsystem is
fully inert (master flag OFF).

## 4. Legacy→gateway migration plan (DARK, staged)
1. **Represent the estate (no behaviour change).** Arm `MONITORING_GATEWAY_ENABLED` only, then run
   `python manage.py shell -c "from execution.monitoring_onboarding import sync_routes_from_endpoints as s; print(s())"`
   once. This creates a deterministic `MonitoringRoute` (RESERVED/REGISTERED, bound to each live
   `HostedExecutionEndpoint`) for the 11 live accounts. Still no routing change — the client stays on the
   legacy per-account endpoints. Rollback: flip the flag OFF (routes become inert) or delete the DARK rows.
2. **Reconcile read-only.** Arm `MONITORING_ROUTE_RECONCILE_ENABLED`, run `reconcile_monitoring_routes`
   once manually. It classifies each route's readiness/reason and raises governed alerts. Confirm it
   reproduces the Phase-1 diagnosis (#50 terminal_init_failed; #53/#54/#56/#57 feature_disabled; the rest
   READY/flat). No schedule is added yet.
3. **Deploy + certify the host gateway (separate packet, Sponsor-gated).** Install `host_gateway` on
   `100.79.101.19` bound to ONE Tailnet port; register the 11 local bridges (loopback). Run the Phase-9
   escape/isolation battery on the host. Only on PASS, arm `MONITORING_GATEWAY_ROUTING_ENABLED` so the
   client addresses the single gateway port. Rollback: flip routing OFF → client falls back to legacy
   endpoints byte-identically.
4. **Retire the per-account Tailnet ports.** Once routing is proven through the gateway, the per-account
   bridge ports no longer need Tailnet reachability — see the ACL delta below.

## 5. Exact minimal future Tailscale policy delta (for the operator; applied via the existing
least-privilege policy OAuth operator — `GFX-TAILSCALE-POLICY-AUTOMATION-001`; NOT applied here)
- **Target posture (gateway):** backend (`100.119.23.29`) → host (`100.79.101.19`) permit **only** the
  single gateway port (e.g. `tcp:8790` if the gateway reuses the hosted-executor convention, or a dedicated
  gateway port). All per-account bridge ports (`8800–8899`) are then **removed** from the backend→host
  grant — the gateway reaches them on host loopback. Net delta from today (`…,8804-8814`): collapse the
  whole range to the one gateway port.
- **Interim (if routing not yet armed):** no change required to run the foundation DARK; the current
  `8804-8814` grant continues to serve the legacy path.
- **Fallback (Design B, if the gateway is not deployed):** certify the full pool sub-range once
  (`8804-8814` → the certified pool, e.g. `8800-8819`) so in-range onboarding needs no further ACL edit;
  range exhaustion is then the only future trigger.

## 6. Does the preferred gateway eliminate future per-account policy edits?
**Yes.** With routing armed and the ACL reduced to the single gateway port, every subsequent account (and
every new logical node on the same physical host) is reached through that one port — **zero** new Tailscale
grants per account, ever. A brand-new physical host needs exactly one grant (its gateway port) once. This
structurally closes the recurring onboarding bridge-port ACL gap. The port-pool fallback reduces churn to
once-per-range-exhaustion but does not fully eliminate it.

## 7. Remaining Sponsor decisions (not in this packet)
Host-gateway deployment + Phase-9 on-host isolation certification; the ACL reduction above; enabling the
ADR-0072 positions gate (`MT5_LIVE_READONLY_POSITIONS_ENABLED`) to un-gate live positions; repairing #50's
MT5 terminal init (upstream MT5-bridge action); wiring the structured readiness into the dashboard UI and
the onboarding writer; whether to add a reconcile schedule (and its cadence).
