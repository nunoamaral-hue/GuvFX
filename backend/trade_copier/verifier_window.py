"""trade_copier.verifier_window — the bounded, ephemeral source for the backend's dedicated
``COPIER_INGEST_HMAC_KEY`` (ADR-0101).

WHY THIS EXISTS
---------------
The two gated backend verifier endpoints — signed event ingestion (ADR-0074) and host-emitter
attestation verify (ADR-0077) — both need the SAME dedicated secret, ``COPIER_INGEST_HMAC_KEY``, to
check an emitter signature. Historically they resolved it with ``core.credentials.resolve_secret``,
which is ``os.environ``-only. Packet 080 proved the production backend is NOT keyed and established
that putting the key in the container environment is unacceptable: it would be visible via
``docker inspect``, ``/proc/<pid>/environ`` and a normal persisted env file, and it would remain at
rest for the whole container lifetime (a standing, un-bounded verifier).

THE CORRECTIVE (ADR-0101)
-------------------------
This module resolves the key from a **bounded verifier window** instead of the environment:

* The operator opens exactly ONE window (single-flight — DB partial-unique on ``state=OPEN``) with a
  short, capped lifetime, and writes the key to an EPHEMERAL tmpfs file (default ``/dev/shm/…``) via a
  non-echoing stdin path — never a command argument, never an env var, never a persisted file, never a
  Docker image layer, never ``docker inspect`` metadata.
* While the window is OPEN and unexpired, the gated endpoints read the key from that tmpfs file at
  request time (the key lives only in RAM, only for the window).
* The window is **fail-closed**: no window / expired / wrong purpose / exhausted / missing-or-insecure
  key file ⇒ the key cannot be resolved and the endpoint returns 503. Normal backend operation (no
  window) therefore holds NO key.
* Cleanup is **idempotent and guaranteed**: closing a window shreds the tmpfs file; an expired window is
  reaped (and its file shredded) on the next open/claim; a ``max_uses`` cap auto-closes+shreds; and the
  tmpfs file is RAM-backed, so a container restart erases it regardless.

This module stores NO secret, NO broker credential and NO token in the database — the window row holds
only the tmpfs PATH, never the key value. It grants NO execution/arm/admission authority of any kind:
it only makes an identity-proof key readable to two endpoints that create no order, job, lifecycle or
broker contact. It NEVER substitutes a different credential (security.md RULE 3) and NEVER reads the key
from the environment.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import stat as _stat
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# The ephemeral key file lives on tmpfs (RAM): never persisted to disk/image, erased on restart.
DEFAULT_KEY_PATH = "/dev/shm/guvfx_copier_ingest_verify.key"
# A window may never outlive these bounds. Short by design — a verification session, not a standing key.
MAX_WINDOW_TTL_SECONDS = 900
MIN_WINDOW_TTL_SECONDS = 30
# The keyring key-id both verifier paths use (matches event_ingestion.DEFAULT_KEY_ID and the emitter).
INGEST_KEY_ID = "copier-ingest"
_MIN_KEY_LEN = 32


class VerifierWindowError(Exception):
    """A fail-closed verifier-window fault. ``reason`` is a stable, sanitised code — never a secret."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verifier_key_path() -> str:
    """The configured tmpfs path for the ephemeral key (plain config, never a secret). Default ``/dev/shm``."""
    raw = getattr(settings, "COPIER_VERIFIER_KEY_PATH", None)
    if raw is None:
        raw = os.getenv("COPIER_VERIFIER_KEY_PATH", "")
    return str(raw).strip() or DEFAULT_KEY_PATH


def _now(now=None):
    return now or timezone.now()


def _purpose_covers(window_purpose: str, requested: str) -> bool:
    """A ``BOTH`` window serves either endpoint; otherwise the purposes must match exactly."""
    return window_purpose == "BOTH" or window_purpose == requested


# ─────────────────────────────── ephemeral key file I/O (fail-closed) ───────────────────────────────

