"""ADR-0027 — AGENT-side broker-login validation handler (op VALIDATE_LOGIN).

The single, non-destructive, runtime-INDEPENDENT login probe. It runs against a DEDICATED ISOLATED
validation terminal (never a running beta slot, never the golden image, never Nuno's live terminal) so a
probe can NEVER touch a customer's or the operator's session. It:

  1. re-affirms the isolated-terminal CONTRACT (a fixed, contained, disjoint directory with a terminal
     executable) and FAILS CLOSED on any violation — no path ever comes from the request;
  2. opens the envelope-encrypted password under the request's own AAD (bound to op/runtime/correlation/
     nonce) — a lifted ciphertext fails here;
  3. takes a GLOBAL single-flight lock (one login probe on the box at a time — the terminal directory is a
     single-instance resource);
  4. runs a minimal MT5 probe: ``initialize(path, login, password, server) → account_info().trade_mode``.
     It calls NO order, symbol, position or history API — the probe surface is login + classify ONLY;
  5. ALWAYS shuts the terminal down and drops the plaintext, on success AND on every failure;
  6. returns an allowlisted, secret-free ``{ok, reason_code, is_demo}`` — never a password, ciphertext,
     host path, or raw MT5 error string.

Classification precision (which MT5 error → which taxonomy code) is CALIBRATED under host certification
with positive+negative controls (security RULE 11); until then an unrecognised failure is the conservative
retryable ``could_not_verify`` — the probe never falsely blames the customer's credentials.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

import validation_handoff as handoff

_log = logging.getLogger("guvfx.beta.validate_login")


def _safe_corr(correlation_id) -> str:
    """Bounded, character-safe correlation id for a log line — never a path/secret, and a malformed id can never
    inject control characters into the operator log."""
    s = str(correlation_id or "unknown")
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)[:120]


# The VALIDATE_LOGIN customer reason for an isolated-terminal contract failure. Named (not an inline literal) so
# it is defined ONCE and referenced by both the fail-closed result and the diagnostic write; it is a
# VALIDATE-taxonomy code (classified by the backend broker-login taxonomy), NOT an agent-lifecycle op reason —
# exactly like its sibling ``_denied`` reasons (invalid_login, credential_unsealable, ...).
ISOLATION_CHECK_FAILED = "isolation_check_failed"

# ── isolated-terminal contract ─────────────────────────────────────────────────────────────────────────────
#: The ONE dedicated root the validation terminal must live beneath. A fixed namespace, never request-derived.
DEFAULT_VALIDATION_ROOT = r"C:\GuvFX\beta\validation"
VALIDATION_EXE_NAME = "terminal64.exe"
#: Roots the validation terminal must be DISJOINT from — running slots, both golden locations, the per-account
#: runtime tree. Neither may contain the other. Operator estate roots are added via config.
DEFAULT_FORBIDDEN_ROOTS = (
    r"C:\GuvFX\beta\slots",
    r"C:\GuvFX\beta\golden",
    r"C:\GuvFX\golden",
    r"C:\GuvFX\beta\accounts",
)


class IsolationError(Exception):
    """The validation terminal failed its isolation contract. ``reason_code`` is sanitised."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _norm(path: str) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").lower()


def _beneath(path: str, root: str) -> bool:
    """True iff ``path`` is ``root`` or strictly beneath it (case-insensitive, boundary-safe)."""
    p, r = _norm(path), _norm(root)
    return bool(r) and (p == r or p.startswith(r + "\\"))


def _is_absolute_windows(path: str) -> bool:
    p = (path or "").replace("/", "\\")
    return len(p) >= 3 and p[1] == ":" and p[2] == "\\" and p[0].isalpha()


def _has_traversal(path: str) -> bool:
    """True if any component is ``.`` or ``..``. The containment checks are LEXICAL (string prefix), but the
    OS + MT5 resolve ``..`` — so ``C:\\GuvFX\\beta\\validation\\..\\slots\\1`` would pass the isolated-root
    prefix yet resolve INTO a live slot. Reject traversal outright rather than trust a lexical prefix."""
    return any(seg in ("..", ".") for seg in _norm(path).split("\\"))


