"""hosted_workspace.recovery_lease — governed, file-based, mutually-exclusive OPERATIONAL LEASE (packet-127).

WHY THIS EXISTS
---------------
Packet-126 proved a concurrent backend *deploy* can recreate the backend and reset ``beta.env`` mid-flight,
silently invalidating an in-progress bounded recovery/acceptance window. The advisory hold *file* used there
had no TTL, no correlation id, no audit, and no mutual-exclusion with deployment. This module replaces it with a
single-writer, fail-closed **operational lease** that BOTH deployment tooling and recovery/acceptance tooling
consult before a mutating action:

  * a **deploy** action is blocked while a **recovery** lease is live (unless an *audited* emergency override);
  * a **recovery** acceptance refuses to arm while a **deploy** lease is live;
  * only ONE lease is live at a time (a single lease file ⇒ mutual exclusion / fail-closed acquisition);
  * every lease carries owner / purpose / correlation-id / created / expiry and a bounded TTL;
  * an **EXPIRED** lease is safely reclaimable; a **MALFORMED** lease **FAILS CLOSED** (blocks, never auto-proceeds);
  * acquire / release / reclaim / override are appended to a no-secret JSON-lines audit log.

BOUNDARIES
----------
This is NOT a remote-command mechanism: it only reads/writes a small JSON *state* file and an audit log. It
stores NO secret — owner/purpose/correlation are free-text the caller keeps secret-free, and a name-deny guard
rejects obvious secret-bearing content defensively. Every function is import-safe without Django (stdlib-only at
module load); the Django ``settings`` default path is resolved lazily and tolerantly, so the Django-free deploy
gate and the in-container management command share this one source of truth.

The state file is deliberately simple and atomically written (temp + ``os.replace``) so a reader never observes a
half-written lease; a truncated/garbage file classifies as MALFORMED (fail-closed), never as ABSENT.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

# ── lease kinds (mutually exclusive) ──────────────────────────────────────────────────────────────────────────
KIND_RECOVERY = "recovery_acceptance"
KIND_DEPLOY = "deploy"
# ADR-0117: the governed copier execution driver's single-flight lease. It uses its OWN lease FILE (a distinct
# path passed to acquire/release), so it is independent of — never mutually exclusive with — the deploy/recovery
# operational lease; the kind is validated here for audit + fail-closed symmetry.
KIND_COPIER_DRIVER = "copier_execution_driver"
_VALID_KINDS = (KIND_RECOVERY, KIND_DEPLOY, KIND_COPIER_DRIVER)

# ── bounded TTL (seconds) — a lease can never be unbounded; it always self-expires ──────────────────────────────
TTL_MIN_S = 60
TTL_MAX_S = 7200          # 2h hard ceiling — longer than any bounded pilot, short enough to self-heal a crash
TTL_DEFAULT_S = 1800      # 30 min

_LEASE_FILENAME = "recovery_operational_lease.json"
_AUDIT_FILENAME = "recovery_operational_lease_audit.jsonl"

# Defence-in-depth: reject obvious secret-bearing content in free-text lease fields. Matches a NAME token (not a
# value) immediately before ``=`` OR a long base64/hex-looking run. The lease must never carry a secret.
_SECRET_NAME_RE = re.compile(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|KEYRING|PRIVATE)\s*=")
_LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


class LeaseStatus(str, Enum):
    ABSENT = "ABSENT"        # no lease file
    ACTIVE = "ACTIVE"        # valid + not expired
    EXPIRED = "EXPIRED"      # valid + past expiry → reclaimable
    MALFORMED = "MALFORMED"  # unreadable/invalid → FAIL CLOSED (blocks)


@dataclass(frozen=True)
class Lease:
    kind: str
    purpose: str
    owner: str
    correlation_id: str
    created_at: str          # ISO-8601 UTC
    expires_at: str          # ISO-8601 UTC

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "purpose": self.purpose, "owner": self.owner,
            "correlation_id": self.correlation_id, "created_at": self.created_at, "expires_at": self.expires_at,
        }

    def remaining_s(self, now: datetime) -> int:
        try:
            exp = _parse_iso(self.expires_at)
            return int((exp - now).total_seconds())
        except Exception:  # noqa: BLE001
            return 0


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _secret_free(*values: str) -> bool:
    """True iff none of the free-text values look secret-bearing (defence in depth; the lease must carry no secret)."""
    for v in values:
        s = str(v or "")
        if _SECRET_NAME_RE.search(s) or _LONG_OPAQUE_RE.search(s):
            return False
    return True


def default_lease_dir() -> str:
    """Resolve the lease directory tolerantly (configurable; Django-optional).

    Order: explicit setting/env ``GUVFX_RECOVERY_LEASE_DIR`` → the backend handoff mount (``BASE_DIR/.guvfx_handoff``,
    which is ``/app/.guvfx_handoff`` in the container) → a last-resort temp dir. NEVER a hard-coded home path."""
    env = os.getenv("GUVFX_RECOVERY_LEASE_DIR")
    if env:
        return env
    try:  # Django-optional: resolve the handoff mount under BASE_DIR when settings are importable.
        from django.conf import settings
        base = getattr(settings, "GUVFX_RECOVERY_LEASE_DIR", None)
        if base:
            return str(base)
        base_dir = getattr(settings, "BASE_DIR", None)
        if base_dir:
            return os.path.join(str(base_dir), ".guvfx_handoff")
    except Exception:  # noqa: BLE001 — Django not configured (host gate / tests passing explicit paths)
        pass
    return os.path.join(tempfile.gettempdir(), "guvfx_handoff")


def lease_path(path: str | None = None) -> str:
    if path:
        return path
    return os.path.join(default_lease_dir(), _LEASE_FILENAME)


def audit_path(path: str | None = None) -> str:
    if path:
        # audit lives beside the lease file
        return os.path.join(os.path.dirname(os.path.abspath(path)) or ".", _AUDIT_FILENAME)
    return os.path.join(default_lease_dir(), _AUDIT_FILENAME)


def _validate_lease_dict(d: dict) -> Lease | None:
    """Strict schema validation → Lease, or None if invalid (⇒ MALFORMED)."""
    if not isinstance(d, dict):
        return None
    required = ("kind", "purpose", "owner", "correlation_id", "created_at", "expires_at")
    if any(k not in d for k in required):
        return None
    if d["kind"] not in _VALID_KINDS:
        return None
    for k in required:
        if not isinstance(d[k], str) or d[k].strip() == "":
            return None
    try:
        _parse_iso(d["created_at"])
        _parse_iso(d["expires_at"])
    except Exception:  # noqa: BLE001
        return None
    if not _secret_free(d["purpose"], d["owner"], d["correlation_id"]):
        return None
    return Lease(kind=d["kind"], purpose=d["purpose"], owner=d["owner"],
                 correlation_id=d["correlation_id"], created_at=d["created_at"], expires_at=d["expires_at"])


def read_lease(now: datetime, path: str | None = None) -> tuple[LeaseStatus, Lease | None]:
    """Classify the lease file. A missing file is ABSENT; an unreadable/invalid file is MALFORMED (fail-closed),
    NEVER ABSENT — a garbage file must block, not silently permit."""
    p = lease_path(path)
    if not os.path.exists(p):
        return LeaseStatus.ABSENT, None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        # A non-UTF-8 (e.g. UTF-16/ANSI/BOM-less) or unreadable lease is MALFORMED, never ABSENT (RULE 9/11:
        # an encoding trap must fail closed, not silently classify as "no lease").
        return LeaseStatus.MALFORMED, None
    if raw.strip() == "":
        return LeaseStatus.MALFORMED, None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return LeaseStatus.MALFORMED, None
    lease = _validate_lease_dict(d)
    if lease is None:
        return LeaseStatus.MALFORMED, None
    try:
        expired = now >= _parse_iso(lease.expires_at)
    except Exception:  # noqa: BLE001
        return LeaseStatus.MALFORMED, None
    return (LeaseStatus.EXPIRED if expired else LeaseStatus.ACTIVE), lease


def _write_new_lease(p: str, text: str) -> None:
    """Atomically CREATE the lease file; raise FileExistsError if one already exists. The O_EXCL create IS the
    compare-and-swap that makes acquisition fail-closed under a race — a second acquirer that observed ABSENT but
    lost the write cannot clobber the winner's just-written lease."""
    d = os.path.dirname(os.path.abspath(p)) or "."
    os.makedirs(d, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


class _AcquireLock:
    """Best-effort cross-process exclusive lock over a sidecar ``.lock`` file (POSIX ``flock``). Serialises the whole
    classify→reclaim→create critical section so a concurrent acquire cannot interleave. Degrades to a no-op if
    ``fcntl`` is unavailable (non-POSIX) — the O_EXCL create still provides the core compare-and-swap."""

    def __init__(self, lease_file: str):
        self._fd = None
        self._fcntl = None
        try:
            import fcntl
            lock_file = lease_file + ".lock"
            os.makedirs(os.path.dirname(os.path.abspath(lock_file)) or ".", exist_ok=True)
            self._fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._fcntl = fcntl
        except Exception:  # noqa: BLE001 — no fcntl / lock failure ⇒ degrade to O_EXCL-only protection
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd = None

    def release(self):
        if self._fd is not None:
            try:
                if self._fcntl is not None:
                    self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def _audit(event: str, now: datetime, path: str | None, *, kind: str = "", correlation_id: str = "",
           purpose: str = "", owner: str = "", reason: str = "") -> None:
    """Append a no-secret audit line. Best-effort: auditing must never crash a lease op."""
    rec = {"ts": _iso(now), "event": event, "kind": kind, "correlation_id": correlation_id,
           "purpose": purpose, "owner": owner, "reason": reason}
    if not _secret_free(purpose, owner, correlation_id, reason):
        # Redact ALL free-text fields (incl. correlation_id) — any one of them tripping the secret guard means the
        # whole record is untrusted; never write a secret-bearing token verbatim.
        rec.update({"purpose": "<redacted>", "owner": "<redacted>", "reason": "<redacted>",
                    "correlation_id": "<redacted>"})
    try:
        ap = audit_path(path)
        os.makedirs(os.path.dirname(os.path.abspath(ap)) or ".", exist_ok=True)
        with open(ap, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def acquire(kind: str, purpose: str, owner: str, correlation_id: str, ttl_s: int, now: datetime,
            path: str | None = None) -> tuple[bool, object]:
    """Acquire the single operational lease, FAIL-CLOSED.

    Refuses if: kind invalid; fields secret-bearing/empty; a MALFORMED lease exists (fail-closed); or ANY ACTIVE
    lease exists (mutual exclusion — the deploy/recovery pair can never both be live). An EXPIRED lease is reclaimed
    (audited) first. Returns ``(True, Lease)`` or ``(False, reason:str)``."""
    if kind not in _VALID_KINDS:
        return False, "invalid_kind"
    for field in (purpose, owner, correlation_id):
        if not isinstance(field, str) or field.strip() == "":
            return False, "empty_field"
    if not _secret_free(purpose, owner, correlation_id):
        return False, "secret_bearing_field_rejected"
    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError):
        return False, "invalid_ttl"
    ttl = max(TTL_MIN_S, min(TTL_MAX_S, ttl))

    p = lease_path(path)
    lock = _AcquireLock(p)        # serialise the whole check→reclaim→create critical section (best-effort)
    try:
        status, cur = read_lease(now, path)
        if status == LeaseStatus.MALFORMED:
            return False, "malformed_existing_lease_fail_closed"
        if status == LeaseStatus.ACTIVE:
            return False, f"held:{cur.kind}:{cur.correlation_id}"
        if status == LeaseStatus.EXPIRED:
            _audit("reclaim_expired", now, path, kind=cur.kind, correlation_id=cur.correlation_id,
                   reason="auto_on_acquire")
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

        created_dt = _parse_iso(_iso(now))  # normalise now → tz-aware UTC
        lease = Lease(kind=kind, purpose=purpose, owner=owner, correlation_id=correlation_id,
                      created_at=_iso(created_dt), expires_at=_iso(created_dt + timedelta(seconds=ttl)))
        try:
            _write_new_lease(p, json.dumps(lease.to_dict(), sort_keys=True, indent=2) + "\n")
        except FileExistsError:
            # Lost a concurrent acquire race (another acquirer created the lease between our check and write).
            return False, "lost_acquire_race"
        _audit("acquire", now, path, kind=kind, correlation_id=correlation_id, purpose=purpose, owner=owner,
               reason=f"ttl_s={ttl}")
        return True, lease
    finally:
        lock.release()


def release(correlation_id: str, now: datetime, path: str | None = None) -> tuple[bool, str]:
    """Release ONLY the lease matching ``correlation_id``. ABSENT ⇒ already released (idempotent success). A lease
    held by a different correlation is NEVER released here (use ``reclaim`` for a stale one)."""
    status, cur = read_lease(now, path)
    if status == LeaseStatus.ABSENT:
        return True, "already_absent"
    if status == LeaseStatus.MALFORMED:
        return False, "malformed_use_reclaim"
    if cur is not None and cur.correlation_id == correlation_id:
        try:
            os.remove(lease_path(path))
        except OSError:
            return False, "remove_failed"
        _audit("release", now, path, kind=cur.kind, correlation_id=correlation_id, reason=str(status.value))
        return True, "released"
    return False, "correlation_mismatch"


def reclaim(now: datetime, path: str | None = None) -> tuple[bool, str]:
    """Safely clean up a STALE lease: EXPIRED or MALFORMED only. An ACTIVE lease is never reclaimed (it is still
    valid); use its holder's ``release``. Audited."""
    status, cur = read_lease(now, path)
    if status == LeaseStatus.ABSENT:
        return False, "absent"
    if status == LeaseStatus.ACTIVE:
        return False, "active_not_stale"
    try:
        os.remove(lease_path(path))
    except OSError:
        return False, "remove_failed"
    _audit("reclaim", now, path, kind=(cur.kind if cur else ""),
           correlation_id=(cur.correlation_id if cur else ""), reason=str(status.value))
    return True, f"reclaimed_{status.value.lower()}"


def blocks(action_kind: str, now: datetime, path: str | None = None) -> tuple[bool, str]:
    """Would an action of ``action_kind`` be blocked right now? FAIL-CLOSED.

      * MALFORMED lease ⇒ blocked (``malformed_fail_closed``) — never auto-proceed past a garbage lease.
      * ACTIVE lease of the OTHER kind ⇒ blocked (``held:<kind>:<corr>``) — deploy⊥recovery mutual exclusion.
      * ACTIVE lease of the SAME kind ⇒ blocked too (single-lease model; a second same-kind actor must wait).
      * EXPIRED / ABSENT ⇒ clear (an EXPIRED lease is reclaimable, not blocking).
    """
    status, cur = read_lease(now, path)
    if status == LeaseStatus.MALFORMED:
        return True, "malformed_fail_closed"
    if status == LeaseStatus.ACTIVE:
        return True, f"held:{cur.kind}:{cur.correlation_id}"
    return False, ("reclaimable_expired" if status == LeaseStatus.EXPIRED else "clear")


def status_metadata(now: datetime, path: str | None = None) -> dict:
    """Safe, no-secret lease status for observability/metadata (no env, no values beyond lease fields)."""
    status, cur = read_lease(now, path)
    out = {"status": status.value, "path": lease_path(path)}
    if cur is not None:
        out.update({"kind": cur.kind, "purpose": cur.purpose, "owner": cur.owner,
                    "correlation_id": cur.correlation_id, "created_at": cur.created_at,
                    "expires_at": cur.expires_at, "remaining_s": cur.remaining_s(now)})
    return out