def write_key_file(path: str, key: str) -> None:
    """Write the ephemeral key to ``path`` with 0600, refusing to follow a symlink. Used by the operator
    command (which reads the key from STDIN — never argv/env). Rejects an obviously-truncated key."""
    value = (key or "").strip()
    if len(value) < _MIN_KEY_LEN:
        raise VerifierWindowError("key_too_short")
    # O_NOFOLLOW defeats a pre-planted symlink; O_TRUNC clears any stale content; 0600 from creation.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _read_key_file(path: str) -> str:
    """Read + validate the ephemeral key. Fail-closed on absence, symlink, loose permissions, wrong
    ownership, non-utf8, too-short or placeholder content. Returns the key string on success."""
    try:
        st = os.lstat(path)
    except OSError:
        raise VerifierWindowError("key_file_absent")
    if _stat.S_ISLNK(st.st_mode):
        raise VerifierWindowError("key_file_symlink")
    if not _stat.S_ISREG(st.st_mode):
        raise VerifierWindowError("key_file_not_regular")
    if st.st_mode & 0o077:
        raise VerifierWindowError("key_file_permissions")   # group/other access ⇒ refuse
    if st.st_uid != os.getuid():
        raise VerifierWindowError("key_file_ownership")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        raise VerifierWindowError("key_file_unreadable")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise VerifierWindowError("key_file_invalid")
    if len(value) < _MIN_KEY_LEN:
        raise VerifierWindowError("key_file_too_short")
    from core.credentials import is_placeholder
    if is_placeholder(value):
        raise VerifierWindowError("key_file_placeholder")
    return value


def _shred_file(path: str) -> None:
    """Best-effort shred: overwrite with random bytes then unlink. Idempotent; never raises (cleanup must
    not fail a request or a close). The file is tmpfs (RAM), so this plus a restart leaves nothing."""
    try:
        if not path:
            return
        if os.path.islink(path):
            os.unlink(path)
            return
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        with open(path, "r+b", buffering=0) as fh:
            fh.write(_secrets.token_bytes(max(size, 1)))
            fh.flush()
            os.fsync(fh.fileno())
        os.unlink(path)
    except OSError:
        logger.debug("verifier key shred best-effort failed (already gone / tmpfs)")


def _unique_key_path(base: str | None = None) -> str:
    """A PER-WINDOW-UNIQUE tmpfs key path derived from the configured base: same directory and extension, with
    a random stem token (GFX-164). Two windows therefore never share a key file, so one window's cleanup (close
    / reap / exhaustion) can never touch another window's key. The random component also defeats a pre-planted
    symlink at a predictable name. Used when the caller hands ``open_window`` the key value to own."""
    base = (base or verifier_key_path()).strip() or DEFAULT_KEY_PATH
    directory = os.path.dirname(base) or "/dev/shm"
    name = os.path.basename(base) or "guvfx_copier_ingest_verify.key"
    stem, dot, ext = name.rpartition(".")
    if not dot:                      # no extension on the base name
        stem, ext = name, "key"
    return os.path.join(directory, f"{stem}.{_secrets.token_hex(8)}.{ext}")


# ─────────────────────────────── window lifecycle ───────────────────────────────