def _assert_isolated_dir(validation_dir, *, validation_root=DEFAULT_VALIDATION_ROOT,
                         forbidden_roots=DEFAULT_FORBIDDEN_ROOTS) -> None:
    """The PATH-CONTRACT portion of the isolated-terminal check (rules 1-4: absolute, valid root, no traversal,
    beneath root, disjoint from every forbidden root). Fail-closed: raises ``IsolationError`` with the SAME
    sub-reasons as the full check. It does NOT require the executable to EXIST — that ``terminal_present`` rule
    is checked only by ``assert_isolated_validation_terminal``. Reused as the containment guard for BOTH the
    destination and the precompiled SOURCE before any materialisation, so a copy can never write into a
    non-isolated dest nor read FROM a forbidden/golden/live/slot source (ONE source of truth for the rules)."""
    if not validation_dir or not _is_absolute_windows(validation_dir):
        raise IsolationError("validation_terminal_unconfigured")
    # A bare-drive validation ROOT (``C:\``) would make EVERY absolute path pass the prefix test, collapsing
    # the isolation guarantee — the root itself must be a proper contained directory, and neither the dir nor
    # the root may contain a ``..``/``.`` component that the OS would resolve past the lexical boundary.
    if "\\" not in _norm(validation_root) or _has_traversal(validation_root):
        raise IsolationError("validation_terminal_unconfigured")
    if _has_traversal(validation_dir):
        raise IsolationError("validation_terminal_not_isolated")
    if not _beneath(validation_dir, validation_root):
        raise IsolationError("validation_terminal_not_isolated")
    for forbidden in forbidden_roots:
        # DISJOINT: the validation dir may neither sit under a forbidden root nor contain one. This is what
        # guarantees a probe cannot reach a running slot, the golden image or a per-account runtime.
        if _beneath(validation_dir, forbidden) or _beneath(forbidden, validation_dir):
            raise IsolationError("validation_terminal_not_isolated")


def assert_isolated_validation_terminal(validation_dir, *, validation_root=DEFAULT_VALIDATION_ROOT,
                                        forbidden_roots=DEFAULT_FORBIDDEN_ROOTS,
                                        exe_name=VALIDATION_EXE_NAME, path_exists) -> str:
    """Prove the validation terminal is the dedicated, isolated one and return its executable path. Every
    failure raises ``IsolationError`` (fail-closed) — a probe is NEVER attempted against an unproven path. The
    path-contract rules are UNCHANGED (delegated to ``_assert_isolated_dir``); this then requires the terminal
    executable to actually exist (``terminal_present``)."""
    _assert_isolated_dir(validation_dir, validation_root=validation_root, forbidden_roots=forbidden_roots)
    # containment already proven on the normalised form; build the launch path in original case
    exe_path = validation_dir.replace("/", "\\").rstrip("\\") + "\\" + exe_name
    if not path_exists(exe_path):
        raise IsolationError("validation_terminal_missing")
    return exe_path


def prepare_validation_terminal(validation_dir, *, validation_root, forbidden_roots, precompiled_dir, mirror) -> str:
    """Deterministically MATERIALISE/restore the isolated validation terminal from the certified precompiled
    baseline BEFORE the isolation gate runs, so ``terminal_present`` holds on the active in-process path (whose
    proven blocker was ``validation_terminal_missing``). REUSES the runner's already-proven mirror primitive
    (source-validated + reparse-safe + deletes-extras) — no parallel copy design.

    NEVER raises and NEVER weakens the isolation gate. It refuses to copy unless BOTH the destination AND the
    precompiled SOURCE independently satisfy the isolated-path contract (so it can neither write into a
    non-isolated dest nor read FROM a forbidden/golden/live/slot source); if either fails the contract, or the
    source is not configured/valid, it SKIPS the copy and lets the authoritative isolation gate report the exact
    failing rule with its full diagnostic. Returns a fixed, secret-safe status LABEL:
      * ``restored`` — the baseline was mirrored in (terminal now present);
      * ``precompiled_unconfigured`` — no precompiled dir configured;
      * ``path_contract_unmet`` — dest or source is not an isolated path (isolation gate will report the rule);
      * ``no_source`` / ``invalid_source`` — the precompiled source is missing or lacks ``terminal64.exe``.
    """
    if not precompiled_dir:
        return "precompiled_unconfigured"
    try:
        _assert_isolated_dir(validation_dir, validation_root=validation_root, forbidden_roots=forbidden_roots)
        _assert_isolated_dir(precompiled_dir, validation_root=validation_root, forbidden_roots=forbidden_roots)
    except IsolationError:
        return "path_contract_unmet"                     # do NOT copy; the isolation gate reports the exact rule
    try:
        return mirror(precompiled_dir, validation_dir)   # runner primitive: "restored"|"no_source"|"invalid_source"
    except Exception:                                    # noqa: BLE001 — materialisation must never break validate
        return "mirror_failed"


