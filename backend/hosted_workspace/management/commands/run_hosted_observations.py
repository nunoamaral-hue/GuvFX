"""run_hosted_observations — Beta Readiness Stream 2 (G15): the periodic autonomous-provisioning scheduler.

One cron cycle (cadence = the cron schedule; run it e.g. every minute). It:
  1. allocates a node for any workspace still at PROVISIONING (G2, ``provisioning_runner``), then
  2. polls hosted observations through the certified single writer (``observation_runner``).

Two-level darkness (Workstream C fail-closed): a DORMANT no-op unless ``HOSTED_OBSERVATION_SCHEDULER_ENABLED``
is on (or ``--force``); and even then each driver only does work while the master
``hosted_persistent_mt5_enabled()`` is on. SINGLETON / no-overlap: a Postgres advisory lock — a second cycle
that finds the lock held simply skips (never two concurrent observation passes). Observable health: a
secret-free summary line. It NEVER launches MT5, never logs in, never places an order, and never arms
execution — it only advances PROVISIONING→WAITING_FOR_LOGIN and ingests observations.

The per-workspace ``observe_fn`` is a PLUGGABLE seam. In this repository-only phase there is no host-resident
observe bridge, so the resolver returns a fail-closed function that yields ``None`` for every workspace
(observation unavailable → nothing ingested → freshness lapses → fail-closed). A later host-certification
increment wires a real host observe bridge here without touching this command's control flow.
"""
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from hosted_workspace.flags import hosted_observation_scheduler_enabled

# Fixed 64-bit advisory-lock key for the hosted-observation singleton (arbitrary but stable).
_SINGLETON_LOCK_KEY = 748_293_410_017

# Fast onboarding re-poll (P0): re-observe pre-CONNECTED tenants every ~INTERVAL for up to ~BUDGET seconds within
# one cron invocation. BUDGET is kept comfortably < 60s so the invocation finishes and releases the singleton
# lock before the next cron tick (no overlap). Overridable via env for tuning.
def _repoll_interval_s() -> float:
    import os
    try:
        return max(3.0, float(os.getenv("HOSTED_OBSERVATION_REPOLL_INTERVAL_S", "12")))
    except (TypeError, ValueError):
        return 12.0


def _repoll_budget_s() -> float:
    import os
    try:
        return max(0.0, min(50.0, float(os.getenv("HOSTED_OBSERVATION_REPOLL_BUDGET_S", "45"))))
    except (TypeError, ValueError):
        return 45.0


def _dark_observe_fn(_workspace):
    """Fail-closed placeholder observe_fn for the repository-only phase: no host observe bridge exists yet, so
    every workspace observation is UNAVAILABLE (``None``) → nothing is ingested. Replaced by a real host
    observe bridge in a later host-certification increment."""
    return None


def resolve_observe_fn():
    """Return the observe_fn the cycle will use (pluggable seam). STREAM 9E: the REAL live host observe transport
    (``live_observe.live_observe_fn`` — signed ``OBSERVE_WORKSPACE`` → session-bound observer → certified
    producer) is selected when ``HOSTED_MT5_OBSERVATION_ENABLED`` is on AND EITHER the trust anchor
    ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` (ADR-0041) OR the bounded ``SUPERVISED_SINGLE_TENANT_BETA_ENABLED``
    posture (ADR-0044) is on; otherwise the fail-closed dark placeholder (no host bridge, ingests nothing).
    ``live_observe_fn`` is itself fail-closed on the SAME gates PER WORKSPACE (the supervised branch additionally
    enforces the single-non-CZ-demo-tenant boundary in ``supervised_single_tenant_beta_active``), so the darkness
    — and the single-tenant bound — holds even if this resolver were bypassed."""
    from hosted_workspace.flags import (
        hosted_mt5_observation_enabled,
        hosted_remoteapp_isolation_certified,
        supervised_single_tenant_beta_enabled,
    )
    if hosted_mt5_observation_enabled() and (
            hosted_remoteapp_isolation_certified() or supervised_single_tenant_beta_enabled()):
        from hosted_workspace.live_observe import live_observe_fn
        return live_observe_fn
    return _dark_observe_fn


def try_acquire_singleton(key: int = _SINGLETON_LOCK_KEY) -> bool:
    """Non-blocking singleton guard. On Postgres, ``pg_try_advisory_lock`` — True iff acquired (a concurrent
    cycle holding it returns False → the caller must skip). On any other backend (e.g. a test sqlite), there
    is no cross-connection lock available, so return True and rely on the cron cadence for non-overlap."""
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
        return bool(cur.fetchone()[0])


