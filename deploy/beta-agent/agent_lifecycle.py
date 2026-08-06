"""Validation-agent MINIMUM PRODUCTION HARDENING — lifecycle logging, single-instance guard, launch proof.

WS-C + WS-D of the production-hardening programme (design: docs/VALIDATION_AGENT_PRODUCTION_HARDENING.md).
Pure stdlib, side-effect-free except the durable log/lock files it is explicitly asked to write; every
host-touching primitive is injectable so the whole module is unit-testable OFF the Windows host.

It provides three things the Aug-5 incident proved missing:
  1. **Durable lifecycle logging** independent of the WinSW wrapper log — so a start/exit is never invisible.
  2. **Single-instance guard** — only one agent may own the sanctioned listener identity; a second fails closed.
  3. **Launch proof** — the agent can tell it was started by the sanctioned service vs an ad-hoc process, and
     never reports itself supervised/HEALTHY when it was not.

SECRET-SAFE: nothing here reads or writes a password, ciphertext, signing key, or credential envelope. The
launch token is a NON-secret liveness marker (a per-boot value the service injects), never logged verbatim.
"""
from __future__ import annotations

import json
import os
import re

SCHEMA_VERSION = "2026-08-06.1"

# ── lifecycle event vocabulary (design §; monitoring-catalogue maps these to metrics/alerts) ──
EVENTS = (
    "AGENT_STARTING", "AGENT_LISTENING", "AGENT_READY", "AGENT_DEGRADED", "AGENT_STOPPING",
    "AGENT_STOPPED", "AGENT_CRASHED", "AGENT_RESTART_SCHEDULED", "AGENT_RESTARTED",
    "AGENT_CRASH_LOOP", "AGENT_LAUNCH_REJECTED",
)

# Every persisted field is on this allow-list; an unknown key is dropped (never persisted). No field here is
# a secret — pid/session/version/reason are operational, not sensitive.
_ALLOWED_FIELDS = frozenset({
    "schema_version", "ts", "event", "agent_version", "manifest_version", "pid", "parent_pid",
    "session_id", "service_identity", "supervised", "bind_host", "bind_port", "startup_reason",
    "shutdown_reason", "exit_classification", "uptime_s", "result", "correlation_id", "detail",
})
_SECRET_HINT = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|priv(ate)?[_-]?key|hmac|cipher|enc[_-]?key|"
    r"-----BEGIN|keyring)", re.IGNORECASE)
_MAX_STR = 300


def _scrub(v):
    if isinstance(v, str):
        return "[REDACTED]" if _SECRET_HINT.search(v) else v[:_MAX_STR]
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    return _scrub(str(v))


def build_event(event: str, *, now, fields: dict | None = None) -> dict:
    """Build one secret-safe, allow-listed lifecycle event. ``now`` is an injected epoch float (no wall-clock
    dependency, so tests are deterministic). An unknown event name is allowed through but flagged."""
    out = {"schema_version": SCHEMA_VERSION, "ts": float(now), "event": str(event)}
    if event not in EVENTS:
        out["detail"] = f"unknown-event:{event}"[:_MAX_STR]
    for k, v in (fields or {}).items():
        if k in _ALLOWED_FIELDS and k not in ("schema_version", "ts", "event"):
            out[k] = _scrub(v)
    return out


