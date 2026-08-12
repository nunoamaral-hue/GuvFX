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


def _dark_observe_fn(_workspace):
    """Fail-closed placeholder observe_fn for the repository-only phase: no host observe bridge exists yet, so
    every workspace observation is UNAVAILABLE (``None``) → nothing is ingested. Replaced by a real host
    observe bridge in a later host-certification increment."""
    return None


def resolve_observe_fn():
    """Return the observe_fn the cycle will use (pluggable seam). STREAM 9E: the REAL live host observe transport
    (``live_observe.live_observe_fn`` — signed ``OBSERVE_WORKSPACE`` → session-bound observer → certified
    producer) is selected ONLY when BOTH the trust anchor ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` (ADR-0041:
    observation is trustworthy only when a tenant cannot forge the handoff) AND ``HOSTED_MT5_OBSERVATION_ENABLED``
    are on; otherwise the fail-closed dark placeholder (no host bridge, ingests nothing). ``live_observe_fn`` is
    itself fail-closed on the SAME two gates + the separately-gated signed executor, so the darkness holds even
    if this resolver were bypassed."""
    from hosted_workspace.flags import (
        hosted_mt5_observation_enabled,
        hosted_remoteapp_isolation_certified,
    )
    if hosted_remoteapp_isolation_certified() and hosted_mt5_observation_enabled():
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
    """One provisioning+observation pass (NO flag gate, NO lock — the drivers self-gate on the master flag).
    Exposed for tests. Returns a combined secret-free summary."""
    from hosted_workspace.observation_runner import run_hosted_observations
    from hosted_workspace.provisioning_runner import run_workspace_provisioning
    prov = run_workspace_provisioning()
    obs = run_hosted_observations(observe_fn=observe_fn or resolve_observe_fn(),
                                  source="hosted_workspace.scheduler")
    return {"provisioning": prov, "observation": obs}


class Command(BaseCommand):
    help = ("Beta Readiness G2/G15 scheduler: allocate pending workspace nodes + poll hosted observations. "
            "Dormant unless HOSTED_OBSERVATION_SCHEDULER_ENABLED (or --force); singleton; fail-closed; DARK.")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if HOSTED_OBSERVATION_SCHEDULER_ENABLED is false (controlled validation).")

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
        finally:
            release_singleton()

        p, o = result["provisioning"], result["observation"]
        self.stdout.write(
            f"[run_hosted_observations] {now.isoformat()} "
            f"prov: enabled={p['enabled']} candidates={p['candidates']} allocated={p['allocated']} "
            f"already={p['already']} no_capacity={p['no_capacity']} not_deliverable={p['not_deliverable']} "
            f"errors={p['errors']} | obs: enabled={o['enabled']} polled={o['polled']} "
            f"applied={o['applied']} unavailable={o['unavailable']} errors={o['errors']}"
        )
