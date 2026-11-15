"""hosted_workspace.recovery_acceptance — bounded acceptance RUNNER core with GUARANTEED teardown (packet-127).

Packet-126 was torn down correctly only because a human watched it. This module makes the bounded single-account
acceptance self-cleaning: whatever happens — PASS, FAIL, TIMEOUT, or an exception mid-run — the ``finally`` block
restores the byte-exact baseline, recreates the backend, confirms both recovery flags read OFF, and releases the
lease. A failure to tear down raises an operator ALERT and leaves the lease to self-expire (TTL); it NEVER silently
leaves recovery flags enabled.

The host I/O (read/write ``beta.env``, recreate the backend, watch the cron, raise alerts) is injected via the
``HostOps`` interface, so the guarantee is unit-testable with a fake: the tests drive every exit path and assert the
baseline is restored and the lease released on each. The real host wrapper (``scripts/recovery_acceptance_runner.py``)
supplies a ``HostOps`` that shells to ``docker compose`` + ``beta.env`` and uses the ``recovery_ops`` CLI for the lease.

This module arms NOTHING by itself in production: it is a runner invoked deliberately for a bounded window, and its
arm step only writes a conservative, allow-list-of-one, breaker-1/1 configuration that the recovery code still gates
behind its own fail-closed checks.
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from hosted_workspace import recovery_lease

OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"
OUTCOME_TIMEOUT = "TIMEOUT"
OUTCOME_ERROR = "ERROR"
OUTCOME_BLOCKED = "BLOCKED"

_ARM_MARKER = "# --- GUVFX bounded recovery acceptance (packet-127 runner; auto-removed on teardown) ---"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_armed_env(baseline: str, account_id: int, *, stale_s: int | None = None) -> str:
    """Return the baseline ``beta.env`` content with a single conservative acceptance block appended: established +
    LIVE recovery ON, allow-list of EXACTLY this account, and the fail-closed host breaker pinned to 1 down / 1
    relaunch. Optionally a faster (never < 300s) stale threshold. Teardown restores the baseline byte-exact, so this
    block never survives a completed run."""
    lines = [
        _ARM_MARKER,
        "HOSTED_ESTABLISHED_RECOVERY_ENABLED=1",
        "HOSTED_LIVE_RECOVERY_ENABLED=1",
        f"HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS={int(account_id)}",
        "HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN=1",
        "HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES=1",
    ]
    if stale_s is not None:
        # Clamp defensively to the hard floor; the recovery code also rejects < 300 fail-closed.
        safe = max(recovery_lease.TTL_MIN_S, int(stale_s))
        if safe < 300:
            safe = 900
        lines.append(f"HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S={safe}")
    sep = "" if baseline.endswith("\n") else "\n"
    return baseline + sep + "\n".join(lines) + "\n"


class HostOps(abc.ABC):
    """Injected host I/O. All methods are side-effecting on the real host; the fake used in tests records calls."""

    @abc.abstractmethod
    def now(self) -> datetime: ...

    @abc.abstractmethod
    def read_env(self) -> str: ...

    @abc.abstractmethod
    def write_env(self, content: str) -> None: ...

    @abc.abstractmethod
    def recreate_backend(self) -> None: ...

    @abc.abstractmethod
    def flags_off(self) -> bool:
        """True iff BOTH established and live recovery flags read OFF from the live backend."""

    @abc.abstractmethod
    def observe(self, account_id: int, deadline: datetime) -> str:
        """Watch to a terminal outcome: OUTCOME_PASS / OUTCOME_FAIL / OUTCOME_TIMEOUT."""

    @abc.abstractmethod
    def alert(self, title: str, detail: dict) -> None:
        """Raise an operator-visible alert (no secret content)."""


@dataclass
class AcceptanceResult:
    outcome: str
    armed: bool
    teardown_ok: bool
    flags_off: object            # bool | None
    baseline_sha: str | None
    final_sha: str | None
    lease_correlation: str
    reason: str
    detail: dict = field(default_factory=dict)

    def ok(self) -> bool:
        return self.outcome == OUTCOME_PASS and self.teardown_ok


def _teardown(ops: HostOps, baseline: str | None, baseline_sha: str | None, correlation_id: str,
              lease_path: str | None, env_dirty: bool) -> tuple[bool, str | None, object]:
    """GUARANTEED cleanup. Restores the byte-exact baseline whenever beta.env was mutated (``env_dirty`` — set the
    instant ``write_env`` is called, NOT after the recreate), confirms flags OFF, releases the lease. On ANY failure
    it ALERTS and returns False — the lease then self-expires via its TTL; recovery flags are NEVER left silently
    enabled, because the restore is driven by 'the env was written', so a recreate failure right after an arm write
    still forces the restore."""
    final_sha = None
    flags_off: object = None
    try:
        restored = True
        if env_dirty and baseline is not None:
            ops.write_env(baseline)
            ops.recreate_backend()
            final = ops.read_env()
            final_sha = _sha(final)
            restored = (final_sha == baseline_sha)
        flags_off = ops.flags_off()
        released, rel_reason = recovery_lease.release(correlation_id, ops.now(), lease_path)
        ok = bool(restored) and (flags_off is True) and bool(released)
        if not ok:
            ops.alert("recovery_acceptance teardown INCOMPLETE",
                      {"restored": restored, "flags_off": flags_off, "released": released,
                       "release_reason": rel_reason, "correlation_id": correlation_id})
        return ok, final_sha, flags_off
    except Exception as exc:  # noqa: BLE001 — teardown must never raise; alert + leave lease to self-expire.
        ops.alert("recovery_acceptance teardown FAILED",
                  {"error": type(exc).__name__, "correlation_id": correlation_id,
                   "note": "lease left to self-expire via TTL; investigate flags immediately"})
        return False, final_sha, flags_off


def run_bounded_acceptance(ops: HostOps, account_id: int, *, ttl_s: int, purpose: str, owner: str,
                           correlation_id: str, lease_path: str | None = None, stale_s: int | None = None,
                           watch_s: int | None = None) -> AcceptanceResult:
    """Run one bounded single-account acceptance with guaranteed teardown.

    Refuses (OUTCOME_BLOCKED, no arm, no lease held) if a conflicting lease is live or the lease is malformed.
    Otherwise acquires a recovery lease, captures the byte-exact baseline, arms ONLY ``account_id``, watches to
    PASS/FAIL/TIMEOUT, and ALWAYS tears down in ``finally``."""
    now = ops.now()
    blocked, reason = recovery_lease.blocks(recovery_lease.KIND_RECOVERY, now, lease_path)
    if blocked:
        return AcceptanceResult(outcome=OUTCOME_BLOCKED, armed=False, teardown_ok=True, flags_off=None,
                                baseline_sha=None, final_sha=None, lease_correlation=correlation_id,
                                reason=f"blocked:{reason}")
    ok, lease_or_reason = recovery_lease.acquire(
        recovery_lease.KIND_RECOVERY, purpose, owner, correlation_id, ttl_s, now, lease_path)
    if not ok:
        return AcceptanceResult(outcome=OUTCOME_BLOCKED, armed=False, teardown_ok=True, flags_off=None,
                                baseline_sha=None, final_sha=None, lease_correlation=correlation_id,
                                reason=f"acquire_refused:{lease_or_reason}")

    baseline = None
    baseline_sha = None
    env_dirty = False       # becomes True the INSTANT beta.env is written — drives the guaranteed restore.
    armed = False           # the full arm sequence (write + recreate) completed — reported, but NOT the restore gate.
    outcome = OUTCOME_ERROR
    detail: dict = {}
    try:
        baseline = ops.read_env()
        baseline_sha = _sha(baseline)
        env_dirty = True    # set BEFORE the write, so a failure in write_env/recreate_backend still forces teardown.
        ops.write_env(build_armed_env(baseline, account_id, stale_s=stale_s))
        ops.recreate_backend()
        armed = True
        deadline = ops.now() + timedelta(seconds=int(watch_s if watch_s is not None else ttl_s))
        outcome = ops.observe(account_id, deadline)
    except Exception as exc:  # noqa: BLE001
        outcome = OUTCOME_ERROR
        detail["exception"] = type(exc).__name__
    finally:
        teardown_ok, final_sha, flags_off = _teardown(
            ops, baseline, baseline_sha, correlation_id, lease_path, env_dirty)

    return AcceptanceResult(outcome=outcome, armed=armed, teardown_ok=teardown_ok, flags_off=flags_off,
                            baseline_sha=baseline_sha, final_sha=final_sha, lease_correlation=correlation_id,
                            reason=reason, detail=detail)
