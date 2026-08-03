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

import os
import threading
import time

import validation_handoff as handoff

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


def assert_isolated_validation_terminal(validation_dir, *, validation_root=DEFAULT_VALIDATION_ROOT,
                                        forbidden_roots=DEFAULT_FORBIDDEN_ROOTS,
                                        exe_name=VALIDATION_EXE_NAME, path_exists) -> str:
    """Prove the validation terminal is the dedicated, isolated one and return its executable path. Every
    failure raises ``IsolationError`` (fail-closed) — a probe is NEVER attempted against an unproven path."""
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
    # containment already proven on the normalised form; build the launch path in original case
    exe_path = validation_dir.replace("/", "\\").rstrip("\\") + "\\" + exe_name
    if not path_exists(exe_path):
        raise IsolationError("validation_terminal_missing")
    return exe_path


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
    if "no connection" in t or "no ipc" in t or "connect" in t or "network" in t or "connection" in t:
        return "server_unavailable"
    if c == -6:                      # RES_E_AUTH_FAILED with no clearer text — an auth-layer rejection
        return "invalid_password"
    if c == -10005:                  # RES_E_INTERNAL_FAIL_TIMEOUT
        return "login_timeout"
    if c == -10004:                  # RES_E_INTERNAL_FAIL_CONNECT
        return "server_unavailable"
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
                 forbidden_roots=DEFAULT_FORBIDDEN_ROOTS, lock=None, login_timeout_ms=30000):
        self._open_envelope = open_envelope
        self._bind_aad = bind_aad
        self._probe_factory = mt5_probe_factory
        self._path_exists = path_exists
        self._validation_dir = validation_dir
        self._validation_root = validation_root
        self._forbidden_roots = tuple(forbidden_roots)
        self._lock = lock or threading.Lock()
        self._login_timeout_ms = int(login_timeout_ms)

    def _denied(self, reason_code):
        return {"ok": False, "reason_code": reason_code, "is_demo": None}

    def validate(self, *, operation, runtime_uuid, correlation_id, nonce, payload) -> dict:
        # 1) isolated-terminal contract FIRST — never attempt a probe against an unproven path.
        try:
            exe_path = assert_isolated_validation_terminal(
                self._validation_dir, validation_root=self._validation_root,
                forbidden_roots=self._forbidden_roots, path_exists=self._path_exists)
        except IsolationError:
            return self._denied("isolation_check_failed")

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

        # 4) single-flight: one probe at a time within this agent process (the terminal dir is a single-
        #    instance resource; the agent is deployed single-process — see the class docstring).
        if not self._lock.acquire(blocking=False):
            del password
            return self._denied("validation_busy")
        try:
            return self._probe(exe_path, login, server, password)
        finally:
            password = None          # drop the plaintext reference asap
            del password
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


def build_inprocess_handler(cfg: dict):
    """Assemble the in-process ``LoginValidationHandler`` (envelope-open + isolated-terminal + MT5 probe).
    Used BY THE TASK-LAUNCHED RUNNER, which executes in a GUI-capable window station. Returns ``None`` when
    the validation terminal or the envelope private key is not configured (fail closed at build). Kept here
    (not in agent.py) so the runner reuses the exact, integrity-pinned probe assembly."""
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
    return LoginValidationHandler(
        open_envelope=lambda sealed, aad: cred_env.open_envelope(sealed, aad=aad),
        bind_aad=cred_env.bind_aad,
        mt5_probe_factory=RealMt5Probe,
        path_exists=os.path.isfile,
        validation_dir=validation_dir,
        validation_root=cfg.get("validation_root") or DEFAULT_VALIDATION_ROOT,
        forbidden_roots=forbidden,
        login_timeout_ms=int(cfg.get("login_timeout_ms", 30000)))


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
