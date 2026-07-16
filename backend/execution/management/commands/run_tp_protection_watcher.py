"""
GFX-PKT-TP-PROTECTION-LATENCY — dedicated adaptive TP-protection watcher for ti_signals.

WHY: production evidence (plan 33) showed the incremental TP-protection ladder is CORRECT but slow —
its reaction is bounded by the ~1-minute monitor cadence and, worse, by position-ingestion going
BLIND for minutes when a SYNC job strands (a hung/recycled worker holds its lease). This command cuts
the reaction latency to ~1 second DURING the narrow protection window, WITHOUT the blast radius of
running the whole monitor chain every second.

WHAT IT IS: a thin, adaptive DRIVER over the EXISTING protection state machine
(``execution.breakeven.sweep_breakeven``). It adds NO new protection logic and NO new MT5 egress. It
reuses ExecutionJob + the ingest worker + the bridge exactly as the minute chain does — it is
enqueue-only and holds NO MT5/bridge credentials.

Design (each property required by the packet):
* **ti_signals ONLY** — ``sweep_breakeven(sources={"ti_signals"})``. Wayond is never touched; it stays
  on the minute chain, unchanged.
* **Adaptive cadence** — idle (~30s) when no eligible ti_signals plan is open; pre-TP (~3s) when a
  plan is open before any protection is due; active (~1s) once a protection stage is due / in-flight /
  softly deferred; back to idle once all positions close.
* **Single-flight** — a Postgres SESSION advisory lock so only ONE watcher ever acts. A duplicate
  start simply idles and retries; the lock releases automatically if the process dies.
* **Self-healing ingestion** — each tick it also reclaims lease-expired RUNNING SYNC/MODIFY jobs (the
  existing idempotent reclaim). Combined with the short protection-sync lease, a stranded sync frees
  ingestion within ~a minute instead of blocking the ladder for many minutes.
* **Fallback** — the minute monitor chain STILL runs the same sweep as the slower reconciliation net.
  If this watcher stops, protection continues (just slower). Nothing here is load-bearing-once-only.
* **Fail-safe** — every tick is wrapped; a transient DB/logic error is logged and the loop continues.
* **Bounded MODIFY load** — the ladder's own idempotency (per-(ticket,stage) in-flight guard +
  monotonic ``protection_stage``) means a faster poll NEVER enqueues a duplicate MODIFY, so the
  risk-bearing SL-edit bridge calls do NOT scale with cadence. (The position-SYNC/deals-fetch IS
  intentionally driven faster in the HOT window — that is the whole point: detect a TP close within
  ~1s instead of ~1min — but it is bounded to <=1 in-flight sync per account by
  ``_ensure_position_sync`` and it places no order.)
* **Observability** — a heartbeat (source ``tp_protection_watcher``) with cadence/state each tick.

Inert unless ``BREAKEVEN_ENABLED`` (the ladder arm) AND ``TP_WATCHER_ENABLED`` (this watcher's arm).
``--once`` runs a single tick (debug); ``--dry-run`` runs the tick inside a rolled-back transaction so
it exercises the real query load and cadence decision while persisting NOTHING (resource benchmark).
"""
import json
import logging
import os
import signal
import time

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, OperationalError, connections, transaction
from django.utils import timezone

logger = logging.getLogger("guvfx.execution.tp_watcher")

WATCHER_SOURCE = "tp_protection_watcher"
WATCHER_SOURCES = {"ti_signals"}
# Stable arbitrary 63-bit key for the cluster-wide single-flight advisory lock.
_ADVISORY_LOCK_KEY = 778866553311


def _f(env, default):
    try:
        return float(os.getenv(env, default))
    except Exception:
        return float(default)


