#!/usr/bin/env python3
"""deploy_recovery_gate — refuse a backend deploy while a governed recovery lease is live (packet-127).

The HOST deploy process (e.g. just before ``docker compose up -d --force-recreate guvfx-backend``) runs this gate.
It is deliberately **Django-free and dependency-free** (stdlib only) so it still works when the backend container is
down or being recreated — exactly when a deploy happens. It reads the SAME lease state file the in-container
``recovery_ops`` command writes, so the two share one source of truth; ``test_safe_container_env``-style agreement
tests assert this gate classifies identically to ``hosted_workspace.recovery_lease``.

EXIT CODES: 0 = deploy permitted; 2 = BLOCKED (a live recovery/deploy lease, or a malformed lease ⇒ fail-closed).
An ``--emergency-override REASON`` proceeds (exit 0) and appends an audit record; it is the ONLY way past a block,
and it is recorded.

USAGE:
    python3 scripts/deploy_recovery_gate.py --lease-file /srv/guvfx/mt5_handoff/recovery_operational_lease.json
    python3 scripts/deploy_recovery_gate.py --lease-file <path> --emergency-override "hotfix CVE-xxxx, approved by <op>"
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

_VALID_KINDS = ("recovery_acceptance", "deploy")
_AUDIT_FILENAME = "recovery_operational_lease_audit.jsonl"

# Kept byte-for-byte in step with hosted_workspace.recovery_lease so the gate classifies IDENTICALLY (a lease the
# lease module calls MALFORMED must not be read as EXPIRED/ACTIVE here). A secret-bearing field ⇒ MALFORMED ⇒ block.
_SECRET_NAME_RE = re.compile(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|KEYRING|PRIVATE)\s*=")
_LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _secret_free(*values):
    for v in values:
        s = str(v or "")
        if _SECRET_NAME_RE.search(s) or _LONG_OPAQUE_RE.search(s):
            return False
    return True


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(s):
    dt = datetime.fromisoformat(str(s))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def classify(path, now):
    """Return (status, lease_dict|None): ABSENT / ACTIVE / EXPIRED / MALFORMED. MALFORMED on ANY read/parse/schema
    error (fail-closed — a garbage lease must block, never read as ABSENT). Mirrors recovery_lease.read_lease."""
    if not os.path.exists(path):
        return "ABSENT", None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return "MALFORMED", None            # non-UTF-8 / unreadable ⇒ fail closed (RULE 9/11), never ABSENT
    if raw.strip() == "":
        return "MALFORMED", None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return "MALFORMED", None
    required = ("kind", "purpose", "owner", "correlation_id", "created_at", "expires_at")
    if (not isinstance(d, dict) or any(k not in d for k in required)
            or d.get("kind") not in _VALID_KINDS
            or any(not isinstance(d.get(k), str) or not str(d.get(k)).strip() for k in required)):
        return "MALFORMED", None
    if not _secret_free(d["purpose"], d["owner"], d["correlation_id"]):
        return "MALFORMED", None            # parity with recovery_lease._validate_lease_dict (secret ⇒ MALFORMED)
    try:
        expired = now >= _parse_iso(d["expires_at"])
        _parse_iso(d["created_at"])
    except Exception:
        return "MALFORMED", None
    return ("EXPIRED" if expired else "ACTIVE"), d


def _audit(path, record):
    try:
        ap = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", _AUDIT_FILENAME)
        os.makedirs(os.path.dirname(os.path.abspath(ap)) or ".", exist_ok=True)
        with open(ap, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def deploy_blocked(path, now):
    """(blocked: bool, reason: str). Blocked by a MALFORMED lease (fail-closed) or ANY ACTIVE lease. An EXPIRED or
    ABSENT lease does not block (an expired lease is reclaimable)."""
    status, d = classify(path, now)
    if status == "MALFORMED":
        return True, "malformed_lease_fail_closed"
    if status == "ACTIVE":
        return True, "held:%s:%s" % (d.get("kind"), d.get("correlation_id"))
    return False, ("reclaimable_expired" if status == "EXPIRED" else "clear")


def _default_lease_file():
    f = os.getenv("GUVFX_RECOVERY_LEASE_FILE")
    if f:
        return f
    d = os.getenv("GUVFX_RECOVERY_LEASE_DIR")
    if d:
        return os.path.join(d, "recovery_operational_lease.json")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse a backend deploy while a governed recovery lease is live.")
    ap.add_argument("--lease-file", default=_default_lease_file(),
                    help="Path to the lease state file (or set GUVFX_RECOVERY_LEASE_FILE / GUVFX_RECOVERY_LEASE_DIR).")
    ap.add_argument("--emergency-override", dest="override", default=None,
                    help="Audited reason to deploy anyway despite a live recovery lease.")
    args = ap.parse_args(argv)
    if not args.lease_file:
        print(json.dumps({"deploy_permitted": False, "reason": "no_lease_file_configured_fail_closed"}))
        return 2
    now = _now()
    blocked, reason = deploy_blocked(args.lease_file, now)
    if blocked and args.override:
        reason_txt = str(args.override)[:200]
        if not _secret_free(reason_txt):
            reason_txt = "<redacted>"
        _audit(args.lease_file, {"ts": now.isoformat(), "event": "emergency_override", "kind": "deploy",
                                 "reason": "deploy_override:" + reason_txt})
        print(json.dumps({"deploy_permitted": True, "override": True, "original_reason": reason}))
        return 0
    print(json.dumps({"deploy_permitted": (not blocked), "blocked": blocked, "reason": reason}))
    return 2 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