def isolation_report(validation_dir, *, validation_root=DEFAULT_VALIDATION_ROOT,
                     forbidden_roots=DEFAULT_FORBIDDEN_ROOTS, exe_name=VALIDATION_EXE_NAME,
                     path_exists) -> dict:
    """SECRET-SAFE structured evaluation of the isolated-terminal contract, for the on-host operator diagnostic
    (ADR-0027 §2). Unlike ``assert_isolated_validation_terminal`` — which short-circuits and raises the FIRST
    ``IsolationError`` — this evaluates EVERY rule independently and returns which rule fails, on what EFFECTIVE
    path, and against which forbidden root, so an ``isolation_check_failed`` can be localised without host access.

    It is DIAGNOSTICS ONLY: it performs no mutation, carries only paths + booleans (never a credential/token/
    key), and NEVER changes the fail-closed decision or the customer reason code — the authoritative gate stays
    ``assert_isolated_validation_terminal`` and the customer reason stays ``isolation_check_failed``. ``sub_reason``
    is one of the reasons the code actually enforces (or ``None`` on pass). Never raises."""
    vdir = validation_dir or ""
    vroot = validation_root or ""
    absolute = bool(vdir) and _is_absolute_windows(vdir)
    root_valid = ("\\" in _norm(vroot)) and not _has_traversal(vroot)
    no_traversal = not _has_traversal(vdir)
    beneath_root = _beneath(vdir, vroot)
    matched = None
    for f in forbidden_roots:
        if f and (_beneath(vdir, f) or _beneath(f, vdir)):
            matched = f
            break
    disjoint = matched is None
    terminal_path = (vdir.replace("/", "\\").rstrip("\\") + "\\" + exe_name) if vdir else ""
    try:
        terminal_present = bool(terminal_path) and bool(path_exists(terminal_path))
    except Exception:            # noqa: BLE001 — a path check must never crash diagnostics
        terminal_present = False
    # sub_reason precedence MIRRORS the order assert_isolated_validation_terminal enforces its rules, so the
    # diagnostic names the same rule the gate would fail on (just more granularly than the collapsed raises).
    if not absolute:
        sub_reason = "validation_terminal_unconfigured"
    elif not root_valid:
        sub_reason = "validation_root_invalid"
    elif not no_traversal:
        sub_reason = "validation_terminal_traversal"
    elif not beneath_root:
        sub_reason = "validation_terminal_outside_root"
    elif not disjoint:
        sub_reason = "validation_terminal_not_isolated"
    elif not terminal_present:
        sub_reason = "validation_terminal_missing"
    else:
        sub_reason = None
    return {
        "result": "pass" if sub_reason is None else "fail",
        "sub_reason": sub_reason,
        "validation_dir": vdir,
        "validation_dir_canonical": _norm(vdir),
        "validation_root": vroot,
        "validation_root_canonical": _norm(vroot),
        "forbidden_roots": [f for f in forbidden_roots if f],
        "matched_forbidden_root": matched,
        "terminal_path": terminal_path,
        "terminal_exists": terminal_present,
        "checks": {
            "absolute": absolute,
            "no_traversal": no_traversal,
            "beneath_root": beneath_root,
            "disjoint": disjoint,
            "terminal_present": terminal_present,
        },
    }


