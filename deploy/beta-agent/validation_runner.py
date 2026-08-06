"""ADR-0027 task-launch remediation — the VALIDATION RUNNER (scheduled-task entrypoint).

Launched ONLY by the pre-approved, single-instance scheduled task. Unlike the WinSW Agent service, a
scheduled task runs in a GUI-capable window station, so MT5 can create its chart windows (root cause,
2026-08-02).

Observability enhancement (2026-08-03): the prior credentialed validation returned ``login_timeout`` AFTER a
broker TCP connection but the decisive journal lines were LOST — the runner scrubbed the logs before any
preservation and the terminal lingered after ``mt5.shutdown()``. The runner now:

  1. sweeps stale handoff files + expired diagnostic artefacts;
  2. finds the ONE pending request and CLAIMS it (single-use);
  3. runs the in-process login probe (envelope-open + isolated-terminal + MT5 probe);
  4. CAPTURES a secret-safe diagnostic artefact — terminal pid/session, journal MILESTONES (codes, not raw
     text), MT5 last_error, terminal/account availability, stage localisation — and writes it DURABLY BEFORE
     any cleanup, so a single future validation can say exactly where the login stops;
  5. deterministically TERMINATES the isolated validation terminal (``mt5.shutdown()`` alone is proven
     insufficient) — path-guarded so it can never touch Customer Zero, a slot, golden or a production terminal;
  6. scrubs the credential artefact + logs and restores the certified precompiled baseline;
  7. writes back ONLY the secret-safe {ok, reason_code, is_demo} (+ a compact operator summary).

If diagnostic capture fails it STILL performs emergency credential-artefact cleanup and returns
``diagnostic_capture_failed`` — never a silent ``login_timeout``. It takes NO arguments and reads NO secret
from its command line/env.
"""
from __future__ import annotations

import glob
import os
import sys
import time

import validation_diagnostics as diag
import validation_handoff as handoff

_DEFAULT_RETENTION_S = 3 * 24 * 3600          # bounded diagnostic retention (packet §7): 72h


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


def _copy_file_os(src: str, dst: str) -> None:
    """Copy one file with ``os``/builtin ``open`` only (no shutil). Parent dirs must already exist."""
    with open(src, "rb") as r, open(dst, "wb") as w:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            w.write(chunk)


