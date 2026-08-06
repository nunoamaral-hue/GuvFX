"""ADR-0027 observability enhancement (2026-08-03) — SECRET-SAFE validation diagnostics.

The final credentialed VALIDATE_LOGIN returned ``login_timeout`` AFTER a broker TCP connection was
established, but the decisive terminal-journal lines were LOST: the runner scrubbed the logs before any
preservation, and the isolated terminal kept running after ``mt5.shutdown()``. This module gives the runner
the ability to preserve a durable, bounded, secret-safe diagnostic artefact per attempt — so a single future
validation can localise exactly where the login stops.

Design invariants:
  * **Pure stdlib.** Nothing here imports Django, crypto keys, MetaTrader5, or any Windows API. The journal
    parse, redaction, stage model, evidence assembly and termination-path guard are all unit-testable off the
    host. Process enumeration/termination itself lives in the Windows adapter (``win_slot_ops``); this module
    only decides WHICH paths are safe to terminate.
  * **Milestones, not raw text.** A terminal journal can in principle contain a login number; we therefore map
    journal lines to a FIXED allow-list of milestone CODES and never persist broad raw journal text.
  * **Secret-safe by construction.** Every value that goes into the durable artefact is passed through a
    secret scan that drops anything resembling a password/token/key/ciphertext, and long values are bounded.
    The evidence dict is filtered to an ALLOW-LIST of keys — an unknown key can never be persisted.
  * **Never raises in the hot path.** The runner calls into here around a live probe; a diagnostics fault must
    degrade to "capture failed", never crash the probe or leak a secret.
"""
from __future__ import annotations

import json
import os
import re

SCHEMA_VERSION = "2026-08-03.1"

# ── stage model (packet §4) ──────────────────────────────────────────────────────────────────────────────
# Ordered; the runner marks each stage reached. ``first_failing_stage`` is the first NOT reached.
STAGES = (
    "REQUEST_ACCEPTED", "RUNNER_STARTED", "ENVELOPE_OPENED", "TERMINAL_LAUNCHED", "GUI_READY",
    "IPC_READY", "BROKER_TCP_CONNECTED", "BROKER_AUTHORISED", "ACCOUNT_INFO_READY", "CLASSIFIED",
    "SHUTDOWN_REQUESTED", "TERMINAL_EXITED", "BASELINE_RESTORED", "COMPLETE",
)

# ── journal milestone codes (packet §2 allow-list) ───────────────────────────────────────────────────────
MILESTONE_CODES = frozenset({
    "TERMINAL_STARTED", "DATA_PATH_READY", "GUI_MAIN_WINDOW_CREATED", "GUI_MDI_CREATE_FAILED",
    "IPC_PIPE_READY", "BROKER_CONNECTING", "BROKER_TCP_ESTABLISHED", "BROKER_CONNECTED",
    "BROKER_AUTHORISED", "BROKER_LOGIN_FAILED", "INVALID_LOGIN", "ACCOUNT_DISABLED",
    "SERVER_UNAVAILABLE", "ACCOUNT_INFO_READY", "TERMINAL_STOPPED", "MCP_BIND_CONFLICT",
    "UNKNOWN_SAFE_MILESTONE",
})

