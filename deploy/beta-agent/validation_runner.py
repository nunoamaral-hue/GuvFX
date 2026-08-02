"""ADR-0027 task-launch remediation — the VALIDATION RUNNER (scheduled-task entrypoint).

Launched ONLY by the pre-approved, single-instance scheduled task. Unlike the WinSW Agent service, a
scheduled task runs in a GUI-capable window station, so MT5 can create its chart windows (root cause,
2026-08-02). The runner:

  1. sweeps stale handoff files (a crashed prior run leaves a claim behind);
  2. finds the ONE pending request the Agent staged (refuses if not exactly one — one active validation);
  3. atomically CLAIMS it (single-use; a replay finds nothing);
  4. builds the in-process login handler (envelope-open + isolated-terminal + MT5 probe) and runs it — the
     envelope is opened with the machine-scoped private key at point of use, never persisted;
  5. writes back ONLY the secret-safe {ok, reason_code, is_demo}.

It takes NO arguments and reads NO secret from its command line/env. If anything goes wrong it writes a
retryable, secret-free outcome (never a raw error, never a credential).
"""
from __future__ import annotations

import glob
import os
import sys

import validation_handoff as handoff


def _rmtree_os(root: str) -> None:
    """Recursively delete ``root`` using only ``os`` (host mutations must not pull in shutil/subprocess —
    Windows-adapter boundary). Never raises."""
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            try:
                os.remove(os.path.join(dirpath, f))
            except OSError:
                pass
        for d in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, d))
            except OSError:
                pass
    try:
        os.rmdir(root)
    except OSError:
        pass


def _scrub_validation_terminal(cfg: dict) -> None:
    """Post-probe cleanup (packet §6): remove the credential artefact ``accounts.dat`` and the login/account
    logs the probe wrote, so NO reusable credential remains and the next probe starts clean. (Full
    baseline-fingerprint restoration from the precompiled reference is a separate ops/host step.) Pure file
    I/O; never a Windows-API call; never raises."""
    vdir = cfg.get("validation_terminal_dir")
    if not vdir:
        return
    for adat in (os.path.join(vdir, "config", "accounts.dat"),
                 os.path.join(vdir, "Config", "accounts.dat")):
        try:
            os.remove(adat)
        except (FileNotFoundError, OSError):
            pass
    for logs in (os.path.join(vdir, "Logs"), os.path.join(vdir, "logs")):
        _rmtree_os(logs)


def _single_pending(handoff_dir: str):
    """Return (request_id, count) for the pending ``*.req.json``. request_id is None unless exactly one."""
    reqs = sorted(glob.glob(os.path.join(handoff_dir, "*" + handoff._REQ)))
    if len(reqs) != 1:
        return None, len(reqs)
    name = os.path.basename(reqs[0])
    return name[: -len(handoff._REQ)], 1


def run_once(cfg: dict, *, build_handler=None) -> str:
    """One claim → probe → result cycle. Returns a short non-secret status string (for the task log)."""
    handoff_dir = cfg.get("validation_handoff_dir")
    if not handoff_dir:
        return "no_handoff_dir"
    try:
        handoff.sweep_stale(handoff_dir, max_age_s=float(cfg.get("login_timeout_ms", 30000)) / 1000.0 + 120)
    except Exception:                        # noqa: BLE001
        pass
    rid, count = _single_pending(handoff_dir)
    if rid is None:
        return "no_single_pending:%d" % count      # 0 (nothing to do) or >1 (refuse — one active only)
    req = handoff.claim_request(handoff_dir, rid)
    if req is None:
        return "claim_failed"                       # already claimed / bad tag / expired
    build = build_handler
    if build is None:
        import validate_login                       # noqa: PLC0415 — host-side import
        build = validate_login.build_inprocess_handler
    handler = build(cfg)
    if handler is None:
        handoff.write_result(handoff_dir, rid,
                             {"ok": False, "reason_code": "validation_unconfigured", "is_demo": None})
        return "unconfigured"
    try:
        outcome = handler.validate(
            operation=req.get("operation"), runtime_uuid=req.get("runtime_uuid"),
            correlation_id=req.get("correlation_id"), nonce=req.get("nonce"), payload=req.get("payload"))
    except Exception:                        # noqa: BLE001 — the handler never raises, but stay fail-closed
        outcome = {"ok": False, "reason_code": "could_not_verify", "is_demo": None}
    finally:
        _scrub_validation_terminal(cfg)      # remove the credential artefact + logs after EVERY probe (§6)
    handoff.write_result(handoff_dir, rid, outcome)
    return "ok"


def main() -> int:
    import config                            # noqa: PLC0415 — host-side import (agent config loader)
    status = run_once(config.load_config())
    print("validation_runner:", status)      # non-secret status only; captured by the task's own log
    return 0


if __name__ == "__main__":
    sys.exit(main())