# ── MT5 error → customer-safe taxonomy (calibrated under host certification; RULE 11) ────────────────────────
def classify_init_error(code, text: str) -> str:
    """Map an MT5 ``initialize`` failure ``(code, text)`` to a taxonomy reason. Text-first, then code; an
    unrecognised failure is the conservative retryable ``could_not_verify`` (never a false credential blame).
    The exact code↔reason table is a host-certification deliverable with positive+negative controls."""
    t = (text or "").lower()
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = None
    # LOCAL validation-host / MT5-terminal IPC failure (2026-08-05 incident, WS-A). The MetaTrader5 Python
    # package could not establish its IPC channel to ITS OWN terminal process — this happens BEFORE any broker
    # is contacted (a Session-0 GUI/window-station readiness condition), so it is a PLATFORM/host fault, never
    # a broker outage and never the customer's credentials. MT5 code -10004 is RES_E_INTERNAL_FAIL_CONNECT (the
    # Python↔terminal IPC connect, ALWAYS local); its canonical texts are "No IPC connection"/"IPC timeout"/
    # "IPC initialize failed"/"IPC recv failed". Kept FIRST so a generic "connection"/"network" token can never
    # mis-route a local IPC failure to a broker reason (the exact defect this replaces).
    # -10004 = RES_E_INTERNAL_FAIL_CONNECT and -10005 = RES_E_INTERNAL_FAIL_TIMEOUT are BOTH members of the
    # local RES_E_INTERNAL_FAIL_* IPC family (the Python↔terminal channel connecting / timing out) — an
    # INTERNAL timeout, never a broker/login timeout. Both handled here, FIRST — before the generic text
    # "timeout" rule below — so a local IPC timeout can never be mis-routed to ``login_timeout`` (Phase-4 WS-C).
    if c == -10004 or c == -10005 or "no ipc" in t or "ipc connection" in t or "ipc timeout" in t \
            or "ipc initialize" in t or "ipc recv" in t or "ipc send" in t:
        return "validation_ipc_unavailable"
    if "invalid account" in t or ("account" in t and "not found" in t):
        return "invalid_login"
    if "disabled" in t or "blocked" in t or "closed" in t:
        return "account_disabled"
    if "invalid password" in t or "authorization failed" in t or "authorisation failed" in t:
        return "invalid_password"
    if "timeout" in t or "timed out" in t:
        return "login_timeout"
    if "unknown server" in t or "server not found" in t or ("server" in t and "not found" in t):
        return "server_not_found"
    # BROKER server reached and reported unavailable — REQUIRE broker-server-specific evidence (the server is
    # named AND an availability failure), NOT a bare "connect"/"network" token (which more often reflects a
    # local/host problem and must never be blamed on the broker). ``server_unavailable`` is preserved ONLY here.
    if "server" in t and ("unavailable" in t or "not responding" in t or "is busy" in t
                          or "no connection to" in t or "connection to trade server" in t):
        return "server_unavailable"
    if c == -6:                      # RES_E_AUTH_FAILED with no clearer text — an auth-layer rejection
        return "invalid_password"
    # (-10005 RES_E_INTERNAL_FAIL_TIMEOUT is handled at the TOP as a local IPC-family failure, not here — it
    #  is an internal IPC timeout, never a broker/login timeout; Phase-4 WS-C.)
    # A bare "connection"/"network" token with no IPC marker and no broker-server evidence is ambiguous — stay
    # conservative (retryable, blames neither the broker nor the customer) rather than assert a broker outage.
    return "could_not_verify"


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RealMt5Probe:
    """Production MT5 probe. Lazily imports ``MetaTrader5`` (Windows/terminal-only) and exposes ONLY the four
    calls the login check needs — initialize, last_error, account_info, shutdown. It deliberately provides NO
    order / symbol / position / history method, so no downstream code can trade through it."""

    def __init__(self):
        self._mt5 = None

    def initialize(self, *, path, login, password, server, timeout_ms) -> bool:
        import MetaTrader5 as mt5           # noqa: PLC0415 — host-only import, kept off the module top
        self._mt5 = mt5
        return bool(mt5.initialize(path=path, login=int(login), password=password, server=server,
                                   timeout=int(timeout_ms), portable=True))

    def last_error(self):
        return self._mt5.last_error() if self._mt5 else (None, "")

    def account_info(self):
        return self._mt5.account_info() if self._mt5 else None

    def terminal_info(self):
        """Terminal state (build, connected flag, path). Read-only; ADR-0027 observability. None if absent."""
        try:
            return self._mt5.terminal_info() if self._mt5 else None
        except Exception:            # noqa: BLE001 — an observability read must never fault the probe
            return None

    def version(self):
        """MT5 Python package/terminal version tuple. None if unavailable."""
        try:
            return self._mt5.version() if self._mt5 else None
        except Exception:            # noqa: BLE001
            return None

    def shutdown(self) -> None:
        if self._mt5:
            self._mt5.shutdown()


