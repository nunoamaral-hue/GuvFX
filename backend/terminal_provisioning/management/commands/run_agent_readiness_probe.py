"""Monitoring-Runner WS-B/D — ONE validation-agent readiness monitor pass, safe to run from cron.

    python manage.py run_agent_readiness_probe                 # one pass (probe -> hysteresis -> alerts)
    python manage.py run_agent_readiness_probe --dry-run       # evaluate + persist state, but DELIVER nothing
    python manage.py run_agent_readiness_probe --synthetic-state UNREACHABLE   # test the pipeline, no network
    python manage.py run_agent_readiness_probe --json          # machine-readable outcome on stdout

Contract (see ``agent_monitor_runner``): read-only w.r.t. the agent/estate; the only side effects are the
signed-NEGOTIATE probe, a write to the singleton ``AgentMonitorState`` row, and — when an alert fires — a
message to the configured ops alert sink (DARK by default: sink=null). It performs NO broker validation,
touches NO credential, creates NO attempt, starts NO MT5, and reads/writes NO customer account.

Single-flight: the run takes an exclusive row lock on the ``AgentMonitorState`` singleton
(``select_for_update(nowait=True)``); a second concurrent run refuses immediately with exit 50 rather than
double-probing or racing the durable state. Disabled monitoring is inert (exit 0). Deterministic exit codes:

    0  healthy | disabled            10 agent-unhealthy-but-ran     20 config-error
    30 probe-failure                 40 alert-delivery-failure       50 overlap-refused
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, transaction

from terminal_provisioning import agent_health_probe as probe
from terminal_provisioning import agent_monitor_runner as runner
from terminal_provisioning.agent_alert_sink import NullAlertSink, build_alert_sink
from terminal_provisioning.models import AgentMonitorState

EXIT_OVERLAP_REFUSED = 50


class Command(BaseCommand):
    help = "Run one validation-agent readiness monitor pass (probe -> hysteresis -> alert delivery)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="evaluate + persist state, but route delivery to the NULL sink (send nothing)")
        parser.add_argument("--synthetic-state", default="",
                            help="test-only: skip the network probe and inject this readiness STATE "
                                 f"(one of: {', '.join(probe.STATES)})")
        parser.add_argument("--json", action="store_true", help="emit the machine-readable outcome on stdout")

    def handle(self, *args, **o):
        cfg = runner.load_config()
        # Disabled posture is inert and quiet (exit 0) — the cron can be installed before the flag is armed.
        if not cfg.enabled:
            out = runner.RunOutcome(status=runner.STATUS_DISABLED,
                                    reason="VALIDATION_AGENT_MONITORING_ENABLED is off")
            self._emit(out, o, note="monitoring disabled (inert)")
            return

        sink = NullAlertSink() if o["dry_run"] else build_alert_sink()
        now = time.time()

        # ── test-only synthetic path: NEVER touches the durable singleton, so it cannot mask a real outage
        # or fabricate a RECOVERED all-clear in the shared row the cron + ops dashboard read. It runs on a
        # throwaway in-memory state and is never persisted, and takes no lock (it shares no row). ──
        synthetic = self._build_synthetic(o.get("synthetic_state") or "")
        if synthetic is not None:
            outcome = runner.run_once(state=AgentMonitorState(), sink=sink, now=now, config=cfg,
                                      synthetic_readiness=synthetic)
            self._emit(outcome, o, note="synthetic (in-memory, not persisted)")
            return

        try:
            with transaction.atomic():
                # ensure the singleton exists, then take the exclusive lock (fail-fast if held).
                AgentMonitorState.objects.get_or_create(pk=AgentMonitorState.SINGLETON_ID)
                try:
                    state = (AgentMonitorState.objects
                             .select_for_update(nowait=True)
                             .get(pk=AgentMonitorState.SINGLETON_ID))
                except DatabaseError as exc:
                    # ONLY a nowait lock-contention error is an overlap; any other DB error is a real monitor
                    # failure and must surface as probe-failure (30), not be masked as a routine overlap.
                    if not _is_lock_not_available(exc):
                        raise
                    self.stderr.write("another readiness probe holds the lock — refusing (overlap)")
                    raise _Overlap()
                outcome = runner.run_once(state=state, sink=sink, now=now, config=cfg)
                state.save()
        except _Overlap:
            # write output BEFORE raising the exit code (SystemExit would skip the JSON otherwise).
            if o["json"]:
                self.stdout.write(json.dumps({"status": "overlap_refused", "exit_code": EXIT_OVERLAP_REFUSED}))
            else:
                self.stdout.write(f"status=overlap_refused exit={EXIT_OVERLAP_REFUSED}")
            self._set_exit(EXIT_OVERLAP_REFUSED)
            return
        except Exception as exc:  # noqa: BLE001 — never crash the cron; map to probe-failure (30)
            self.stderr.write(f"monitor run failed: {type(exc).__name__}")
            out = runner.RunOutcome(status=runner.STATUS_PROBE_FAILURE, reason=type(exc).__name__)
            self._emit(out, o)
            return

        self._emit(outcome, o, note=("dry-run (no delivery)" if o["dry_run"] else ""))

    def _build_synthetic(self, state_name: str):
        if not state_name:
            return None
        state_name = state_name.strip().upper()
        if state_name not in probe.STATES:
            self.stderr.write(f"unknown --synthetic-state {state_name!r}; ignoring (real probe will run)")
            return None
        band = probe._BAND.get(state_name, probe.BAND_UNAVAILABLE)
        supervised = True if state_name in (probe.HEALTHY, probe.READY_UNARMED) else (
            False if state_name == probe.UNSUPERVISED else None)
        return probe.AgentReadiness(
            state=state_name, band=band, supervised=supervised,
            validate_login_available=(state_name == probe.HEALTHY),
            reason=f"synthetic_{state_name.lower()}", correlation_id="synthetic",
            elapsed_ms=0, probed_at=time.time(), layers={"synthetic": True})

    def _emit(self, outcome, o, note: str = ""):
        if o["json"]:
            self.stdout.write(json.dumps(outcome.as_dict()))
        else:
            tail = f" [{note}]" if note else ""
            self.stdout.write(
                f"status={outcome.status} exit={outcome.exit_code} band={outcome.band} "
                f"state={outcome.state} fired={len(outcome.alerts_fired)} "
                f"delivered={outcome.alerts_delivered} failed={outcome.alerts_failed}{tail}")
        self._set_exit(outcome.exit_code)

    def _set_exit(self, code: int):
        # BaseCommand maps a returned non-str/None to exit via SystemExit only if we raise; use the documented
        # pattern: stash on self and raise SystemExit for a non-zero code so cron sees it.
        if code:
            raise SystemExit(code)


class _Overlap(Exception):
    pass


def _is_lock_not_available(exc) -> bool:
    """True only for the PostgreSQL ``select_for_update(nowait=True)`` lock-contention error (SQLSTATE 55P03),
    so a dropped/reset connection or statement timeout on the locked ``.get()`` is NOT misreported as a
    routine overlap. Falls back to a message match for backends that don't expose ``pgcode``."""
    cause = getattr(exc, "__cause__", None)
    pgcode = getattr(cause, "pgcode", None) or getattr(exc, "pgcode", None)
    if pgcode == "55P03":
        return True
    msg = str(exc).lower()
    return "could not obtain lock" in msg or "lock not available" in msg or "locknotavailable" in msg