def _mirror_os(src: str, dst: str) -> str:
    """Mirror ``src`` → ``dst`` using only ``os`` (no shutil/robocopy): copy files that are missing or differ
    in size, then remove ``dst`` files/dirs absent from ``src``. Restores the certified precompiled baseline
    deterministically (packet §6). Two safety guards (review 2026-08-03):
      * refuse an INVALID/EMPTY source — the source must actually contain the terminal executable, so a
        missing/empty precompiled dir can never trigger a destructive wipe of the terminal dir;
      * the delete pass removes a file ONLY when its REAL path is still beneath ``dst`` (no ``followlinks``),
        so a reparse point / junction inside ``dst`` can never redirect a delete OUTSIDE ``dst``.
    """
    if not os.path.isdir(src):
        return "no_source"
    if not os.path.isfile(os.path.join(src, "terminal64.exe")):
        return "invalid_source"                          # not a real baseline → NEVER delete from dst
    os.makedirs(dst, exist_ok=True)
    dst_real = os.path.realpath(dst)
    keep = set()
    for dirpath, _dirnames, filenames in os.walk(src, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        ddir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(ddir, exist_ok=True)
        for f in filenames:
            sp, dp = os.path.join(dirpath, f), os.path.join(ddir, f)
            keep.add(os.path.normcase(os.path.relpath(dp, dst)))
            try:
                if (not os.path.exists(dp)) or os.path.getsize(dp) != os.path.getsize(sp):
                    _copy_file_os(sp, dp)
            except OSError:
                pass
    for dirpath, dirnames, filenames in os.walk(dst, topdown=False, followlinks=False):
        for f in filenames:
            dp = os.path.join(dirpath, f)
            if os.path.normcase(os.path.relpath(dp, dst)) in keep:
                continue
            real = os.path.realpath(dp)
            if real != dst_real and not real.startswith(dst_real + os.sep):
                continue                                 # reparse/junction escapes dst → refuse to delete
            try:
                os.remove(dp)
            except OSError:
                pass
        for d in dirnames:
            dd = os.path.join(dirpath, d)
            try:
                if not os.listdir(dd):
                    os.rmdir(dd)
            except OSError:
                pass
    return "restored"


def _light_fingerprint(root: str) -> str:
    """A cheap parity signature (file count + total bytes) recorded after restore — the FULL SHA parity is the
    ops host-certification check, this only flags gross drift without hashing every file each run."""
    n = total = 0
    try:
        for dirpath, _d, filenames in os.walk(root):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
    except OSError:
        return "unavailable"
    return "files=%d;bytes=%d" % (n, total)


def _scrub_validation_terminal(cfg: dict) -> None:
    """Emergency credential-artefact cleanup: remove ``accounts.dat`` and the login/account logs the probe
    wrote, so NO reusable credential remains. Runs on EVERY path — success, failure AND diagnostic-capture
    failure. Pure file I/O; never raises."""
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


def _forbidden_roots(cfg: dict) -> tuple:
    """The estate roots the validation terminal — and thus any terminate target — must stay disjoint from."""
    import validate_login                      # noqa: PLC0415 — host-side import; reuse the pinned defaults
    roots = (validate_login.DEFAULT_FORBIDDEN_ROOTS
             + (cfg.get("slots_root", ""), cfg.get("golden_dir", ""), cfg.get("beta_root", ""))
             + tuple(cfg.get("validation_forbidden_roots", ())))
    return tuple(r for r in dict.fromkeys(roots) if r)


def _config_source(cfg: dict) -> dict:
    """SECRET-SAFE provenance of the PATH-related validation config the RUNNER resolved: whether each value came
    from the runner's process environment or a code default. Emits the source LABEL only — never the value, and
    never the full environment (allow-list of path-config names). Lets an operator see that the scheduled-task
    runner's effective config came from ``env`` vs ``default`` when its effective terminal dir differs from a
    service-level/manual value (the discrepancy class that produced ``isolation_check_failed``)."""
    names = {
        "validation_terminal_dir": "BETA_AGENT_VALIDATION_TERMINAL_DIR",
        "validation_root": "BETA_AGENT_VALIDATION_ROOT",
        "validation_forbidden_roots": "BETA_AGENT_VALIDATION_FORBIDDEN_ROOTS",
        "validation_precompiled_dir": "BETA_AGENT_VALIDATION_PRECOMPILED_DIR",
    }
    return {k: ("env" if os.environ.get(env) else "default") for k, env in names.items()}


def _default_win(cfg: dict):
    """Build the real Windows adapter for process discovery/termination. Off-host every method raises
    WindowsApiUnavailable — the runner catches that and records the capture as unavailable."""
    import win_slot_ops                         # noqa: PLC0415 — host-side import (the Windows adapter)
    return win_slot_ops.ValidationWindowsOps(golden_dir=cfg.get("golden_dir", ""),
                                             slots_root=cfg.get("slots_root", ""))


def _single_pending(handoff_dir: str):
    """Return (request_id, count) for the pending ``*.req.json``. request_id is None unless exactly one."""
    reqs = sorted(glob.glob(os.path.join(handoff_dir, "*" + handoff._REQ)))
    if len(reqs) != 1:
        return None, len(reqs)
    name = os.path.basename(reqs[0])
    return name[: -len(handoff._REQ)], 1


def _derive_stages(op: dict, milestones) -> set:
    """The CONTIGUOUS set of login stages reached, up to the HIGHEST positively evidenced one (probe result +
    journal milestone codes). Reasoning is MONOTONIC-UP-FROM-EVIDENCE: a positive later signal (broker TCP,
    authorisation) proves the earlier stages (GUI, IPC) even when MT5 did not journal them — that is sound,
    unlike inferring a stage from a process merely staying alive, which packet §4 forbids and this never does.
    Covers only the LOGIN stages (REQUEST_ACCEPTED..CLASSIFIED); the cleanup stages are added by the runner
    from its own termination/restore results, never from the probe."""
    m = set(milestones or ())
    highest = diag.STAGES.index("REQUEST_ACCEPTED")

    def bump(stage):
        nonlocal highest
        highest = max(highest, diag.STAGES.index(stage))

    bump("RUNNER_STARTED")
    if op:                                       # _operator present ⇒ isolation + envelope-open both passed
        bump("ENVELOPE_OPENED")
    if op.get("initialize_started"):
        bump("TERMINAL_LAUNCHED")
    if "GUI_MAIN_WINDOW_CREATED" in m:
        bump("GUI_READY")
    if "IPC_PIPE_READY" in m:
        bump("IPC_READY")
    if {"BROKER_TCP_ESTABLISHED", "BROKER_CONNECTED"} & m:
        bump("BROKER_TCP_CONNECTED")
    if "BROKER_AUTHORISED" in m or op.get("initialize_result"):
        bump("BROKER_AUTHORISED")                # a successful initialize proves login through authorisation
    if op.get("account_info_present"):
        bump("ACCOUNT_INFO_READY")
    if op.get("trade_mode") is not None:
        bump("CLASSIFIED")
    # A journalled MDI failure caps progress at TERMINAL_LAUNCHED — the GUI never initialised — UNLESS a later
    # positive signal (broker TCP+) contradicts it, in which case the positive evidence wins.
    if "GUI_MDI_CREATE_FAILED" in m and highest < diag.STAGES.index("BROKER_TCP_CONNECTED"):
        highest = min(highest, diag.STAGES.index("TERMINAL_LAUNCHED"))
    return set(diag.STAGES[:highest + 1])


def run_once(cfg: dict, *, build_handler=None, win=None, clock=None,
             read_journal=None, mirror_baseline=None) -> str:
    """One claim → probe → CAPTURE → terminate → scrub → restore cycle. Returns a short non-secret status."""
    clock = clock or time.time
    handoff_dir = cfg.get("validation_handoff_dir")
    if not handoff_dir:
        return "no_handoff_dir"
    diag_dir = cfg.get("validation_diagnostics_dir")
    try:
        handoff.sweep_stale(handoff_dir, max_age_s=float(cfg.get("login_timeout_ms", 30000)) / 1000.0 + 120)
    except Exception:                            # noqa: BLE001
        pass
    if diag_dir:
        try:
            diag.sweep_expired(diag_dir, max_age_s=float(cfg.get("validation_diagnostics_retention_s",
                                                                 _DEFAULT_RETENTION_S)), now=clock())
        except Exception:                        # noqa: BLE001
            pass
    rid, count = _single_pending(handoff_dir)
    if rid is None:
        return "no_single_pending:%d" % count
    req = handoff.claim_request(handoff_dir, rid)
    if req is None:
        return "claim_failed"
    correlation_id = req.get("correlation_id") or rid
    started = clock()

    # ── ADR-0027 Phase 2 pre-probe DIRTY-BASELINE guard ──────────────────────────────────────────────────
    # A prior run whose POST-result baseline restore failed can leave the terminal non-baseline (a lingering
    # accounts.dat or logs). Fail closed BEFORE launching a credentialled login against a dirty terminal — the
    # operator-critical cleanup state from the previous run is caught here, never silently probed over.
    vdir = cfg.get("validation_terminal_dir") or ""
    if vdir:
        dirty = [c for c in (os.path.join(vdir, "config", "accounts.dat"),
                             os.path.join(vdir, "Config", "accounts.dat"),
                             os.path.join(vdir, "logs"), os.path.join(vdir, "Logs")) if os.path.exists(c)]
        if dirty:
            handoff.write_result(handoff_dir, rid,
                                 {"ok": False, "reason_code": "validation_baseline_dirty", "is_demo": None})
            if diag_dir:
                try:
                    diag.write_evidence(diag_dir, correlation_id,
                                        {"correlation_id": correlation_id, "request_id": rid,
                                         "reason_code": "validation_baseline_dirty",
                                         "cleanup_status": "baseline_dirty_preflight",
                                         "stage_reached": "PREFLIGHT", "first_failing_stage": "BASELINE_DIRTY"},
                                        now=clock())
                except Exception:            # noqa: BLE001
                    pass
            return "baseline_dirty"

    build = build_handler
    if build is None:
        import validate_login                    # noqa: PLC0415 — host-side import
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
    except Exception:                            # noqa: BLE001 — the handler never raises; stay fail-closed
        outcome = {"ok": False, "reason_code": "could_not_verify", "is_demo": None}
    if not isinstance(outcome, dict):
        outcome = {"ok": False, "reason_code": "could_not_verify", "is_demo": None}
    op = outcome.pop("_operator", {}) or {}      # operator diagnostic NEVER travels to the customer path
    iso = outcome.pop("_isolation", None)        # structured isolation diagnostic (isolation_check_failed only)

    forbidden = _forbidden_roots(cfg)            # ``vdir`` already resolved by the dirty-baseline guard above

    # ── EVIDENCE CAPTURE — durable, BEFORE any scrub (packet §2/§6). A fault here ⇒ diagnostic_capture_failed
    #    but the credential scrub still runs below (fail-safe). ──
    capture_failed = False
    evidence = {"correlation_id": correlation_id, "request_id": rid}
    try:
        adapter = win if win is not None else _default_win(cfg)
        term_procs = []
        try:
            term_procs = adapter.find_terminal_processes(vdir, forbidden)
        except Exception:                        # noqa: BLE001 — off-host / denied → no attribution
            term_procs = []
        term = term_procs[0] if term_procs else {}
        runner_pid = os.getpid()
        try:
            runner_session = adapter.session_of(runner_pid)
        except Exception:                        # noqa: BLE001
            runner_session = None
        journal_dirs = [os.path.join(vdir, "logs"), os.path.join(vdir, "Logs")] if vdir else []
        rj = read_journal or diag.read_journal_milestones
        try:
            milestones = rj(journal_dirs)
        except Exception:                        # noqa: BLE001
            milestones = []
        adat_path = None
        for cand in (os.path.join(vdir, "config", "accounts.dat"),
                     os.path.join(vdir, "Config", "accounts.dat")):
            if vdir and os.path.exists(cand):
                adat_path = cand
                break
        stages = _derive_stages(op, milestones)
        last_ok, first_fail = diag.stage_localisation(stages)
        if outcome.get("ok"):
            first_fail = None        # the login pipeline fully succeeded; remaining stages are cleanup, not failures
        # An isolation failure stops at STEP 1 (before envelope-open / MT5 launch). Label it explicitly so the
        # artefact localises to the isolation contract rather than the generic next stage.
        if isinstance(iso, dict) and iso.get("result") == "fail":
            last_ok, first_fail = "RUNNER_STARTED", "ISOLATION"
        evidence.update({
            "isolation": iso if isinstance(iso, dict) else None,
            "config_source": _config_source(cfg),
            "runner_executable": sys.executable,
            "attempt_start_utc": started, "runner_pid": runner_pid, "runner_session_id": runner_session,
            "terminal_pid": term.get("pid"), "terminal_session_id": term.get("session"),
            "terminal_path_classification": "isolated_validation" if term else "not_found",
            "initialize_started": op.get("initialize_started"), "initialize_result": op.get("initialize_result"),
            "last_error_code": op.get("last_error_code"), "last_error_reason": op.get("last_error_text"),
            "terminal_info_present": op.get("terminal_info_present"),
            "account_info_present": op.get("account_info_present"),
            "trade_mode": op.get("trade_mode"), "is_demo": outcome.get("is_demo"),
            "mt5_package_version": op.get("mt5_package_version"), "terminal_build": op.get("terminal_build"),
            "gui_mdi_failed": "GUI_MDI_CREATE_FAILED" in milestones,
            # ADR-0027 Phase 2 (B4): SEPARATE the API-confirmed broker authorisation (authoritative on a fast
            # success — initialize()=True + account_info + classification) from the journal/TCP/pipe polling,
            # which can miss a short-lived event on a ~4s login. Never infer an unobserved network event.
            "api_confirmed_authorisation": bool(op.get("initialize_result")
                                                and op.get("account_info_present")),
            "authorisation_observed": "BROKER_AUTHORISED" in milestones,   # journal-observed only
            "broker_tcp_observed": bool({"BROKER_TCP_ESTABLISHED", "BROKER_CONNECTED"} & set(milestones)),
            "ipc_pipe_observed": "IPC_PIPE_READY" in milestones,
            "journal_milestones": milestones, "reason_code": outcome.get("reason_code"),
            "stage_reached": last_ok, "first_failing_stage": first_fail,
            "accounts_dat_created": adat_path is not None,
            "accounts_dat_size": (os.path.getsize(adat_path) if adat_path else None),
            "accounts_dat_mtime": (os.path.getmtime(adat_path) if adat_path else None),
            "shutdown_requested": True,
        })
        if diag_dir:
            diag.write_evidence(diag_dir, correlation_id, evidence, now=clock())   # DURABLE before scrub
    except Exception:                            # noqa: BLE001 — capture failed; still scrub below
        capture_failed = True

    # ── deterministic termination (mt5.shutdown is insufficient), path-guarded ──
    term_result = {}
    try:
        adapter = win if win is not None else _default_win(cfg)
        term_result = adapter.terminate_terminal_processes(vdir, forbidden)
    except Exception:                            # noqa: BLE001
        term_result = {"error": "terminate_unavailable"}

    # ── credential-artefact scrub — ALWAYS (even if capture failed). The terminal is terminated ABOVE first,
    #    so accounts.dat is not held open; then VERIFY it is gone (a lingering terminal that defeated the scrub
    #    must not be reported as clean). ──
    _scrub_validation_terminal(cfg)
    accounts_dat_removed = True
    if vdir:
        for cand in (os.path.join(vdir, "config", "accounts.dat"),
                     os.path.join(vdir, "Config", "accounts.dat")):
            if os.path.exists(cand):
                accounts_dat_removed = False

    # ── ADR-0027 Phase 2 RESULT ORDERING ────────────────────────────────────────────────────────────────
    # The terminal is terminated and the credential is SCRUBBED above, so write the customer-safe result NOW —
    # BEFORE the (potentially slow) baseline mirror — so the Agent receives the runner's real classification
    # inside its result-wait window instead of ``validation_runner_timeout``. The baseline restore runs AFTER;
    # its status rides in the durable artefact + the return code (never hidden, never flips the correct
    # verdict). A HEALTHY verdict is reachable here only because the scrub already ran; a scrub that could NOT
    # be verified downgrades the verdict fail-closed (never HEALTHY while a credential artefact lingers).
    login_succeeded = bool(outcome.get("ok"))
    if capture_failed and not login_succeeded:
        customer = {"ok": False, "reason_code": "diagnostic_capture_failed", "is_demo": None}
    else:
        customer = {"ok": login_succeeded,
                    "reason_code": str(outcome.get("reason_code") or "could_not_verify"),
                    "is_demo": outcome.get("is_demo")}
    if not accounts_dat_removed:
        customer = {"ok": False, "reason_code": "credential_scrub_unverified", "is_demo": None}
        login_succeeded = False
    op_summary = (diag.operator_summary(diag.build_evidence(evidence))
                  if not capture_failed else {"evidence_id": correlation_id})
    handoff.write_result(handoff_dir, rid, customer, operator=op_summary)

    # ── restore the certified precompiled baseline (POST-result cleanup; status is operator-visible) ──
    restore_result = "skipped"
    precompiled = cfg.get("validation_precompiled_dir")
    if precompiled and vdir:
        try:
            mb = mirror_baseline or _mirror_os
            restore_result = mb(precompiled, vdir)
        except Exception:                        # noqa: BLE001
            restore_result = "restore_failed"

    # ADR-0027 Phase 2 (B3): a SINGLE operator-facing cleanup state (NEVER in the customer contract). It is the
    # defined consumer/return of a post-result cleanup failure — surfaced in the durable artefact + the runner
    # return (host log / task LastResult). ``baseline_restore_failed`` means the active image is UNAVAILABLE
    # until re-baselined, which the NEXT request's dirty-baseline guard enforces fail-closed.
    if not accounts_dat_removed:
        cleanup_status = "credential_scrub_failed"
    elif restore_result == "restore_failed":
        cleanup_status = "baseline_restore_failed"
    else:
        cleanup_status = "cleanup_complete"

    finished = clock()
    if not capture_failed and diag_dir:
        try:
            evidence.update({
                "attempt_finish_utc": finished, "elapsed_ms": int((finished - started) * 1000),
                "cleanup_started": True, "cleanup_finished": True, "shutdown_requested": True,
                "cleanup_status": cleanup_status,
                "terminal_exited_after_shutdown": not term_result.get("remaining"),
                "stray_termination_attempted": bool(term_result.get("targets")),
                "stray_termination_result": term_result,
                "baseline_restore_result": restore_result,
                "accounts_dat_created": not accounts_dat_removed and evidence.get("accounts_dat_created"),
                "final_baseline_fingerprint": _light_fingerprint(vdir) if vdir else None,
            })
            diag.write_evidence(diag_dir, correlation_id, evidence, now=clock())
        except Exception:                        # noqa: BLE001
            pass

    # The customer result was written BEFORE this slow restore. The runner return is the OPERATOR cleanup state,
    # never the customer verdict.
    if capture_failed and not login_succeeded:
        return "diagnostic_capture_failed"
    if capture_failed:
        return "capture_degraded_login_ok"
    return cleanup_status


def main() -> int:
    import config                                # noqa: PLC0415 — host-side import (agent config loader)
    status = run_once(config.load_config())
    print("validation_runner:", status)          # non-secret status only; captured by the task's own log
    return 0


if __name__ == "__main__":
    sys.exit(main())