# Ordered SPECIFIC-FAILURE → SUCCESS → GENERAL: first matching pattern wins per line. The FAILURE
# classifications MUST precede the generic ``authoris(ed)`` success token, or a genuine denial such as MT5's
# literal ``authorization failed`` / ``not authorized`` would be misread as the SUCCESS milestone (review
# 2026-08-03). Patterns run on a LOWERCASED line; only the resulting CODE is retained — never the matched
# text. Word boundaries are chosen so real MT5 wordings match: ``fail`` (a prefix, so it catches ``failed``),
# not ``fail\b`` (which would not).
_JOURNAL_PATTERNS = (
    (r"\bmetatrader 5 .*\bstarted\b", "TERMINAL_STARTED"),
    (r"\bmdi\b.*\b(create|unhook)\b.*fail", "GUI_MDI_CREATE_FAILED"),
    (r"\bcreate (new )?frame\b.*fail", "GUI_MDI_CREATE_FAILED"),
    (r"\bmain window\b|\bchart\b.*\bopened\b", "GUI_MAIN_WINDOW_CREATED"),
    (r"\bbind error\b.*22346|\bmcp\b.*\bbind", "MCP_BIND_CONFLICT"),
    (r"\bnamed pipe\b|\bipc\b.*\bready\b|\bpipe\b.*\bcreated\b", "IPC_PIPE_READY"),
    # ── failure classifications FIRST (specific) ──
    (r"\binvalid account\b|\baccount .*not found\b|\bno such account\b", "INVALID_LOGIN"),
    (r"\b(account|login)\b.*(disabled|blocked|closed|suspended)", "ACCOUNT_DISABLED"),
    (r"\bunknown server\b|\bserver .*not found\b", "SERVER_UNAVAILABLE"),
    (r"\bnot (auth|logg)|(auth\w*|login|logon)\s*(is\s*)?(fail|reject|denied|error|invalid)|"
     r"(fail|reject|denied|error)\w*.{0,20}(auth|login|logon)", "BROKER_LOGIN_FAILED"),
    # ── then the SUCCESS token (only after every failure form is ruled out above) ──
    (r"\bauthoris(ed|ation)\b|\bauthoriz(ed|ation)\b", "BROKER_AUTHORISED"),
    (r"\bconnected to\b|\bconnection established\b|socket .*connected", "BROKER_TCP_ESTABLISHED"),
    (r"\bnetwork\b.*\bconnected\b", "BROKER_CONNECTED"),
    (r"\bconnecting to\b|\bconnect to\b", "BROKER_CONNECTING"),
    (r"\baccount\b.*\b(balance|equity|leverage|trade_mode|company)\b", "ACCOUNT_INFO_READY"),
    (r"\bterminal\b.*\b(stopped|shutdown)\b", "TERMINAL_STOPPED"),
    (r"\bdata (folder|path)\b|\b\\terminal\b", "DATA_PATH_READY"),
)
_COMPILED_PATTERNS = tuple((re.compile(p), code) for p, code in _JOURNAL_PATTERNS)

# ── secret scan ──────────────────────────────────────────────────────────────────────────────────────────
# A value that MATCHES any of these is never persisted (dropped/masked). This is a defence-in-depth guard on
# top of the milestone-code discipline: even if a raw string slips into the evidence dict, it cannot carry a
# secret through this filter.
_SECRET_HINT = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|priv(ate)?[_-]?key|hmac|cipher|enc[_-]?key|"
    r"-----BEGIN|ssh-rsa|bearer\s)", re.IGNORECASE)
_MAX_STR = 512            # any retained string is bounded (a journal excerpt cannot grow unbounded)
_MAX_LIST = 200           # milestone / list fields are bounded


def looks_secret(value) -> bool:
    """True if a string plausibly carries a secret. Non-strings are never secret by this check."""
    return isinstance(value, str) and bool(_SECRET_HINT.search(value))


def _scrub_scalar(value):
    if isinstance(value, str):
        if looks_secret(value):
            return "[REDACTED]"
        return value[:_MAX_STR]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    # any other type is coerced to a bounded repr and re-scanned
    s = str(value)[:_MAX_STR]
    return "[REDACTED]" if looks_secret(s) else s


def scrub(value):
    """Recursively drop/mask secret-looking strings and bound sizes. Never raises."""
    try:
        if isinstance(value, dict):
            return {str(k)[:64]: scrub(v) for k, v in list(value.items())[:_MAX_LIST]}
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in list(value)[:_MAX_LIST]]
        return _scrub_scalar(value)
    except Exception:            # noqa: BLE001 — scrubbing must never crash the diagnostics path
        return "[REDACTED]"