def release_singleton(key: int = _SINGLETON_LOCK_KEY) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", [key])


def run_cycle(*, observe_fn=None) -> dict:
    """One provisioning + observation + auto-arm pass (NO flag gate, NO lock — the drivers self-gate on their
    flags). Exposed for tests. Returns a combined secret-free summary. Ordered: allocate nodes → ingest
    observations (advance canonical state, incl. → EXECUTION_READY) → auto-arm any EXECUTION_READY-but-unarmed
    workspace (ADR-0044 Decision 2; DARK unless master + execution flags on; the arm re-proves all preconditions)."""
    from hosted_workspace.auto_arm_runner import run_hosted_auto_arm
    from hosted_workspace.capability_recovery import run_hosted_capability_recovery
    from hosted_workspace.delivery_observe_runner import run_hosted_delivery_observe
    from hosted_workspace.flags import hosted_bounded_observation_enabled, hosted_delivery_lifecycle_enabled
    from hosted_workspace.observation_runner import run_hosted_observations
    from hosted_workspace.provisioning_runner import run_workspace_provisioning
    prov = run_workspace_provisioning()
    # P0 BOUNDED path (flag ON, production cycle only — tests injecting a serial observe_fn keep the legacy path):
    # ONE bounded, tenant-isolated, de-duplicated concurrent observe pass drives BOTH the canonical and delivery
    # single writers, so a slow/unavailable tenant cannot serialize the cycle. Recovery still runs AFTER observation.
    if hosted_bounded_observation_enabled() and observe_fn is None:
        from hosted_workspace.bounded_observation import run_bounded_observation_cycle
        b = run_bounded_observation_cycle(source="hosted_workspace.scheduler")
        obs = {"enabled": b["enabled"], "polled": b["polled"], "applied": b["applied"],
               "unavailable": b["unavailable"], "errors": b["errors"]}
        dd = b["delivery"]
        deliv = {"enabled": hosted_delivery_lifecycle_enabled(), "polled": b["polled"],
                 "connected": dd["connected"], "disconnected": dd["disconnected"], "held": dd["held"],
                 "cz_skipped": dd["cz_skipped"], "errors": b["errors"]}
        recovery = run_hosted_capability_recovery()
        arm = run_hosted_auto_arm()
        return {"provisioning": prov, "observation": obs, "capability_recovery": recovery,
                "delivery": deliv, "auto_arm": arm, "bounded": {"workers": b["workers"], "reasons": b["reasons"]}}
    # LEGACY serial path (flag OFF or test-injected observe_fn) — byte-identical to before this stream.
    obs = run_hosted_observations(observe_fn=observe_fn or resolve_observe_fn(),
                                  source="hosted_workspace.scheduler")
    # AJ#6.3 Shape-3: post-login MT5 automation-capability recovery — AFTER observation (so it sees the current
    # CONNECTED+matched+trade_allowed=False state) and BEFORE auto-arm. DARK unless HOSTED_CAPABILITY_RECOVERY_
    # ENABLED; capability-only (re-assert config + graceful tenant relaunch); bounded/loop-safe; CZ-excluded; it
    # advances no state and arms nothing — the observer re-proves trade_allowed=True on the next cycle.
    recovery = run_hosted_capability_recovery()
    # BB#1: the delivery-CONNECTED edge — drive the delivery single writer from the trusted session signal.
    # DARK unless HOSTED_DELIVERY_LIFECYCLE_ENABLED; own transport gating; CZ-excluded; single-writer.
    deliv = run_hosted_delivery_observe(source="hosted_workspace.scheduler")
    arm = run_hosted_auto_arm()
    return {"provisioning": prov, "observation": obs, "capability_recovery": recovery,
            "delivery": deliv, "auto_arm": arm}


