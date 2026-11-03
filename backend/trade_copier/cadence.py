"""trade_copier.cadence — the bounded reconciliation-cadence + detection-latency model (ADR-0071).

PURE (no Django, no I/O, no wall clock except an injected ``now``/``rng``). The hybrid design pairs the fast
EVENT path (``trade_copier.revisions``) with a RAPID reconciliation poll as the correctness backstop. This
module is the *planner* for that poll — it decides how often to re-read and reports the observed latency — but
it never performs a read and never places an order.

Design rules enforced here (packet GFX-COPIER-HYBRID-CHANGE-001 §"Reconciliation path"):

* **Bounded configuration, never hard-coded timing.** A ``ReconciliationCadence`` carries an explicit
  ``[min, max]`` and a base interval; ``plan_next_delay`` always returns a value inside the band.
* **Safe degradation, not request multiplication.** Consecutive failures *lengthen* the interval
  (exponential backoff capped at ``max``); they never shorten it or fan out more reads.
* **Jitter / coordination.** A bounded ±jitter de-correlates many tenants that share one host so they do not
  stampede the bridge on the same tick.
* **Overlap prevention is NOT re-implemented here.** It already exists as the observer's single-flight
  observation lease (``trade_copier.observer._claim_observation_lease``); this planner only chooses the delay
  until the next *attempt*, and a still-in-flight observation is skipped by that lease. See ``OVERLAP_NOTE``.
* **Sub-second only when MEASURED.** The default floor is deliberately NOT sub-second. Sub-second cadence is
  permitted only when a ``CapacityProfile`` built from a REAL measurement authorises it (no-assumption rule:
  ``.claude/rules`` — we do not assume the bridge/host can sustain sub-second polling; we require evidence).

Nothing here is on the execution path; the cadence is inert until ``COPIER_LIFECYCLE_ENABLED`` /
``COPIER_RECONCILE_ENABLED`` arm the passes that consume it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

# The hard sub-second boundary. A cadence whose minimum interval is below this is "sub-second" and is only
# allowed against a measured capacity profile that authorises it.
SUBSECOND_FLOOR_MS = 1000

# Absolute safety rails independent of any config: never hammer faster than this, never sleep longer than this.
_ABS_MIN_INTERVAL_MS = 50
_ABS_MAX_INTERVAL_MS = 300_000   # 5 minutes

OVERLAP_NOTE = (
    "Overlap prevention is the observer's single-flight observation lease, not this planner: a second pass "
    "whose predecessor is still in flight is skipped (observation_in_flight) and reads fresh next tick. The "
    "planner only chooses the delay until the next attempt."
)


class CadenceError(ValueError):
    """A structurally-invalid cadence configuration (fail closed at construction — never a silent clamp)."""


@dataclass(frozen=True)
class CapacityProfile:
    """A MEASURED bridge/host capacity profile. Sub-second cadence is gated on one of these existing.

    Built only from a real measurement (e.g. an observed sample of position-read round-trips) — never
    assumed. An absent profile (``None``) forbids sub-second polling (fail-safe)."""
    sampled_reads: int                 # how many reads the measurement observed (0 ⇒ not a real measurement)
    p95_read_latency_ms: float         # observed 95th-percentile read round-trip
    max_safe_concurrent_reads: int     # concurrent reads the host sustained without degradation
    measured_at: Optional[datetime] = None

    def is_real(self) -> bool:
        return self.sampled_reads > 0 and self.p95_read_latency_ms > 0 and self.max_safe_concurrent_reads >= 1

    def authorises_subsecond(self, interval_ms: int) -> bool:
        """True only when a REAL measurement shows the host can sustain this interval with headroom: the
        interval must be comfortably longer than the measured p95 read (2x headroom) so a read completes well
        within its own cycle. A non-real profile authorises nothing."""
        if not self.is_real():
            return False
        return interval_ms >= 2.0 * float(self.p95_read_latency_ms)


@dataclass(frozen=True)
class ReconciliationCadence:
    """Bounded cadence configuration for the rapid reconciliation poll. Validated at construction."""
    base_interval_ms: int = 2000        # conservative default: 2s between reconciliation reads
    min_interval_ms: int = 1000         # floor == SUBSECOND_FLOOR_MS by default (NOT sub-second)
    max_interval_ms: int = 60_000       # degrade ceiling: 60s
    jitter_frac: float = 0.2            # ±20% de-correlation
    backoff_factor: float = 2.0         # interval *= backoff_factor per consecutive failure
    degrade_after_failures: int = 1     # start backing off from the first failure

    def __post_init__(self):
        if not (_ABS_MIN_INTERVAL_MS <= self.min_interval_ms <= self.max_interval_ms <= _ABS_MAX_INTERVAL_MS):
            raise CadenceError("interval bounds out of order or outside absolute rails")
        if not (self.min_interval_ms <= self.base_interval_ms <= self.max_interval_ms):
            raise CadenceError("base_interval_ms must lie within [min, max]")
        if not (0.0 <= self.jitter_frac < 1.0):
            raise CadenceError("jitter_frac must be in [0, 1)")
        if self.backoff_factor < 1.0:
            raise CadenceError("backoff_factor must be >= 1.0 (failures never shorten the interval)")
        if self.degrade_after_failures < 1:
            raise CadenceError("degrade_after_failures must be >= 1")

    def is_subsecond(self) -> bool:
        return self.min_interval_ms < SUBSECOND_FLOOR_MS

    def validate_against_capacity(self, capacity: Optional[CapacityProfile]) -> tuple[bool, str]:
        """A sub-second cadence is admissible only with a measured capacity profile that authorises its floor.
        A non-sub-second cadence is always admissible. Fail closed when sub-second without evidence."""
        if not self.is_subsecond():
            return True, "ok"
        if capacity is None or not capacity.is_real():
            return False, "subsecond_requires_measured_capacity"
        if not capacity.authorises_subsecond(self.min_interval_ms):
            return False, "subsecond_exceeds_measured_capacity"
        return True, "ok"


def _clamp(v: float, lo: int, hi: int) -> float:
    return max(float(lo), min(float(hi), float(v)))


def plan_next_delay(cadence: ReconciliationCadence, *, consecutive_failures: int = 0,
                    rng: Optional[Callable[[], float]] = None) -> int:
    """Return the delay (ms) until the next reconciliation read attempt.

    Deterministic when ``rng`` is supplied (``rng()`` must yield a float in ``[0, 1)``); otherwise jitter is
    omitted (the midpoint is returned) so a caller that wants reproducibility gets it and a caller that passes
    nothing still gets a safe, bounded value. Backoff: once ``consecutive_failures >= degrade_after_failures``
    the base interval is multiplied by ``backoff_factor`` per failure beyond the threshold, then clamped to
    ``[min, max]`` — so repeated failure *lengthens* the interval and can never fall below the floor or exceed
    the ceiling (no request storm, safe degradation)."""
    failures = max(0, int(consecutive_failures))
    interval = float(cadence.base_interval_ms)
    if failures >= cadence.degrade_after_failures:
        over = failures - cadence.degrade_after_failures + 1
        interval = float(cadence.base_interval_ms) * (cadence.backoff_factor ** over)
    interval = _clamp(interval, cadence.min_interval_ms, cadence.max_interval_ms)

    if cadence.jitter_frac > 0.0 and rng is not None:
        # Symmetric ±jitter_frac around the interval; r in [0,1) -> factor in [1-j, 1+j).
        r = float(rng())
        r = 0.0 if r < 0.0 else (0.999999 if r >= 1.0 else r)
        factor = (1.0 - cadence.jitter_frac) + (2.0 * cadence.jitter_frac) * r
        interval = _clamp(interval * factor, cadence.min_interval_ms, cadence.max_interval_ms)
    return int(round(interval))


# ──────────────────────────────── detection-latency measurement ────────────────────────────────

@dataclass(frozen=True)
class DetectionLatency:
    """A measured detection/apply latency for one lifecycle operation (open/modify/close), in ms.

    Honesty rule (``.claude/rules/evidence.md``): ``true_broker_latency`` is populated ONLY for the EVENT path,
    where the bridge supplies ``broker_event_time``. The POLL path cannot measure true broker-to-detection
    latency (no per-change broker timestamp); for it we report ``detect_to_apply_ms`` (detection → lifecycle
    apply) and mark ``source`` POLL + ``true_broker_latency`` None, rather than inventing a number."""
    source: str                        # "POLL" | "EVENT"
    detect_to_apply_ms: Optional[int]  # observed-at (detection) -> lifecycle applied-at
    true_broker_latency_ms: Optional[int]  # broker_event_time -> applied-at (EVENT path only; else None)


def _ms_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if start is None or end is None:
        return None
    delta_ms = int(round((end - start).total_seconds() * 1000.0))
    return delta_ms if delta_ms >= 0 else None     # a negative delta (clock skew / reorder) is not a latency


def measure_latency(*, source: str, observed_at: Optional[datetime], applied_at: Optional[datetime],
                    broker_event_time: Optional[datetime] = None) -> DetectionLatency:
    """Compute a sanitised detection latency. ``true_broker_latency_ms`` only when a broker event time is
    present (EVENT path); the POLL path leaves it None by construction."""
    return DetectionLatency(
        source=str(source),
        detect_to_apply_ms=_ms_between(observed_at, applied_at),
        true_broker_latency_ms=(_ms_between(broker_event_time, applied_at)
                                if broker_event_time is not None else None),
    )


# A conservative, non-sub-second default any caller can use until a measured capacity profile exists.
DEFAULT_CADENCE = ReconciliationCadence()