def active_window(purpose: str, *, now=None):
    """The single OPEN, unexpired window that covers ``purpose`` — or None (fail-closed on 0 / ≥2 / drift)."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    covering = [w for w in CopierVerifierWindow.objects.filter(state=S.OPEN, expires_at__gt=now)
                if _purpose_covers(w.purpose, purpose)]
    if len(covering) != 1:   # the partial-unique constraint guarantees ≤1 OPEN; be defensive anyway
        return None
    return covering[0]


def open_window(*, opened_by=None, purpose: str = "BOTH", ttl_seconds: int = 300, key_path: str | None = None,
                max_uses: int | None = None, correlation_id: str = "", note: str = "", now=None,
                key: str | None = None):
    """Open exactly one bounded window (single-flight), fail-closed.

    KEY-LIFECYCLE ORDERING (GFX-164). Stale cleanup happens FIRST (``reap_expired``), then the single-flight
    check, and only THEN — when the caller hands us ``key`` — is the ephemeral key file written. Because the
    key is written strictly AFTER the reap and AFTER confirming no window is already open, stale-window
    cleanup can never shred a freshly opened window's key. (Previously the operator wrote the key to a fixed
    default path BEFORE calling ``open_window``; opening a new window then reaped an expired prior window that
    shared that same path, and the reap shredded the just-written key — an OPEN but keyless window.)

    OWNERSHIP. When ``key`` is supplied and no explicit ``key_path`` is given, the key is written to a
    PER-WINDOW-UNIQUE tmpfs path (``_unique_key_path``), so two windows never share key material and one
    window's cleanup can never touch another's. An explicit ``key_path`` is honoured verbatim (operator intent;
    the ordering guarantee above still holds for it under single-flight).

    LEGACY MODE (``key`` omitted): the caller has already written the file at ``key_path`` — retained for the
    ``verification_window`` test/local helper, which uses its own unique temp path. The reap-before-create
    ordering still applies.

    This function OWNS cleanup of the key file it wrote: on any failure after the write it shreds that file,
    and it NEVER shreds a path that an already-open window (or the winner of a concurrent open) references.
    """
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    reap_expired(now=now)                       # (1) stale cleanup — only EXPIRED windows, before any new key exists
    ttl = max(MIN_WINDOW_TTL_SECONDS, min(int(ttl_seconds), MAX_WINDOW_TTL_SECONDS))
    purpose = str(purpose or "BOTH").upper()
    if purpose not in {"INGEST", "ATTEST", "BOTH"}:
        raise VerifierWindowError("bad_purpose")
    owns_write = key is not None
    explicit_path = (key_path or "").strip()
    if owns_write:
        unique_owned = not explicit_path         # we generated the path ⇒ safe to shred it on our own failure
        path = explicit_path or _unique_key_path()
    else:
        unique_owned = False
        path = explicit_path or verifier_key_path()
    wrote_key = False
    try:
        with transaction.atomic():
            existing = (CopierVerifierWindow.objects.select_for_update()
                        .filter(state=S.OPEN, expires_at__gt=now).first())
            if existing is not None:             # (2) single-flight — an unexpired window owns its key; never touch it
                raise VerifierWindowError("window_already_open")
            if owns_write:
                wrote_key = True                 # (3) write the key ONLY after reap + single-flight check
                write_key_file(path, key)
            return CopierVerifierWindow.objects.create(   # (4) create the row referencing the just-written key
                state=S.OPEN, purpose=purpose, key_path=path, max_uses=max_uses,
                expires_at=now + timedelta(seconds=ttl), opened_by=opened_by,
                correlation_id=correlation_id[:64], note=note[:256])
    except IntegrityError:
        # Lost a concurrent create race (partial-unique on state=OPEN). Shred ONLY a key we wrote to a path
        # WE generated uniquely — never a shared/explicit path the winning window now references.
        if wrote_key and unique_owned:
            _shred_file(path)
        raise VerifierWindowError("window_already_open")
    except VerifierWindowError:
        if wrote_key:
            _shred_file(path)                    # our freshly-written key (e.g. create failed) — leave nothing behind
        raise
    except Exception:
        if wrote_key:
            _shred_file(path)
        raise


def claim_key(purpose: str, *, now=None) -> str:
    """Resolve the key for ``purpose`` from the OPEN window, counting one use under a row lock. Fail-closed on
    no/expired/exhausted/purpose-mismatch window or an absent/insecure key file. When a ``max_uses`` cap is
    reached the window auto-closes and its key file is shredded."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    reached_cap = False
    key_path = ""
    with transaction.atomic():
        w = (CopierVerifierWindow.objects.select_for_update()
             .filter(state=S.OPEN).order_by("id").first())
        if w is None:
            raise VerifierWindowError("no_verifier_window")
        if w.expires_at <= now:
            raise VerifierWindowError("verifier_window_expired")
        if not _purpose_covers(w.purpose, purpose):
            raise VerifierWindowError("verifier_window_purpose")
        if w.max_uses is not None and w.uses >= w.max_uses:
            raise VerifierWindowError("verifier_window_exhausted")
        key = _read_key_file(w.key_path)   # fail-closed read; raising here rolls back (no use burned)
        w.uses = w.uses + 1
        key_path = w.key_path
        if w.max_uses is not None and w.uses >= w.max_uses:
            reached_cap = True
            w.state = S.CLOSED
            w.closed_at = now
            w.close_reason = "max_uses"
        w.save(update_fields=["uses", "state", "closed_at", "close_reason"])
    if reached_cap:
        _shred_file(key_path)
    return key


def ingest_keyring(purpose: str, *, now=None) -> dict:
    """The keyring the verifier primitives consume: ``{INGEST_KEY_ID: <key>}`` from the bounded window."""
    return {INGEST_KEY_ID: claim_key(purpose, now=now)}


def close_window(window, *, reason: str = "closed", now=None) -> None:
    """Idempotently shred the window's key file and mark it CLOSED. Safe to call on an already-closed window."""
    now = _now(now)
    _shred_file(window.key_path)
    from trade_copier.models import CopierVerifierWindowState as S
    if window.state != S.CLOSED:
        window.state = S.CLOSED
        window.closed_at = now
        window.close_reason = (reason or "closed")[:64]
        window.save(update_fields=["state", "closed_at", "close_reason"])