class Command(BaseCommand):
    help = ("Beta Readiness G2/G15 scheduler: allocate pending workspace nodes + poll hosted observations. "
            "Dormant unless HOSTED_OBSERVATION_SCHEDULER_ENABLED (or --force); singleton; fail-closed; DARK.")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if HOSTED_OBSERVATION_SCHEDULER_ENABLED is false (controlled validation).")

    def _fast_onboarding_repoll(self) -> None:
        """Re-observe ONLY pre-CONNECTED (onboarding) tenants quickly for a bounded budget, so a fresh login is
        detected in <=~30s rather than up to the 60s cron cadence. Observe-only + bounded; stops early once no
        tenant is actively onboarding. Never raises into the scheduler (fail-open)."""
        import time as _time
        from hosted_workspace.bounded_observation import ONBOARDING_STATES, run_bounded_observation_cycle
        from hosted_workspace.models import HostedMt5Workspace
        interval, budget = _repoll_interval_s(), _repoll_budget_s()
        deadline = _time.monotonic() + budget
        passes = 0
        while _time.monotonic() < deadline:
            if not HostedMt5Workspace.objects.filter(canonical_state__in=list(ONBOARDING_STATES)).exists():
                break                                       # nobody actively onboarding → stop (no wasted sleeps)
            _time.sleep(min(interval, max(0.0, deadline - _time.monotonic())))
            try:
                run_bounded_observation_cycle(source="hosted_workspace.scheduler.repoll",
                                              only_states=ONBOARDING_STATES)
                passes += 1
            except Exception:  # noqa: BLE001 — a repoll failure must never break the scheduler invocation
                break
        if passes:
            self.stdout.write(f"[run_hosted_observations] fast onboarding re-poll passes={passes}")

    def handle(self, *args, **opts):
        now = timezone.now()
        if not hosted_observation_scheduler_enabled() and not opts["force"]:
            self.stdout.write(f"[run_hosted_observations] dormant "
                              f"(HOSTED_OBSERVATION_SCHEDULER_ENABLED=false) at {now.isoformat()}")
            return

        if not try_acquire_singleton():
            self.stdout.write(f"[run_hosted_observations] skipped — another cycle holds the "
                              f"singleton lock at {now.isoformat()}")
            return
        try:
            result = run_cycle()
            # P0 fast onboarding re-poll (flag ON): keep login→CONNECTED detection <=~30s for tenants ACTIVELY
            # awaiting it, WITHOUT re-observing stable tenants every few seconds. Observe-only (provisioning/
            # recovery/arm already ran once above); bounded interval + budget so the whole invocation stays well
            # under the 60s cron cadence — the lock is released before the next tick, so cycles never overlap.
            from hosted_workspace.flags import hosted_bounded_observation_enabled
            if hosted_bounded_observation_enabled() and not opts["force"]:
                self._fast_onboarding_repoll()
        finally:
            release_singleton()

        p, o, a = result["provisioning"], result["observation"], result["auto_arm"]
        d = result["delivery"]
        # P0 bounded-observation telemetry (§8/§9): worker count + typed unavailable reasons make cycle health and
        # overlap observable in the ops log; recovery's onboarding-skip/relaunch counts prove an observe failure
        # never relaunches a tenant. Both sections appear ONLY on the bounded path (legacy line is unchanged).
        bounded_txt = rec_txt = ""
        b = result.get("bounded")
        if b is not None:   # bounded path ONLY — the legacy (flag-off) line stays byte-identical
            bounded_txt = f" | bounded: workers={b['workers']} reasons={b['reasons']}"
            rec = result.get("capability_recovery") or {}
            rec_txt = (f" | recovery: candidates={rec.get('candidates', 0)} attempted={rec.get('attempted', 0)} "
                       f"relaunched={rec.get('relaunched', 0)} skipped_onboarding={rec.get('skipped_onboarding', 0)}")
        self.stdout.write(
            f"[run_hosted_observations] {now.isoformat()} "
            f"prov: enabled={p['enabled']} candidates={p['candidates']} allocated={p['allocated']} "
            f"already={p['already']} no_capacity={p['no_capacity']} not_deliverable={p['not_deliverable']} "
            f"cz_forbidden={p['cz_forbidden']} slot_prep_failed={p['slot_prep_failed']} "
            f"observer_prep_failed={p['observer_prep_failed']} errors={p['errors']} | "
            f"obs: enabled={o['enabled']} polled={o['polled']} "
            f"applied={o['applied']} unavailable={o['unavailable']} errors={o['errors']} | "
            f"deliv: enabled={d['enabled']} polled={d['polled']} connected={d['connected']} "
            f"disconnected={d['disconnected']} held={d['held']} cz_skipped={d['cz_skipped']} errors={d['errors']} | "
            f"arm: enabled={a['enabled']} candidates={a['candidates']} armed={a['armed']} "
            f"refused={a['refused']} errors={a['errors']}"
            f"{rec_txt}{bounded_txt}"
        )