class LoginValidationHandler:
    """Orchestrates one VALIDATE_LOGIN probe. All host-touching parts are injected so the whole flow is
    unit-testable without Windows/MT5/crypto keys:
      * ``open_envelope(sealed, aad) -> bytes`` opens the sealed password bound to the request AAD;
      * ``bind_aad(**ctx) -> bytes`` recomputes the AAD from the verified request context;
      * ``mt5_probe_factory() -> probe`` yields a fresh single-use probe;
      * ``path_exists(path) -> bool`` checks the validation executable exists;
      * ``lock`` is the PROCESS-global single-flight lock (one probe at a time WITHIN the agent process). The
        agent is deployed as a single process, so this is one probe per box; if the agent is ever run
        multi-process, this must become a named OS mutex / file lock (a concurrent probe would collide on the
        single portable validation data dir).
    ``validate`` NEVER raises: every path returns ``{ok, reason_code, is_demo}``."""

    def __init__(self, *, open_envelope, bind_aad, mt5_probe_factory, path_exists,
                 validation_dir, validation_root=DEFAULT_VALIDATION_ROOT,
                 forbidden_roots=DEFAULT_FORBIDDEN_ROOTS, lock=None, login_timeout_ms=30000,
                 persist_isolation_diagnostic=None, clock=None, prepare_terminal=None):
        self._open_envelope = open_envelope
        self._bind_aad = bind_aad
        self._probe_factory = mt5_probe_factory
        self._path_exists = path_exists
        self._validation_dir = validation_dir
        self._validation_root = validation_root
        self._forbidden_roots = tuple(forbidden_roots)
        self._lock = lock or threading.Lock()
        self._login_timeout_ms = int(login_timeout_ms)
        # OPTIONAL in-process isolation-diagnostic persistence (2026-08-06 in-process-capture packet). Set ONLY
        # when this handler runs on the in-process (agent) path — the task-launched runner persists its OWN
        # artefact via ``validation_runner.run_once``, so it leaves this None to avoid a double write. The
        # callable ``persist(correlation_id, isolation_report, *, started, finished)`` is fail-open: it MUST NOT
        # change the fail-closed decision and any fault degrades to ``diagnostic_capture_failed`` (never raises
        # out of ``validate``).
        self._persist_isolation_diagnostic = persist_isolation_diagnostic
        self._clock = clock or time.time
        # OPTIONAL in-process terminal PREPARATION (2026-08-07 materialisation packet). A zero-arg callable that
        # deterministically materialises/restores the isolated validation terminal from the certified precompiled
        # baseline and returns a fixed status label. Set ONLY on the in-process (agent) path — the task-launched
        # runner restores its OWN baseline post-probe, so it leaves this None. It runs UNDER the single-flight
        # lock, BEFORE the isolation gate, and NEVER raises / NEVER weakens the gate.
        self._prepare_terminal = prepare_terminal

    def _denied(self, reason_code, *, isolation=None):
        out = {"ok": False, "reason_code": reason_code, "is_demo": None}
        if isolation is not None:
            # OPERATOR-ONLY structured isolation diagnostic. Carried under a DISTINCT ``_isolation`` key (never
            # ``_operator``, so the runner's stage derivation is unaffected) and stripped before the customer
            # result — the customer contract is ``{ok, reason_code, is_demo}`` only.
            out["_isolation"] = isolation
        return out

    def validate(self, *, operation, runtime_uuid, correlation_id, nonce, payload) -> dict:
        started = self._clock()
        # Single-flight FIRST: PREPARE (materialise) + the isolation read + the probe form ONE atomic critical
        # section over the shared validation-terminal dir, so a concurrent validation can neither materialise the
        # terminal underneath this one nor probe it. A busy validator returns an honest ``validation_busy`` and
        # never touches the terminal. (Moved ahead of the isolation read from its former probe-only position so
        # the new preparation step is covered by the same lock — WS-D concurrency safety.)
        if not self._lock.acquire(blocking=False):
            return self._denied("validation_busy")
        try:
            # 0) PREPARE: deterministically materialise/restore the isolated validation terminal from the
            #    certified precompiled baseline BEFORE the isolation gate (the proven in-process blocker was
            #    ``validation_terminal_missing``). In-process path only; NEVER raises, NEVER weakens the gate. A
            #    non-``restored`` label leaves the terminal as-is and the authoritative gate reports the rule.
            prepare_result = self._prepare_terminal() if self._prepare_terminal is not None else None

            # 1) isolated-terminal contract — never attempt a probe against an unproven path.
            try:
                exe_path = assert_isolated_validation_terminal(
                    self._validation_dir, validation_root=self._validation_root,
                    forbidden_roots=self._forbidden_roots, path_exists=self._path_exists)
            except IsolationError:
                # Fail-closed decision unchanged; ALSO build a secret-safe structured diagnostic so the on-host
                # artefact localises WHICH rule/path failed AND whether preparation ran (customer reason stays
                # ``isolation_check_failed``). FAIL-OPEN persistence — a fault degrades to a secret-safe
                # ``diagnostic_capture_failed`` log line and never alters the result. Runner leaves this None.
                report = isolation_report(
                    self._validation_dir, validation_root=self._validation_root,
                    forbidden_roots=self._forbidden_roots, path_exists=self._path_exists)
                if self._persist_isolation_diagnostic is not None:
                    try:
                        self._persist_isolation_diagnostic(
                            correlation_id, report, started=started, finished=self._clock(),
                            prepare_result=prepare_result)
                    except Exception:            # noqa: BLE001 — capture must never break the probe or leak detail
                        _log.warning("diagnostic_capture_failed correlation=%s component=in_process_isolation "
                                     "stage=ISOLATION error_class=write_failed", _safe_corr(correlation_id))
                return self._denied(ISOLATION_CHECK_FAILED, isolation=report)

            # 2) login / server come from the payload, which the outer signature already bound via payload_digest.
            login = str((payload or {}).get("login") or "").strip()
            server = str((payload or {}).get("server") or "").strip()
            sealed = (payload or {}).get("password_env")
            if not login:
                return self._denied("invalid_login")
            if not server:
                return self._denied("broker_server_missing")
            if not isinstance(sealed, dict):
                return self._denied("credential_missing")

            # 3) open the envelope under THIS request's AAD (op/runtime/correlation/nonce). Any tamper/rebind/
            #    wrong-key/missing-key fails closed here, before any terminal is touched.
            try:
                aad = self._bind_aad(operation=operation, runtime_uuid=runtime_uuid,
                                     correlation_id=correlation_id, nonce=nonce)
                pw_bytes = self._open_envelope(sealed, aad)
                password = pw_bytes.decode("utf-8")
            except Exception:            # noqa: BLE001 — EnvelopeError et al.; never leak detail
                return self._denied("credential_unsealable")

            # 4) probe — the single-flight lock is ALREADY held (acquired above), so it covers the full
            #    prepare → isolate → probe critical section.
            try:
                return self._probe(exe_path, login, server, password)
            finally:
                password = None          # drop the plaintext reference asap
                del password
        finally:
            self._lock.release()

    def _probe(self, exe_path, login, server, password) -> dict:
        # ADR-0027 observability: an ``_operator`` diagnostic rides ALONGSIDE the customer-safe result. It
        # carries only allow-listed, non-secret fields (never a password/ciphertext/host path); the RUNNER
        # scrubs every value and adds journal milestones + cleanup results before anything is persisted. The
        # customer path (mgmt_agent_core / TaskLaunchLoginValidator) reads ONLY {ok, reason_code, is_demo} and
        # never forwards ``_operator``.
        op = {"initialize_started": False, "initialize_result": None, "last_error_code": None,
              "last_error_text": "", "terminal_info_present": False, "account_info_present": False,
              "trade_mode": None, "is_demo": None, "mt5_package_version": None, "terminal_build": None}

        def _out(result):
            result["_operator"] = op
            return result

        try:
            login_id = int(login)
        except (TypeError, ValueError):
            return _out(self._denied("invalid_login"))
        try:
            probe = self._probe_factory()        # bound BEFORE the finally; a factory fault → no terminal
        except Exception:                        # noqa: BLE001 — nothing was launched; nothing to shut down
            return _out(self._denied("could_not_verify"))
        try:
            op["initialize_started"] = True
            ok = probe.initialize(path=exe_path, login=login_id, password=password, server=server,
                                  timeout_ms=self._login_timeout_ms)
            op["initialize_result"] = bool(ok)
            ver = probe.version() if hasattr(probe, "version") else None
            if ver is not None:
                op["mt5_package_version"] = str(ver)[:64]
            tinfo = probe.terminal_info() if hasattr(probe, "terminal_info") else None
            op["terminal_info_present"] = tinfo is not None
            if tinfo is not None:
                op["terminal_build"] = _as_int(getattr(tinfo, "build", None))
            if not ok:
                code, text = probe.last_error()
                op["last_error_code"] = _as_int(code)
                op["last_error_text"] = (str(text)[:200] if text else "")
                return _out(self._denied(classify_init_error(code, text)))
            acc = probe.account_info()
            op["account_info_present"] = acc is not None
            if acc is None:
                return _out(self._denied("could_not_verify"))
            trade_mode = getattr(acc, "trade_mode", None)
            op["trade_mode"] = _as_int(trade_mode)
            if trade_mode is None:
                return _out(self._denied("could_not_verify"))
            # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL. is_demo is True ONLY for a genuine demo account; a
            # contest or real account is a connected, correctly-classified session (not a failure) but is
            # NOT demo — the platform treats it as live-detected and never auto-trades it here.
            is_demo = (trade_mode == 0)
            op["is_demo"] = is_demo
            return _out({"ok": True, "reason_code": "demo_ok" if is_demo else "live_detected",
                         "is_demo": is_demo})
        except Exception:            # noqa: BLE001 — a probe-layer fault is retryable, never a raw leak
            return _out(self._denied("could_not_verify"))
        finally:
            # ALWAYS shut the terminal down — on success AND on every failure/exception path.
            try:
                probe.shutdown()
            except Exception:        # noqa: BLE001 — shutdown must not mask the outcome or leak
                pass