def watcher_enabled() -> bool:
    return os.getenv("TP_WATCHER_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


class Command(BaseCommand):
    help = ("Adaptive 1s-in-window TP-protection watcher for ti_signals (drives the existing ladder; "
            "enqueue-only, single-flight, self-healing, minute-chain remains the fallback).")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Run a single tick and exit (prints JSON metrics; debug/benchmark).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Run the tick in a ROLLED-BACK transaction — real query load, zero "
                                 "persisted writes (resource benchmark; no order/trade/notification).")

    def handle(self, *args, **opts):
        self._stop = False
        idle = _f("TP_WATCHER_IDLE_INTERVAL", "30")
        pre = _f("TP_WATCHER_PRE_INTERVAL", "3")
        active = _f("TP_WATCHER_ACTIVE_INTERVAL", "1")

        if opts["once"]:
            metrics = self._measured_tick(dry_run=opts["dry_run"], idle=idle, pre=pre, active=active)
            self.stdout.write(json.dumps(metrics))
            return

        signal.signal(signal.SIGTERM, self._on_sigterm)
        signal.signal(signal.SIGINT, self._on_sigterm)
        logger.info("tp_watcher: starting (idle=%ss pre=%ss active=%ss enabled=%s)",
                    idle, pre, active, watcher_enabled())
        while not self._stop:
            if not self._acquire_lock():
                logger.info("tp_watcher: another instance holds the single-flight lock — idling")
                self._sleep(idle)
                continue
            try:
                self._loop(idle, pre, active, dry_run=opts["dry_run"])
            finally:
                self._release_lock()

    # -- main loop ----------------------------------------------------------
    def _loop(self, idle, pre, active, *, dry_run):
        logger.info("tp_watcher: single-flight lock acquired — adaptive loop running")
        while not self._stop:
            try:
                cadence = self._tick(dry_run=dry_run, idle=idle, pre=pre, active=active)
            except OperationalError:
                # A bare management-command loop never fires Django's request signals, so a dropped
                # connection is NOT auto-recycled: ensure_connection() only reconnects when
                # ``self.connection is None`` — which only ``close()`` sets. Without this explicit
                # close the broken connection is reused forever and the watcher wedges (every cursor
                # raises, so even re-acquiring the lock fails). Close it → the next connect() is fresh.
                logger.exception("tp_watcher: DB error — closing connection and re-acquiring lock")
                try:
                    connections[DEFAULT_DB_ALIAS].close()
                except Exception:
                    pass
                return  # leave the loop → release (best-effort) + re-acquire on a fresh connection
            except Exception:
                logger.exception("tp_watcher: tick failed (continuing)")
                cadence = idle
            self._sleep(cadence)

    def _measured_tick(self, *, dry_run, idle, pre, active):
        """One tick with timing + query-count for the benchmark (--once)."""
        from django.test.utils import CaptureQueriesContext
        t0 = time.monotonic()
        with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as ctx:
            cadence = self._tick(dry_run=dry_run, idle=idle, pre=pre, active=active)
        return {"cadence_s": cadence, "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "queries": len(ctx.captured_queries), "dry_run": bool(dry_run),
                "enabled": watcher_enabled()}

    def _tick(self, *, dry_run, idle, pre, active):
        if dry_run:
            # Exercise the real load, persist nothing.
            with transaction.atomic():
                cadence = self._do_tick(idle, pre, active)
                transaction.set_rollback(True)
            return cadence
        return self._do_tick(idle, pre, active)

    def _do_tick(self, idle, pre, active):
        from execution.breakeven import breakeven_enabled, sweep_breakeven
        from execution.execution_health import (reclaim_orphaned_modify_jobs,
                                                 reclaim_orphaned_sync_jobs)
        from reliability.services.heartbeat import record_beat
        now = timezone.now()
        if not watcher_enabled() or not breakeven_enabled():
            record_beat(WATCHER_SOURCE, interval_s=max(int(idle) * 3, 90),
                        detail={"state": "disabled", "cadence_s": idle})
            return idle
        # Self-heal ingestion FIRST so this same sweep can see a just-freed close.
        reclaimed = reclaim_orphaned_sync_jobs(now) + reclaim_orphaned_modify_jobs(now)
        res = sweep_breakeven(sources=WATCHER_SOURCES)
        open_legs = res.get("open_legs", 0)
        advanced = res.get("advanced_open_legs", 0)
        hot = bool(advanced or res.get("enqueued") or res.get("inflight") or res.get("deferred"))
        state = "active" if (open_legs and hot) else ("pre" if open_legs else "idle")
        cadence = active if state == "active" else (pre if state == "pre" else idle)
        record_beat(WATCHER_SOURCE, interval_s=max(int(cadence) * 3, 5), detail={
            "state": state, "cadence_s": cadence, "open_legs": open_legs,
            "advanced_open_legs": advanced, "enqueued": res.get("enqueued"),
            "deferred": res.get("deferred"), "tp2_locked": res.get("tp2_locked"),
            "superseded": res.get("superseded"), "reclaimed": reclaimed})
        # Log only on a STATE TRANSITION or when something actually happened — NOT every active tick
        # (at 1s that would grow the log by thousands of lines per protected position). Steady-state
        # ticks are silent; the heartbeat detail above carries the per-tick state for /operations.
        did_something = bool(res.get("enqueued") or res.get("deferred") or res.get("applied")
                             or res.get("superseded") or reclaimed)
        if state != getattr(self, "_last_state", None) or did_something:
            logger.info("tp_watcher: state=%s cadence=%ss open=%s advanced=%s enq=%s def=%s applied=%s reclaimed=%s",
                        state, cadence, open_legs, advanced, res.get("enqueued"),
                        res.get("deferred"), res.get("applied"), reclaimed)
        self._last_state = state
        return cadence

    # -- single-flight advisory lock ---------------------------------------
    def _acquire_lock(self) -> bool:
        try:
            with connections[DEFAULT_DB_ALIAS].cursor() as c:
                c.execute("SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_KEY])
                return bool(c.fetchone()[0])
        except Exception:
            logger.exception("tp_watcher: advisory-lock acquire failed")
            return False

    def _release_lock(self):
        try:
            with connections[DEFAULT_DB_ALIAS].cursor() as c:
                c.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_KEY])
        except Exception:  # pragma: no cover - best-effort; the lock also frees on session end
            pass

    # -- lifecycle ----------------------------------------------------------
    def _on_sigterm(self, *_):
        logger.info("tp_watcher: signal received — stopping after this tick")
        self._stop = True

    def _sleep(self, seconds):
        deadline = time.monotonic() + max(0.0, float(seconds))
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