def classify_launch(env: dict, *, expected_token_present: bool = True) -> dict:
    """Classify how the agent was launched from its environment ONLY (no host calls). Returns
    ``{supervised, startup_reason, service_identity, override}``.

    Supervised iff the sanctioned service injected its per-start marker (``BETA_AGENT_SUPERVISED_TOKEN`` set to
    a non-empty value) AND the process runs under the expected service identity marker
    (``BETA_AGENT_SERVICE_IDENTITY``). A documented, explicit maintenance override
    (``BETA_AGENT_LAUNCH_OVERRIDE=1``) is honoured but recorded as an override (supervised stays False, so
    monitoring still shows it as not-supervised). The token is a NON-secret liveness marker, never returned."""
    token = str(env.get("BETA_AGENT_SUPERVISED_TOKEN", "") or "").strip()
    identity = str(env.get("BETA_AGENT_SERVICE_IDENTITY", "") or "").strip()
    override = str(env.get("BETA_AGENT_LAUNCH_OVERRIDE", "") or "").strip().lower() in ("1", "true", "yes", "on")
    supervised = bool(token) and bool(identity) and expected_token_present
    if supervised:
        reason = "service_supervised"
    elif override:
        reason = "operator_override"
    else:
        reason = "unsanctioned_or_manual"
    return {"supervised": supervised, "startup_reason": reason, "service_identity": identity or "unknown",
            "override": override}


def launch_permitted(classification: dict) -> bool:
    """A launch is permitted to BIND only if supervised OR an explicit operator override is set. An
    unsanctioned/manual launch with no override is REFUSED (fail-closed) — the Aug-5 vector."""
    return bool(classification.get("supervised") or classification.get("override"))


# ── single-instance guard (lock file with PID validation; injectable fs + pid-liveness) ──
class InstanceGuardError(Exception):
    """Raised when the sanctioned single-instance identity is already held by a live process."""


def acquire_single_instance(lock_path: str, *, pid: int, now, pid_alive, open_excl=None, read_text=None,
                            write_text=None, remove=None) -> dict:
    """Acquire the single-instance lock at ``lock_path``. Returns the lock record on success; raises
    ``InstanceGuardError`` if a LIVE process already holds it. Stale-lock recovery is SAFE: a lock whose pid is
    not alive is reclaimed (no silent takeover of a running instance). All fs/pid ops are injectable so this is
    testable off-host; the real caller passes os-backed implementations.

    ``open_excl(path)->bool`` attempts an atomic exclusive create (O_CREAT|O_EXCL) and returns True if THIS
    process created it. ``pid_alive(pid)->bool`` reports process liveness."""
    read_text = read_text or (lambda p: open(p, encoding="utf-8").read())
    write_text = write_text or (lambda p, s: open(p, "w", encoding="utf-8").write(s))
    remove = remove or os.remove
    record = json.dumps({"pid": int(pid), "ts": float(now)})
    if open_excl and open_excl(lock_path):
        write_text(lock_path, record)
        return {"acquired": True, "pid": int(pid), "reclaimed_stale": False}
    # lock exists — inspect the holder
    try:
        held = json.loads(read_text(lock_path))
        held_pid = int(held.get("pid"))
    except Exception:  # noqa: BLE001 — a corrupt lock is treated as stale (safe: we validate liveness next)
        held_pid = -1
    if held_pid > 0 and held_pid != int(pid) and pid_alive(held_pid):
        raise InstanceGuardError(f"single-instance lock held by live pid {held_pid}")
    # holder is dead / ours / unparseable → reclaim (stale recovery), then re-create atomically
    try:
        remove(lock_path)
    except Exception:  # noqa: BLE001
        pass
    if open_excl and not open_excl(lock_path):
        # someone raced us to the reclaimed lock — fail closed rather than double-bind
        raise InstanceGuardError("single-instance lock re-taken during stale recovery")
    write_text(lock_path, record)
    return {"acquired": True, "pid": int(pid), "reclaimed_stale": True}


def append_event(log_path: str, event: dict, *, opener=None) -> None:
    """Append one JSON lifecycle event as a line to a durable log, independent of the WinSW wrapper log.
    ``opener(path)`` is injectable (defaults to append-mode UTF-8). Never raises — a logging failure must not
    crash the agent."""
    opener = opener or (lambda p: open(p, "a", encoding="utf-8"))
    try:
        line = json.dumps(event, sort_keys=True) + "\n"
        fh = opener(log_path)
        try:
            fh.write(line)
        finally:
            fh.close()
    except Exception:  # noqa: BLE001 — durable logging is best-effort; the process must not die because of it
        pass