def reap_expired(*, now=None) -> int:
    """Close (and shred) every OPEN window past its expiry. Idempotent; returns the count reaped."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    reaped = 0
    for w in CopierVerifierWindow.objects.filter(state=S.OPEN, expires_at__lte=now):
        close_window(w, reason="expired", now=now)
        reaped += 1
    return reaped


def close_all(*, reason: str = "operator_close", now=None) -> int:
    """Operator cleanup: close + shred every OPEN window, and defensively shred the default key path (covers an
    interrupted open that never created a row). Idempotent; returns the count of windows closed."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    closed = 0
    for w in CopierVerifierWindow.objects.filter(state=S.OPEN):
        close_window(w, reason=reason, now=now)
        closed += 1
    _shred_file(verifier_key_path())
    return closed


def window_status(*, now=None) -> dict:
    """Read-only, NON-SECRET status for the operator command: whether a window is open, its purpose, expiry and
    use count. NEVER reads or returns the key."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    w = CopierVerifierWindow.objects.filter(state=S.OPEN, expires_at__gt=now).order_by("id").first()
    if w is None:
        return {"open": False}
    return {
        "open": True, "purpose": w.purpose, "opened_at": w.opened_at.isoformat(),
        "expires_at": w.expires_at.isoformat(), "uses": w.uses, "max_uses": w.max_uses,
        "key_file_present": os.path.exists(w.key_path),
    }


def covers_live_authorization(authz, *, now=None) -> dict:
    """ADR-0117 arm-time gate: is there an OPEN window that covers INGEST, whose ephemeral key file is present, and
    whose expiry is >= the authorization's expiry (so the emitter's signed events can be verified for the WHOLE
    live window — the window must never lapse mid-demo and 503 an in-flight close)? Returns a non-secret verdict
    ``{"ready": bool, "reason": str, "window": {...}?}``. NEVER reads the key VALUE (presence/metadata only)."""
    from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
    now = _now(now)
    w = CopierVerifierWindow.objects.filter(state=S.OPEN, expires_at__gt=now).order_by("id").first()
    if w is None:
        return {"ready": False, "reason": "no_open_window"}
    if not _purpose_covers(w.purpose, "INGEST"):
        return {"ready": False, "reason": "purpose_not_ingest", "window": {"purpose": w.purpose}}
    # A USE-CAPPED window auto-closes + shreds its key after ``max_uses`` verifications; since each ingested event
    # (OPEN, every MODIFY, the CLOSE, heartbeats) burns one use, a capped window would lapse mid-lifecycle and 503
    # the follower CLOSE, stranding a live position. The live window must be bounded ONLY by its TTL (``max_uses``
    # None — open it with ``copier_verifier_window --open --max-uses 0``). Refuse any finite cap at arm time.
    if getattr(w, "max_uses", None) is not None:
        return {"ready": False, "reason": "window_use_capped",
                "window": {"purpose": w.purpose, "max_uses": w.max_uses}}
    authz_exp = getattr(authz, "expires_at", None)
    if authz_exp is not None and w.expires_at < authz_exp:
        return {"ready": False, "reason": "window_expires_before_authorization",
                "window": {"purpose": w.purpose, "expires_at": w.expires_at.isoformat(),
                           "authorization_expires_at": authz_exp.isoformat()}}
    if not os.path.exists(w.key_path):   # metadata only — the key VALUE is never read here
        return {"ready": False, "reason": "key_file_absent", "window": {"purpose": w.purpose}}
    return {"ready": True, "reason": "ok",
            "window": {"purpose": w.purpose, "expires_at": w.expires_at.isoformat()}}


@contextmanager
def verification_window(secret: str, *, purpose: str = "BOTH", ttl_seconds: int = 300,
                        max_uses: int | None = None, key_path: str | None = None, opened_by=None):
    """TEST / local-operator helper: open a window, write ``secret`` to an ephemeral key file, and GUARANTEE
    cleanup on exit (shred + close + remove any temp file). Tests pass SYNTHETIC keys only. In production the
    operator uses the ``copier_verifier_window`` command (key from stdin) rather than this helper."""
    tmp = None
    path = key_path
    if path is None:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="guvfx_verify_", suffix=".key")
        os.close(fd)
        tmp = path
    write_key_file(path, secret)
    window = open_window(purpose=purpose, ttl_seconds=ttl_seconds, key_path=path, max_uses=max_uses,
                         opened_by=opened_by)
    try:
        yield window
    finally:
        close_window(window, reason="context_exit")
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