def _build_isolation_persister(cfg: dict, agent_meta):
    """Build the in-process isolation-diagnostic persister closure (or ``None`` when no diagnostics directory is
    configured). Captures the config-source PROVENANCE at construction time (not reconstructed after the request)
    and delegates persistence to the SHARED ``validation_diagnostics.write_isolation_diagnostic`` so the
    in-process and runner artefacts use one schema. The closure raises on I/O failure; ``validate`` catches it
    and degrades to ``diagnostic_capture_failed`` (fail-open)."""
    import validation_diagnostics as diag                       # noqa: PLC0415 — host-side import (pure stdlib)
    diag_dir = cfg.get("validation_diagnostics_dir")
    if not diag_dir:
        return None
    meta = agent_meta or {}
    cfg_source = diag.config_source(cfg, env=os.environ)         # provenance captured at construction (WS-D)
    service_identity = os.environ.get("BETA_AGENT_SERVICE_IDENTITY") or meta.get("service_identity")
    supervised = meta.get("supervised")
    manifest_version = meta.get("manifest_version")

    def _persist(correlation_id, isolation_report, *, started, finished, prepare_result=None):
        diag.write_isolation_diagnostic(
            diag_dir, correlation_id=correlation_id, reason_code=ISOLATION_CHECK_FAILED,
            isolation=isolation_report, config_source=cfg_source, execution_mode="in_process",
            process_meta={"process_id": os.getpid(), "process_session_id": None,
                          "executable": sys.executable, "service_identity": service_identity,
                          "supervised": supervised, "manifest_version": manifest_version},
            stage_reached="AGENT_RECEIVED", first_failing_stage="ISOLATION",
            prepare_result=prepare_result,               # secret-safe label: did materialisation run/succeed?
            started=started, finished=finished, now=finished)

    return _persist