# ── journal decode + milestone extraction ────────────────────────────────────────────────────────────────
def decode_journal_bytes(raw: bytes) -> str:
    """Decode a terminal-journal byte string. MT5 journals are commonly UTF-16; a BOM-less UTF-16 file read as
    ANSI is the RULE-11 trap, so we detect UTF-16 explicitly (BOM, then a NUL-density heuristic) before
    falling back to UTF-8. Always returns a string (never raises)."""
    if not raw:
        return ""
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16")                 # BOM-driven (LE or BE)
        if raw[:3] == b"\xef\xbb\xbf":
            return raw.decode("utf-8-sig")
        # BOM-less: a high NUL density on even/odd byte positions is UTF-16 (RULE 11 — do not guess ANSI).
        sample = raw[:4096]
        nul_even = sample[0::2].count(0)
        nul_odd = sample[1::2].count(0)
        if len(sample) >= 8 and (nul_odd > len(sample) // 4):
            return raw.decode("utf-16-le", errors="replace")
        if len(sample) >= 8 and (nul_even > len(sample) // 4):
            return raw.decode("utf-16-be", errors="replace")
        return raw.decode("utf-8", errors="replace")
    except Exception:            # noqa: BLE001
        return raw.decode("latin-1", errors="replace")


def extract_milestones(text: str) -> list:
    """Map journal lines to the allow-listed milestone CODES (deduped, order-preserving). Raw text is NEVER
    returned — only codes. A line that matches nothing contributes no code (not UNKNOWN_SAFE_MILESTONE, which
    is reserved for an explicit-but-unclassified marker a caller may add)."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        low = line.lower()
        for rx, code in _COMPILED_PATTERNS:
            if rx.search(low):
                if code not in seen:
                    seen.add(code)
                    out.append(code)
                break
        if len(out) >= _MAX_LIST:
            break
    return out


def _default_shared_open(path: str) -> bytes:
    """Read a possibly-locked journal with shared read/write (MT5 holds the file open). Pure ``os`` — no
    Windows API. Returns b"" if unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return b""
    try:
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            if sum(len(c) for c in chunks) > 4 * 1024 * 1024:      # bound: never slurp an unbounded journal
                break
        return b"".join(chunks)
    except OSError:
        return b""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_journal_milestones(journal_dirs, *, list_dir=None, reader=None) -> list:
    """Find the newest ``*.log`` under any of ``journal_dirs`` and return its allow-listed milestone codes.
    ``list_dir(dir)->[(name, mtime)]`` and ``reader(path)->bytes`` are injectable for tests; the defaults use
    ``os`` with a shared-read open. Never raises; returns [] when no journal is present/readable."""
    list_dir = list_dir or _os_list_logs
    reader = reader or _default_shared_open
    best_path, best_mtime = None, -1.0
    for d in journal_dirs:
        for name, mtime in list_dir(d):
            if name.lower().endswith(".log") and mtime > best_mtime:
                best_path, best_mtime = os.path.join(d, name), mtime
    if best_path is None:
        return []
    return extract_milestones(decode_journal_bytes(reader(best_path)))


def _os_list_logs(directory: str):
    try:
        with os.scandir(directory) as it:
            return [(e.name, e.stat().st_mtime) for e in it if e.is_file()]
    except OSError:
        return []


# ── termination path guard (mirrors validate_login's containment helpers to avoid an import cycle) ─────────
def _norm(path: str) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").lower()


def _beneath(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return bool(r) and (p == r or p.startswith(r + "\\"))


def _has_traversal(path: str) -> bool:
    return any(seg in ("..", ".") for seg in _norm(path).split("\\"))


def is_terminatable(exe_path: str, terminal_dir: str, forbidden_roots=()) -> bool:
    """True ONLY if ``exe_path`` is a real, canonical path strictly beneath the ONE isolated validation
    terminal dir and beneath NO forbidden root (slots / golden / accounts / production). Fail-closed: a
    missing/relative/traversal path is never terminatable. This is the guard that makes it impossible to kill
    Customer Zero, a slot runtime, or a production terminal."""
    if not exe_path or not terminal_dir:
        return False
    if _has_traversal(exe_path) or _has_traversal(terminal_dir):
        return False
    # A bare-drive containment root (``C:\``) would make EVERY path "beneath" it and collapse the guard — the
    # root must be a proper contained directory (review 2026-08-03), mirroring assert_isolated_validation_terminal.
    if "\\" not in _norm(terminal_dir):
        return False
    if not _beneath(exe_path, terminal_dir):
        return False
    for forbidden in forbidden_roots:
        if forbidden and _beneath(exe_path, forbidden):
            return False
    return True


def select_terminatable(procs, terminal_dir: str, forbidden_roots=()) -> list:
    """From ``procs`` = iterable of ``(pid, exe_path)``, return the pids safe to terminate (guarded by
    ``is_terminatable``). Pure logic — the actual kill is the adapter's job."""
    out = []
    for pid, exe_path in procs:
        if is_terminatable(exe_path, terminal_dir, forbidden_roots):
            out.append(int(pid))
    return out


# ── evidence assembly (packet §1 allow-list) ─────────────────────────────────────────────────────────────
ALLOWED_EVIDENCE_KEYS = frozenset({
    "schema_version", "correlation_id", "request_id", "attempt_start_utc", "attempt_finish_utc",
    "elapsed_ms", "runner_pid", "runner_session_id", "terminal_pid", "terminal_session_id",
    "terminal_path_classification", "terminal_launch_utc", "task_last_run_time", "task_last_result",
    "mt5_package_version", "terminal_build", "initialize_started", "initialize_finished",
    "initialize_result", "last_error_code", "last_error_reason", "terminal_info_present",
    "account_info_present", "login_masked", "server", "trade_mode", "is_demo", "ipc_pipe_observed",
    "broker_tcp_observed", "broker_endpoint", "accounts_dat_created", "accounts_dat_size",
    "accounts_dat_mtime", "gui_mdi_failed", "authorisation_observed", "journal_milestones",
    "reason_code", "stage_reached", "first_failing_stage", "cleanup_started", "cleanup_finished",
    "shutdown_requested", "terminal_exited_after_shutdown", "stray_termination_attempted",
    "stray_termination_result", "baseline_restore_result", "final_baseline_fingerprint",
    "handoff_cleanup_result",
    # ISOLATION diagnostics (ADR-0027 runner-isolation packet, 2026-08-06): a structured, secret-safe record of
    # the effective isolated-terminal contract inputs and the exact failing rule — paths + booleans only, no
    # secret. ``config_source``/``runner_executable`` show HOW the task-launched runner resolved its config.
    "isolation", "config_source", "runner_executable",
})

# Keys inside the nested ``isolation`` object (allow-list applied recursively in build_evidence). Paths + a
# fixed set of booleans/labels — never a secret. An unknown nested key is dropped, same as the top level.
_ALLOWED_ISOLATION_KEYS = frozenset({
    "result", "sub_reason", "validation_dir", "validation_dir_canonical", "validation_root",
    "validation_root_canonical", "forbidden_roots", "matched_forbidden_root", "terminal_path",
    "terminal_exists", "checks",
})
_ALLOWED_ISOLATION_CHECK_KEYS = frozenset({
    "absolute", "no_traversal", "beneath_root", "disjoint", "terminal_present",
})


def stage_localisation(stages_reached) -> tuple:
    """Return ``(last_successful_stage, first_failing_stage)``. ``stages_reached`` is a set/list of stage
    names actually reached. The first failing stage is the earliest STAGES entry NOT reached (or None if all
    reached). A stage is only 'reached' if every earlier stage was reached — a later process staying alive is
    NOT evidence of an earlier stage (packet §4)."""
    reached = set(stages_reached or ())
    last_ok, first_fail = None, None
    for st in STAGES:
        if st in reached and first_fail is None:
            last_ok = st
        elif first_fail is None:
            first_fail = st
    return last_ok, first_fail


def _filter_isolation(iso):
    """Restrict the (already-scrubbed) isolation object to its allow-listed nested keys — an unknown nested key
    is dropped, mirroring the top-level allow-list, so nothing unexpected can ever be persisted inside the
    isolation section. A non-dict becomes ``None``."""
    if not isinstance(iso, dict):
        return None
    out = {k: v for k, v in iso.items() if k in _ALLOWED_ISOLATION_KEYS}
    if isinstance(out.get("checks"), dict):
        out["checks"] = {k: v for k, v in out["checks"].items() if k in _ALLOWED_ISOLATION_CHECK_KEYS}
    elif "checks" in out:
        out["checks"] = None
    return out


def build_evidence(fields: dict) -> dict:
    """Filter to the allow-list and scrub every value. An unknown key is dropped (never persisted); every
    value passes the secret scan. The nested ``isolation`` object is additionally restricted to its own
    allow-list. Always stamps the schema version."""
    out = {"schema_version": SCHEMA_VERSION}
    for k, v in (fields or {}).items():
        if k in ALLOWED_EVIDENCE_KEYS:
            out[k] = _filter_isolation(scrub(v)) if k == "isolation" else scrub(v)
    return out


def operator_summary(evidence: dict) -> dict:
    """The compact operator-diagnostic result (packet §3) — a small, secret-safe subset that can travel back
    with the outcome. Never includes raw journal text or host secrets."""
    e = evidence or {}
    iso = e.get("isolation") if isinstance(e.get("isolation"), dict) else {}
    return {
        "evidence_id": e.get("correlation_id"),
        "stage_reached": e.get("stage_reached"),
        "first_failing_stage": e.get("first_failing_stage"),
        "last_error_code": e.get("last_error_code"),
        "last_error_reason": e.get("last_error_reason"),
        "cleanup_status": e.get("baseline_restore_result"),
        "terminal_exit_status": e.get("terminal_exited_after_shutdown"),
        # ISOLATION localisation (present only when the isolated-terminal contract failed) — sub-reason + the
        # forbidden root that matched (if any). Secret-safe: labels/paths only.
        "isolation_sub_reason": iso.get("sub_reason"),
        "isolation_matched_forbidden_root": iso.get("matched_forbidden_root"),
    }


# ── durable, bounded, expiring artefact store ────────────────────────────────────────────────────────────
_EVIDENCE_SUFFIX = ".diag.json"


def write_evidence(diag_dir: str, correlation_id: str, evidence: dict, *, now: float) -> str:
    """Write the secret-safe evidence JSON atomically under ``diag_dir``. The directory ACL (SYSTEM +
    Administrators + the agent service) is applied by the installer; this only writes the file. Returns the
    path. Raises on I/O failure so the runner can fall back to ``diagnostic_capture_failed``."""
    os.makedirs(diag_dir, exist_ok=True)
    safe = build_evidence(evidence)
    safe["_written_utc"] = float(now)
    cid = re.sub(r"[^A-Za-z0-9._-]", "_", str(correlation_id or "unknown"))[:120]
    path = os.path.join(diag_dir, cid + _EVIDENCE_SUFFIX)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(safe, fh, sort_keys=True)
    os.replace(tmp, path)
    return path


def sweep_expired(diag_dir: str, *, max_age_s: float, now: float) -> int:
    """Delete diagnostic artefacts older than ``max_age_s`` (bounded retention, packet §7). Returns the count
    removed. Never raises; a malformed/foreign file is left alone unless it is our suffix and stale."""
    removed = 0
    try:
        names = os.listdir(diag_dir)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(_EVIDENCE_SUFFIX):
            continue
        p = os.path.join(diag_dir, name)
        try:
            if float(now) - os.path.getmtime(p) > float(max_age_s):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed
