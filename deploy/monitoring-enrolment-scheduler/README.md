# monitoring-enrolment-scheduler (ADR-0103, DARK)

Periodic, idempotent **monitoring-enrolment reconciler** — the deployable scheduler for
`reconcile_monitoring_enrolment` (GFX-DASHBOARD-AUTO-MONITORING-ENROLMENT-087). It closes the
systemic gap that a newly broker-connected MT5 account did not automatically become a complete
dashboard data source.

## What it schedules

Every 5 minutes it runs:

```
python manage.py reconcile_monitoring_enrolment reconcile --apply
```

That pass discovers every eligible (connected, non-tombstoned) account, idempotently reserves +
registers a deterministic `MonitoringRoute` from each account's existing per-tenant endpoint
(reusing the ADR-0075 primitives — it never builds a competing onboarding path), and raises
governed `reliability.AlertEvent`s for any account stuck in a non-ready stage.

## Safety — two-level DARK, installing it changes nothing

`--apply` is a **dormant no-op** unless BOTH flags are on (self-gated in
`execution.monitoring_enrolment.reconcile_enrolment`; the underlying `reserve_route_for_workspace`
is itself gated on the gateway master flag):

- `MONITORING_GATEWAY_ENABLED`
- `MONITORING_ENROLMENT_RECONCILE_ENABLED`

Even once enabled, the reconciler only **reserves/registers** route rows from existing endpoints
and writes alerts. It **never**:

- marks a route `READY` (that requires the ADR-0075 gateway's live identity probe — a separate,
  Sponsor-gated deploy),
- contacts a host, mutates a Tailscale policy, bridge, account or workspace,
- places, sizes, approves or arms any order, or touches Copy Trading.

The read-only dashboard **enrolment projection** (each account's monitoring stage + truthful UI
copy) is independent of this cron and of both flags — truthful presentation is always on; only the
route **mutation** is dark.

## Install / remove

```
deploy/monitoring-enrolment-scheduler/install_monitoring_enrolment_cron.sh            # install (idempotent)
deploy/monitoring-enrolment-scheduler/install_monitoring_enrolment_cron.sh --remove   # uninstall
```

Overridable: `COMPOSE_DIR`, `LOG_DIR`, `BACKEND_SERVICE`, `SCHEDULE`. The installer pre-creates a
writable log (`/var/log/guvfx/monitoring_enrolment.log`) + a logrotate rule, and is idempotent
(end-anchored marker `# guvfx-monitoring-enrolment`; touches no other cron line).

## Rollout order (each step a separate Sponsor-gated decision)

1. Deploy the code (DARK) — nothing changes; the dashboard projection becomes truthful.
2. Install this cron — still inert (both flags off).
3. Dry-run first: `reconcile_monitoring_enrolment report` (read-only) and review the per-account
   stage table.
4. Flip `MONITORING_GATEWAY_ENABLED` then `MONITORING_ENROLMENT_RECONCILE_ENABLED` to let the cron
   begin reserving routes. (Marking routes READY and live identity verification remain a further,
   separate gateway-deploy decision.)