def _build_terminal_preparer(cfg: dict, forbidden):
    """Build the in-process terminal-preparation closure (or ``None`` when no precompiled source is configured).
    REUSES the runner's proven mirror primitive (``validation_runner.mirror_validation_baseline`` → ``_mirror_os``)
    so there is ONE materialisation mechanism, not a parallel copy design. The closure takes no arguments, never
    raises, and returns a fixed status label."""
    precompiled_dir = cfg.get("validation_precompiled_dir")
    if not precompiled_dir:
        return None
    validation_dir = cfg.get("validation_terminal_dir")
    validation_root = cfg.get("validation_root") or DEFAULT_VALIDATION_ROOT

    def _prepare():
        import validation_runner                                # noqa: PLC0415 — host-side; reuse the mirror
        return prepare_validation_terminal(
            validation_dir, validation_root=validation_root, forbidden_roots=forbidden,
            precompiled_dir=precompiled_dir, mirror=validation_runner.mirror_validation_baseline)

    return _prepare


def build_inprocess_handler(cfg: dict, *, agent_meta=None, enable_diagnostics=False, prepare_terminal=False):
    """Assemble the ``LoginValidationHandler`` (envelope-open + isolated-terminal + MT5 probe). Used BOTH by the
    task-launched runner (GUI-capable window station) AND, when ``validation_task_name`` is unset, DIRECTLY by
    the agent as the in-process validator. Returns ``None`` when the validation terminal or the envelope private
    key is not configured (fail closed at build). Kept here (not in agent.py) so both callers reuse the exact,
    integrity-pinned probe assembly.

    ``enable_diagnostics`` wires the in-process isolation-diagnostic persister and ``prepare_terminal`` wires the
    in-process terminal-materialisation step. Both MUST be set ONLY by the agent in-process path — the runner
    leaves them False (it persists its OWN artefact and restores its OWN baseline post-probe via
    ``validation_runner.run_once``), so there is no double write and no change to runner behaviour."""
    validation_dir = cfg.get("validation_terminal_dir")
    if not validation_dir:
        return None
    import broker_cred_envelope as cred_env                     # noqa: PLC0415 — host-only optional import
    if not cred_env.agent_enc_configured():
        return None
    forbidden = tuple(dict.fromkeys(
        DEFAULT_FORBIDDEN_ROOTS
        + (cfg.get("slots_root", ""), cfg.get("golden_dir", ""), cfg.get("beta_root", ""))
        + tuple(cfg.get("validation_forbidden_roots", ()))))
    forbidden = tuple(r for r in forbidden if r)
    persist = _build_isolation_persister(cfg, agent_meta) if enable_diagnostics else None
    prepare = _build_terminal_preparer(cfg, forbidden) if prepare_terminal else None
    return LoginValidationHandler(
        open_envelope=lambda sealed, aad: cred_env.open_envelope(sealed, aad=aad),
        bind_aad=cred_env.bind_aad,
        mt5_probe_factory=RealMt5Probe,
        path_exists=os.path.isfile,
        validation_dir=validation_dir,
        validation_root=cfg.get("validation_root") or DEFAULT_VALIDATION_ROOT,
        forbidden_roots=forbidden,
        login_timeout_ms=int(cfg.get("login_timeout_ms", 30000)),
        persist_isolation_diagnostic=persist,
        prepare_terminal=prepare)


