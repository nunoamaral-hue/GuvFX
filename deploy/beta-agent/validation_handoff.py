"""ADR-0027 (task-launch remediation) — secure single-use LOCAL handoff between the Agent and the
task-launched validation runner.

Root cause of the login-validation timeout (investigation 2026-08-02): MT5 cannot create its GUI when the
terminal is launched IN-PROCESS by the WinSW Agent service (a non-interactive service window station);
the identical terminal builds its GUI when launched via a scheduled task. So the probe MUST run in a
task-launched process. That moves the sealed credential across a process boundary — this module is the ONLY
sanctioned channel for that crossing.

Design invariants (packet security §2/§5):
  * The scheduled task command is FIXED (no arguments) — it NEVER receives login/password/ciphertext/keys/
    server via command line, XML, task arguments, environment, registry or logs.
  * The request travels ONLY as a file under an ACL-restricted handoff directory (SYSTEM + Administrators +
    the runner identity; everyone else denied — the ACL is applied by the install script, not here).
  * The file carries the ENVELOPE-SEALED password (ciphertext) — never plaintext. The runner opens it at
    point of use with the machine-scoped private key, exactly as the in-process handler did.
  * Authenticated: every file is HMAC-tagged with a local per-install key (a file in the same ACL-restricted
    directory). A file whose tag does not verify is ignored.
  * Single-use: a request is CLAIMED by an atomic rename; a second claim of the same id finds nothing. Replay
    of a consumed id therefore fails closed.
  * Expiry-bounded: a request past its ttl is refused.
  * Auto-deleted: the Agent removes the request/claim/result files after every run (success or failure).

Nothing here imports Django, crypto keys or MetaTrader5 — it is pure stdlib and unit-testable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

_KEY_FILE = "_handoff_hmac.key"          # local transport key; created 0600-ish, ACL-locked by the installer
_REQ = ".req.json"
_CLAIM = ".claim.json"
_RES = ".res.json"


def _ensure_dir(handoff_dir: str) -> None:
    os.makedirs(handoff_dir, exist_ok=True)


def local_key(handoff_dir: str) -> bytes:
    """Read (or create once) the local HMAC key. The directory ACL is the primary control; the key
    authenticates file contents so a stray/partial write can never be mistaken for a valid request."""
    _ensure_dir(handoff_dir)
    path = os.path.join(handoff_dir, _KEY_FILE)
    try:
        with open(path, "rb") as fh:
            raw = fh.read().strip()
            if len(raw) >= 32:
                return raw
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32).encode("ascii")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(key)
    os.replace(tmp, path)                # atomic
    return key


def _tag(key: bytes, body_bytes: bytes) -> str:
    return hmac.new(key, body_bytes, hashlib.sha256).hexdigest()


def _write_tagged(path: str, key: bytes, body: dict) -> None:
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    doc = {"body": body, "hmac": _tag(key, body_bytes)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)                # atomic publish; a reader never sees a partial file


def _read_tagged(path: str, key: bytes) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, ValueError):
        return None
    body = doc.get("body")
    if not isinstance(body, dict):
        return None
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not hmac.compare_digest(_tag(key, body_bytes), str(doc.get("hmac") or "")):
        return None                      # authentication failure → ignore
    return body


def new_request_id() -> str:
    return secrets.token_hex(16)


def write_request(handoff_dir: str, request_id: str, request: dict, *, ttl_seconds: int,
                  now: float | None = None) -> str:
    """Publish a single-use request. ``request`` carries the request CONTEXT + the SEALED payload only."""
    _ensure_dir(handoff_dir)
    key = local_key(handoff_dir)
    t = float(now if now is not None else time.time())
    body = {"request_id": request_id, "created": t, "expiry": t + float(ttl_seconds), "request": request}
    path = os.path.join(handoff_dir, request_id + _REQ)
    _write_tagged(path, key, body)
    return path


def claim_request(handoff_dir: str, request_id: str, *, now: float | None = None) -> dict | None:
    """Atomically CLAIM the request (single-use). Returns the request context or ``None`` (missing / already
    claimed / bad tag / expired). The rename is the concurrency guard: only one caller wins the rename."""
    key = local_key(handoff_dir)
    req_path = os.path.join(handoff_dir, request_id + _REQ)
    claim_path = os.path.join(handoff_dir, request_id + _CLAIM)
    try:
        os.replace(req_path, claim_path)         # atomic single-use claim; a replay finds no .req
    except (FileNotFoundError, PermissionError, OSError):
        return None
    body = _read_tagged(claim_path, key)
    if body is None:
        return None
    t = float(now if now is not None else time.time())
    if t > float(body.get("expiry", 0)):
        return None                              # expired
    return body.get("request") if isinstance(body.get("request"), dict) else None


def write_result(handoff_dir: str, request_id: str, outcome: dict, *, now: float | None = None) -> None:
    """Publish the runner's SECRET-SAFE outcome ({ok, reason_code, is_demo} only)."""
    key = local_key(handoff_dir)
    safe = {"ok": bool(outcome.get("ok")),
            "reason_code": str(outcome.get("reason_code") or "could_not_verify"),
            "is_demo": outcome.get("is_demo"),
            "ts": float(now if now is not None else time.time())}
    _write_tagged(os.path.join(handoff_dir, request_id + _RES), key, safe)


def read_result(handoff_dir: str, request_id: str, *, timeout_s: float, poll_s: float = 0.5,
                sleep=time.sleep, clock=time.time) -> dict | None:
    """Poll for the runner's result, verifying its tag. ``None`` on timeout."""
    key = local_key(handoff_dir)
    deadline = clock() + float(timeout_s)
    path = os.path.join(handoff_dir, request_id + _RES)
    while clock() < deadline:
        body = _read_tagged(path, key)
        if body is not None:
            return body
        sleep(poll_s)
    return _read_tagged(path, key)               # one last read (result may have landed at the boundary)


def cleanup(handoff_dir: str, request_id: str) -> None:
    """Remove every file for this request. Idempotent; never raises."""
    for suffix in (_REQ, _CLAIM, _RES):
        try:
            os.remove(os.path.join(handoff_dir, request_id + suffix))
        except (FileNotFoundError, OSError):
            pass


def sweep_stale(handoff_dir: str, *, max_age_s: float, now: float | None = None) -> int:
    """Remove request/claim/result files older than ``max_age_s`` (a crashed runner leaves a claim behind).
    Returns the count removed. Never raises; the key file is never swept."""
    removed = 0
    t = float(now if now is not None else time.time())
    try:
        names = os.listdir(handoff_dir)
    except (FileNotFoundError, OSError):
        return 0
    for name in names:
        if name == _KEY_FILE or not name.endswith((_REQ, _CLAIM, _RES)):
            continue
        p = os.path.join(handoff_dir, name)
        try:
            if t - os.path.getmtime(p) > max_age_s:
                os.remove(p)
                removed += 1
        except (FileNotFoundError, OSError):
            pass
    return removed
