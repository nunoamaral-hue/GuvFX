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

import threading

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
        try:
            login_id = int(login)
        except (TypeError, ValueError):
            return self._denied("invalid_login")
        try:
            probe = self._probe_factory()        # bound BEFORE the finally; a factory fault → no terminal
        except Exception:                        # noqa: BLE001 — nothing was launched; nothing to shut down
            return self._denied("could_not_verify")
        try:
            ok = probe.initialize(path=exe_path, login=login_id, password=password, server=server,
                                  timeout_ms=self._login_timeout_ms)
            if not ok:
                code, text = probe.last_error()
                return self._denied(classify_init_error(code, text))
            acc = probe.account_info()
            if acc is None:
                return self._denied("could_not_verify")
            trade_mode = getattr(acc, "trade_mode", None)
            if trade_mode is None:
                return self._denied("could_not_verify")
            # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL. is_demo is True ONLY for a genuine demo account; a
            # contest or real account is a connected, correctly-classified session (not a failure) but is
            # NOT demo — the platform treats it as live-detected and never auto-trades it here.
            is_demo = (trade_mode == 0)
            return {"ok": True, "reason_code": "demo_ok" if is_demo else "live_detected",
                    "is_demo": is_demo}
        except Exception:            # noqa: BLE001 — a probe-layer fault is retryable, never a raw leak
            return self._denied("could_not_verify")
        finally:
            # ALWAYS shut the terminal down — on success AND on every failure/exception path.
            try:
                probe.shutdown()
            except Exception:        # noqa: BLE001 — shutdown must not mask the outcome or leak
                pass