class TaskLaunchLoginValidator:
    """ADR-0027 task-launch remediation. Exposes the SAME ``validate(...) -> {ok, reason_code, is_demo}``
    interface the agent core expects, but instead of running the MT5 probe IN-PROCESS (a WinSW-service
    window station where MT5 GUI creation fails — root-caused 2026-08-02), it hands the SEALED request to a
    pre-approved, task-launched runner (a GUI-capable window station) and returns the runner's secret-safe
    outcome.

    The delegator NEVER decrypts the credential and NEVER passes a secret via the task command line/args/env
    — only an ACL-restricted, HMAC-authenticated, single-use handoff FILE carries the (already-sealed)
    payload. One delegation at a time (process lock); a busy delegator returns ``validation_busy``; a task
    that will not launch → ``validation_runner_unavailable``; no result in time → ``validation_runner_timeout``.
    The runner opens the envelope with the machine-scoped private key exactly as the in-process handler did,
    so the credential's plaintext lifetime is unchanged (point-of-use only, in the runner)."""

    def __init__(self, *, handoff_dir, task_name, trigger_task, timeout_ms,
                 lock=None, gen_request_id=None, clock=None, sleep=None, result_grace_s=15):
        self._dir = handoff_dir
        self._task = task_name
        self._trigger = trigger_task
        self._timeout_ms = int(timeout_ms)
        self._lock = lock or threading.Lock()
        self._gen = gen_request_id or handoff.new_request_id
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._grace = float(result_grace_s)

    def _denied(self, reason_code):
        return {"ok": False, "reason_code": reason_code, "is_demo": None}

    def validate(self, *, operation, runtime_uuid, correlation_id, nonce, payload) -> dict:
        if not self._task or not self._dir:
            return self._denied("validation_unconfigured")
        # one delegation at a time (mirrors the in-process single-flight; the task is also single-instance)
        if not self._lock.acquire(blocking=False):
            return self._denied("validation_busy")
        rid = self._gen()
        try:
            try:
                handoff.sweep_stale(self._dir, max_age_s=max(120.0, self._timeout_ms / 1000.0 + 60),
                                    now=self._clock())
            except Exception:            # noqa: BLE001 — housekeeping must never fail the probe
                pass
            body = {"operation": operation, "runtime_uuid": str(runtime_uuid),
                    "correlation_id": correlation_id, "nonce": nonce, "payload": payload}
            try:
                handoff.write_request(self._dir, rid, body,
                                      ttl_seconds=int(self._timeout_ms / 1000) + 30, now=self._clock())
            except Exception:            # noqa: BLE001 — cannot stage the request → runner is unavailable
                return self._denied("validation_runner_unavailable")
            try:
                triggered = bool(self._trigger(self._task))
            except Exception:            # noqa: BLE001
                triggered = False
            if not triggered:
                return self._denied("validation_runner_unavailable")
            res = handoff.read_result(self._dir, rid, timeout_s=self._timeout_ms / 1000.0 + self._grace,
                                      sleep=self._sleep, clock=self._clock)
            if res is None:
                return self._denied("validation_runner_timeout")
            return {"ok": bool(res.get("ok")),
                    "reason_code": str(res.get("reason_code") or "could_not_verify"),
                    "is_demo": res.get("is_demo")}
        finally:
            try:
                handoff.cleanup(self._dir, rid)      # remove req/claim/result regardless of outcome
            except Exception:            # noqa: BLE001
                pass
            self._lock.release()
