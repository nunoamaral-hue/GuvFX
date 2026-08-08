#!/usr/bin/env python3
"""
GuvFX MT5 Signal Execution Bridge

A safety-first execution bridge that handles PLACE_ORDER jobs from strategy signals.
This is separate from mt5_demo_bridge.py which handles PLACE_TEST_ORDER (demo trades).

KEY DIFFERENCES FROM DEMO BRIDGE:
- Handles PLACE_ORDER job type (not PLACE_TEST_ORDER)
- Supports SL/TP from payload
- Supports both BUY and SELL
- Uses risk-calculated lot size from payload (capped at 0.02)
- Does NOT auto-close positions (real strategy trades)
- Supports EURUSD and GBPUSD

HTTP SERVER MODE (for OHLC data and demo order execution):
- Runs an embedded HTTP server on port 8788
- Provides /mt5/snapshots/rates endpoint for fetching OHLC data
- Provides POST /mt5/order endpoint for demo order execution (called by Linux ingest worker)
- Used by the backend for H4 auto-evaluation and controlled demo execution

SAFETY RAILS (hard-coded, cannot be bypassed):
- Demo accounts only (is_demo=True in payload)
- MT5-native symbol validation: the exact broker symbol resolved by the backend
  registry must exist and be selectable on the terminal (no static allowlist)
- Max 0.02 lot (hard cap)
- SL/TP required for all orders

REQUIREMENTS:
- Python 3.8+
- MetaTrader5 package: pip install MetaTrader5
- requests package: pip install requests
- MT5 terminal running with Algo Trading enabled

ENVIRONMENT VARIABLES (required):
- GUVFX_API_URL: API base URL (e.g., https://api.guvfx.com)
- GUVFX_WORKER_TOKEN: Worker authentication token (matches MT5_WORKER_TOKEN on server)
- MT5_ACCOUNT_ID: TradingAccount ID to poll jobs for

OPTIONAL:
- MT5_TERMINAL_PATH: Path to MT5 terminal (if non-standard location)
- POLL_INTERVAL_SECONDS: Polling interval (default: 2)
- HTTP_SERVER_PORT: Port for HTTP server (default: 8788)
- GUVFX_AGENT_TOKEN: Token for OHLC endpoint auth (separate from WORKER_TOKEN)

USAGE:
    python mt5_signal_bridge.py
"""

import os
import sys
import time
import hmac
import json
import logging
import random
import threading
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mt5_signal_bridge.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# =============================================================================
# HARD-CODED SAFETY RAILS (DO NOT MODIFY)
# =============================================================================
# Symbol validation is MT5-native (see validate_broker_symbol): the EXACT broker
# symbol resolved by the backend registry (payload["symbol"]) must exist on the
# running terminal and be selectable — fail-closed otherwise. There is deliberately
# NO static symbol allowlist here; symbol availability is broker/account-aware and
# is owned by the backend registry (execution.broker_symbols). The lot-cap, side,
# SL/TP, demo-only and order_check rails below are unchanged.
MAX_LOT_SIZE = 0.02
ALLOWED_SIDES = ["BUY", "SELL"]

# Demo order endpoint safety rails (POST /mt5/order)
DEMO_ORDER_MAX_LOT_SIZE = 0.02  # conservative fail-closed DEFAULT (an order with no source cap)
DEMO_ORDER_ALLOWED_SIDES = ["BUY", "SELL"]

# Hard technical upper bound — no order may exceed this regardless of source. The actual permitted
# size is SOURCE-SCOPED: the promotion payload carries ``max_lot`` (the leg's per-source cap); the
# bridge has no DB, so it trusts that internal value up to this bound and fail-closes to the
# conservative default when it is absent/invalid. This raises the ceiling ONLY as an upper bound;
# it never lifts a source's own cap.
BRIDGE_HARD_MAX_LOT = 1.0

# Independent per-SOURCE ceiling at the outermost bridge gate — the payload's max_lot may go LOWER
# but never above the source's authorised size, even if the payload is malformed/forged. Unknown /
# missing source → the conservative default. A CEILING only (fail-safe: clamps down, never up).
BRIDGE_SOURCE_HARD_CAP = {"ti_signals": float(os.getenv("TI_SOURCE_MAX_LOT", "0.40"))}


def _effective_max_lot(container) -> float:
    """The per-order lot ceiling: the source-scoped ``max_lot`` from the (internal) promotion
    payload, bounded by BOTH the source's independent hard ceiling AND ``BRIDGE_HARD_MAX_LOT``,
    fail-closed to ``DEMO_ORDER_MAX_LOT_SIZE`` for an unknown/missing source."""
    c = container or {}
    try:
        cap = float(c.get("max_lot") or DEMO_ORDER_MAX_LOT_SIZE)
    except (TypeError, ValueError):
        cap = DEMO_ORDER_MAX_LOT_SIZE
    src_ceiling = BRIDGE_SOURCE_HARD_CAP.get(str(c.get("signal_source") or ""), DEMO_ORDER_MAX_LOT_SIZE)
    return min(max(cap, 0.0), src_ceiling, BRIDGE_HARD_MAX_LOT)

# Fail-closed reasons returned when the terminal cannot trade the requested symbol.
SYMBOL_NOT_AVAILABLE_ON_MT5 = "SYMBOL_NOT_AVAILABLE_ON_MT5"
SYMBOL_NOT_SELECTABLE_ON_MT5 = "SYMBOL_NOT_SELECTABLE_ON_MT5"


def validate_broker_symbol(mt5, symbol: str):
    """MT5-native validation of an EXACT broker symbol — replaces the old static allowlist.

    The backend symbol registry (execution.broker_symbols) has already resolved the
    provider symbol to this broker symbol; the bridge's only job is to confirm the
    running MT5 terminal actually offers it and can select it, then hand back the
    ``symbol_info`` the existing order logic already relied on. Fail-closed on anything
    else. This places / checks NO order — it is pure symbol validation.

    Returns ``(ok, symbol_info_or_None, error_or_None)`` where ``error`` is one of
    ``SYMBOL_NOT_AVAILABLE_ON_MT5`` (terminal has no such symbol) or
    ``SYMBOL_NOT_SELECTABLE_ON_MT5`` (present but cannot be made visible/selected).

    Behaviour is byte-identical to the previous inline symbol_info/symbol_select block:
    on a non-visible symbol it attempts ``symbol_select`` and, on success, proceeds with
    the original ``symbol_info`` object (its point/digits are unaffected by selection).
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, None, SYMBOL_NOT_AVAILABLE_ON_MT5
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return False, None, SYMBOL_NOT_SELECTABLE_ON_MT5
    return True, info, None


# =============================================================================
# Polling/Retry Configuration
# =============================================================================
MAX_FETCH_RETRIES = 3
RETRY_BASE_DELAY = 2.0
MAX_CONSECUTIVE_404 = 3
RETRY_DELAY_SECONDS = 5
HTTP_TIMEOUT = 15

# Post-trade delay before completing job (sync race mitigation)
POST_TRADE_SYNC_DELAY = float(os.getenv("GUVFX_POST_TRADE_SYNC_DELAY_SECONDS", "3"))

# Extra buffer points added on top of broker's trade_stops_level / trade_freeze_level
# to avoid edge-case rejections.  Default 2 points ≈ 0.2 pip for 5-digit brokers.
EXTRA_STOP_BUFFER_POINTS = int(os.getenv("GUVFX_EXTRA_STOP_BUFFER_POINTS", "2"))

# Max attempts to widen SL/TP buffer via order_check before giving up
STOP_CLAMP_MAX_RETRIES = 3

# Force-once test job: dynamic SL/TP from live tick price (pips from market)
FORCE_ONCE_SL_PIPS = float(os.getenv("FORCE_ONCE_SL_PIPS", "50"))
FORCE_ONCE_TP_PIPS = float(os.getenv("FORCE_ONCE_TP_PIPS", "100"))

# =============================================================================
# Configuration
# =============================================================================
API_URL = os.getenv("GUVFX_API_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("GUVFX_WORKER_TOKEN", "").strip()   # bridge -> backend job polling (X-Worker-Token)
AGENT_TOKEN = os.getenv("GUVFX_AGENT_TOKEN", "").strip()     # inbound HTTP endpoint auth
# The ONE credential every protected HTTP route is checked against.
#
# WS1 (post-rotation hardening): this used to be ``AGENT_TOKEN or WORKER_TOKEN`` — a fallback between two
# UNRELATED credentials. That is exactly the coupling the 2026-07-22 rotation exposed elsewhere, so the
# fallback is removed: inbound auth uses the agent token and nothing else. Whitespace-only normalises to ""
# above, so missing/empty is treated as NOT CONFIGURED and the bridge fails closed.
HTTP_AUTH_TOKEN = AGENT_TOKEN
ACCOUNT_ID = os.getenv("MT5_ACCOUNT_ID", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
HTTP_SERVER_PORT = int(os.getenv("HTTP_SERVER_PORT", "8788"))
# Close-path robustness (Phase 2, Control 2): a close is risk-reducing, so retry a BOUNDED number of
# times with a fresh price on retryable retcodes before declaring the position still open.
CLOSE_MAX_ATTEMPTS = max(1, int(os.getenv("CLOSE_MAX_ATTEMPTS", "3") or 3))   # never 0 (would skip closing)
CLOSE_RETRYABLE_RC = {10004, 10020, 10021}   # requote / price-changed / price-off (re-read + retry)


def _filling_for(symbol):
    """Order filling the SYMBOL/broker supports (IOC preferred, else FOK).
    IS6 only allows FOK on metals; TradersWay allows IOC. Per-symbol keeps both working."""
    import MetaTrader5 as mt5
    try:
        si = mt5.symbol_info(symbol)
        if si is not None:
            if si.filling_mode & 2:
                return mt5.ORDER_FILLING_IOC
            if si.filling_mode & 1:
                return mt5.ORDER_FILLING_FOK
    except Exception:
        pass
    return mt5.ORDER_FILLING_IOC


# --- Execution binding verification (Phase 2, Control 1: exact runtime binding) --------------------
# Before any OPENING order_send (execute_mt5_trade / execute_demo_order), the bridge independently
# verifies from BROKER TRUTH (not a payload flag) that the connected terminal is the intended account
# and classification. (close_position / modify_position act on an existing position by ticket and keep
# their own demo-only check; routing them through this gate is a tracked follow-up.) This ports the certified
# "exact binding" gate into the production order paths — previously the poller path (execute_mt5_trade)
# verified nothing and trusted the payload is_demo flag alone. Fail closed: if the binding cannot be
# positively verified, refuse to trade. Optional identity pins (MT5_EXPECTED_LOGIN / MT5_EXPECTED_SERVER)
# and MT5_ALLOW_LIVE are read fresh per call so operators/tests can set them without a restart.
def _bridge_allow_live() -> bool:
    return os.getenv("MT5_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def _require_identity_pin() -> bool:
    """ADR-0033 Increment 2 (review MEDIUM). When set — on a persistent-workspace bridge — EVERY order
    must carry a per-job identity pin and the process-env pin fallback is REFUSED. This makes identity
    binding a property of the DEPLOYMENT (the terminal), not merely a self-declared payload flag, so a
    shared multi-account persistent bridge cannot execute a job that omits its (login, server). Legacy
    bridges leave it unset ⇒ exact prior behaviour."""
    return os.getenv("MT5_REQUIRE_IDENTITY_PIN", "").strip().lower() in ("1", "true", "yes", "on")


# --- ADR-0034 / WS3 — Guarded Attach primitive (M1, DARK by default) --------------------------------
# Experiment H proved mt5.initialize(path=) is DUAL-MODE: it ATTACHES to an already-running terminal, but
# if the terminal is DOWN it LAUNCHES it and the launched terminal AUTO-LOGS-IN from cached config\accounts.dat.
# On the persistent, never-own-credentials workspace that auto-login would silently replay the customer's
# saved credentials. The guarded attach makes the never-launch invariant explicit: the target terminal MUST
# already be running (probed BEFORE initialize, so initialize can only ATTACH) and MUST be broker-connected
# with an account identity after attach — else fail closed. It NEVER calls mt5.login() and NEVER relaunches.
# DARK by default: when MT5_GUARDED_ATTACH is unset, guarded_initialize() is byte-identical to
# mt5.initialize(**init_kwargs) — the legacy/production bridge is unchanged.

def _guarded_attach_enabled() -> bool:
    """ADR-0034 WS3. When set — on a persistent-workspace bridge — the attach primitive enforces the
    never-launch invariant (target already running + connected + identity, or fail closed). Legacy/
    production bridges leave it unset ⇒ exact prior mt5.initialize() behaviour (may launch)."""
    return os.getenv("MT5_GUARDED_ATTACH", "").strip().lower() in ("1", "true", "yes", "on")


def evaluate_hosted_startup_config(env) -> list:
    """Pure, fail-closed G6 assertion: given an env mapping, return the list of hosted-execution config
    errors (empty ⇒ safe). A HOSTED WORKSPACE execution bridge must NEVER silently downgrade to legacy,
    shared/NULL-node, credential-login, unguarded, or un-pinned execution. Testable without MT5/process I/O.

    Required when ``MT5_HOSTED_EXECUTION`` is truthy: guarded attach ON, per-job identity pin required ON,
    live/real execution OFF (Phase-1 demo-only), and NO broker credential-login env configured for attach
    (login/password/server), because a hosted attach is path-only + never calls ``mt5.login()``.
    """
    def _truthy(name):
        return str(env.get(name, "")).strip().lower() in ("1", "true", "yes", "on")

    if not _truthy("MT5_HOSTED_EXECUTION"):
        return []  # not a hosted bridge — legacy behaviour, no assertions
    errors = []
    if not _truthy("MT5_GUARDED_ATTACH"):
        errors.append("MT5_HOSTED_EXECUTION requires MT5_GUARDED_ATTACH=1 (never-launch attach); refusing "
                      "to downgrade to an unguarded initialize that could launch/auto-login the terminal")
    if not _truthy("MT5_REQUIRE_IDENTITY_PIN"):
        errors.append("MT5_HOSTED_EXECUTION requires MT5_REQUIRE_IDENTITY_PIN=1 (mandatory per-job "
                      "login/server identity pin); refusing to allow un-pinned hosted execution")
    if _truthy("MT5_ALLOW_LIVE"):
        errors.append("MT5_HOSTED_EXECUTION is DEMO-ONLY in Phase 1: MT5_ALLOW_LIVE must not be set")
    for cred in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        if str(env.get(cred, "")).strip():
            errors.append(f"MT5_HOSTED_EXECUTION forbids a credential-login attach path: {cred} must be "
                          "unset (a hosted workspace is attached path-only; GuvFX never calls mt5.login())")
    return errors


def evaluate_guarded_attach(path, process_running, init_ok, terminal_connected, account_present):
    """Pure, fail-closed decision for the GUARDED (never-launch) attach — no MT5, no I/O, fully
    unit/mutation-testable. Reports the most specific failure first. Returns (ok: bool, reason: str).

    Inputs are gathered by guarded_initialize() in the ONLY safe order: `process_running` is probed
    BEFORE mt5.initialize() is ever called, so a not-running terminal is rejected here and never launched;
    `init_ok`/`terminal_connected`/`account_present` reflect the attach that followed a positive probe."""
    if not path:
        return False, "guarded_attach_no_path"
    if not process_running:
        return False, "guarded_attach_terminal_not_running"  # never launch a down terminal
    if not init_ok:
        return False, "guarded_attach_initialize_failed"
    if not terminal_connected:
        return False, "guarded_attach_not_connected"  # attached but no live broker link
    if not account_present:
        return False, "guarded_attach_no_account"
    return True, "ok"


def _running_terminal_dirs():
    """Install directories (lowercased) of every currently-running terminal64.exe. FAIL-CLOSED: a directory
    is included ONLY when its executable path is confirmed, so a foreign / image-name-only match can never
    stand in for the target. Tries psutil (precise), then wmic ExecutablePath; any failure yields a partial/
    empty set rather than a false positive."""
    dirs = set()
    try:
        import psutil
        for proc in psutil.process_iter(["exe"]):
            try:
                exe = proc.info.get("exe") or ""
                if exe and os.path.basename(exe).lower() == "terminal64.exe":
                    dirs.add(os.path.dirname(os.path.abspath(exe)).lower())
            except Exception:
                continue
        return dirs
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name='terminal64.exe'", "get", "ExecutablePath", "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        for line in (out.stdout or "").splitlines():
            for field in line.split(","):
                f = field.strip()
                if f.lower().endswith("terminal64.exe"):
                    dirs.add(os.path.dirname(os.path.abspath(f)).lower())
    except Exception:
        pass
    return dirs


def _terminal_process_running(path) -> bool:
    """Is a terminal64.exe already running from `path`'s INSTALL DIRECTORY? Used ONLY to guarantee
    guarded_initialize() never launches MT5. Matches strictly by install directory (never image-name alone),
    so a foreign terminal on a multi-install host can never green-light launching a down target.
    FAIL-CLOSED: no path, an unresolvable path, or an unconfirmable process set ⇒ False."""
    if not path:
        return False
    try:
        target_dir = os.path.dirname(os.path.abspath(path)).lower()
    except Exception:
        return False
    return target_dir in _running_terminal_dirs()


def guarded_initialize(mt5, init_kwargs, *, probe=None) -> bool:
    """Attach to MT5. DARK by default — when MT5_GUARDED_ATTACH is unset this is byte-identical to
    `mt5.initialize(**init_kwargs)` (behaviour-preserving for the legacy/production bridge). When enabled,
    enforce the never-launch invariant via evaluate_guarded_attach(): probe the process BEFORE initialize
    so a down terminal is never launched, require broker-connected + an account identity, else fail closed
    (releasing any attach we opened). NEVER calls mt5.login(); NEVER relaunches."""
    if not _guarded_attach_enabled():
        return bool(mt5.initialize(**init_kwargs))  # legacy passthrough — unchanged

    # Attach-only: the guarded path must NEVER authenticate. mt5.initialize(login=,password=,server=)
    # performs a broker login (it re-authorises the terminal), so credential keys are FORBIDDEN here —
    # initialize may only ATTACH by path. (This fails closed at the legacy /mt5/login-and-validate site,
    # which is a temporary-validation path retired under ADR-0034, not a persistent-workspace attach.)
    if any(k in init_kwargs for k in ("login", "password", "server")):
        logger.warning("guarded_attach rejected: guarded_attach_credentials_forbidden")
        return False

    path = init_kwargs.get("path")
    probe = probe or _terminal_process_running
    # Probe FIRST — the attach below is reached only when a terminal is already running, so it can
    # only ATTACH, never launch. (A sub-millisecond probe→attach TOCTOU window remains: if the terminal
    # exits in the gap, initialize(path=) could launch it — but that worst case equals the legacy path,
    # never worse. See ADR-0034 / EXECUTION_READINESS §9 caveats.)
    running = bool(path) and bool(probe(path))
    init_ok = False
    connected = False
    account = None
    identity = None
    if running:
        try:
            init_ok = bool(mt5.initialize(**init_kwargs))
            if init_ok:
                term = mt5.terminal_info()
                connected = bool(term.connected) if term is not None else False
                account = mt5.account_info()
                if account is not None:
                    login = getattr(account, "login", None)
                    identity = {
                        "login_masked": ("****" + str(login)[-4:]) if login is not None else None,
                        "server": getattr(account, "server", None),
                        "trade_mode": getattr(account, "trade_mode", None),
                    }
        except Exception:
            # A raising initialize/terminal_info/account_info IS the degraded state the guard exists for —
            # fail closed. Any attach opened (init_ok True) is released by the `if not ok` shutdown below.
            connected = False
            account = None

    ok, reason = evaluate_guarded_attach(path, running, init_ok, connected, account is not None)
    if not ok:
        if init_ok:
            try:
                mt5.shutdown()  # release the attach we opened; leave no dangling connection
            except Exception:
                pass
        logger.warning(f"guarded_attach rejected: {reason}")
        return False
    logger.info(f"guarded_attach ok: {identity}")  # masked identity read + recorded; never login
    return True


def evaluate_binding(acc, term, expected):
    """Pure broker-truth binding decision — no MT5, no I/O, fully unit/mutation-testable.

    acc:  None, or a mapping with login / server / trade_mode (trade_mode: 0=DEMO, 1=CONTEST, 2=REAL).
    term: None, or a mapping with connected / trade_allowed.
    expected: mapping with is_demo(bool), allow_live(bool), expected_login(str|None), expected_server(str|None).
    Returns (ok: bool, reason: str). ok is True ONLY when every applicable check passes (fail closed).
    """
    if term is None:
        return False, "terminal_info_unavailable"
    if not term.get("connected"):
        return False, "terminal_not_connected"
    if not term.get("trade_allowed"):
        return False, "trade_not_allowed"
    if acc is None:
        return False, "account_info_unavailable"
    trade_mode = acc.get("trade_mode")
    if trade_mode is None:
        return False, "trade_mode_unavailable"
    is_demo_account = (trade_mode == 0)
    # Classification agreement: a job flagged demo must NOT run against a non-demo account.
    if expected.get("is_demo") and not is_demo_account:
        return False, "classification_mismatch_job_demo_broker_trade_mode_%s" % trade_mode
    # A non-demo (real/contest) account may trade ONLY when live execution is explicitly authorised.
    if not is_demo_account and not expected.get("allow_live"):
        return False, "live_execution_not_authorised_trade_mode_%s" % trade_mode
    # Identity pins (enforced only when configured).
    exp_login = expected.get("expected_login")
    if exp_login and str(acc.get("login")) != str(exp_login):
        return False, "account_login_mismatch"
    exp_server = expected.get("expected_server")
    if exp_server and str(acc.get("server")) != str(exp_server):
        return False, "broker_server_mismatch"
    return True, "ok"


def _acc_snapshot(acc_raw):
    if acc_raw is None:
        return None
    return {
        "login": getattr(acc_raw, "login", None),
        "server": getattr(acc_raw, "server", None),
        "trade_mode": getattr(acc_raw, "trade_mode", None),
    }


def _term_snapshot(term_raw):
    if term_raw is None:
        return None
    return {
        "connected": bool(getattr(term_raw, "connected", False)),
        "trade_allowed": bool(getattr(term_raw, "trade_allowed", False)),
    }


def verify_execution_binding(mt5, payload):
    """Read broker truth from a live MT5 handle and evaluate the binding. Returns (ok, reason, details).

    ``details`` carries only a REDACTED account suffix + server + trade_mode for audit/logs (never the
    full login). A read error is treated as an UNVERIFIED binding (fail closed), never a pass.
    """
    try:
        acc = _acc_snapshot(mt5.account_info())
        term = _term_snapshot(mt5.terminal_info())
        # ADR-0033 (Tension 3) — a persistent-workspace job carries a MANDATORY per-job identity pin.
        # It is enforced when the JOB declares it (payload["require_identity_pin"]) OR when this
        # bridge/terminal is configured to require it (MT5_REQUIRE_IDENTITY_PIN — a deployment property
        # of a persistent-workspace bridge). In that case the expected (login, server) MUST come from the
        # PAYLOAD (never the process env), are MANDATORY for BOTH demo and live, and a missing/half pin
        # fails closed. Legacy jobs/bridges (neither set) keep the EXACT prior behaviour: process-env
        # pins, optional on the demo default. Additive; never weakens the legacy path.
        if payload.get("require_identity_pin") or _require_identity_pin():
            exp_login = str(payload.get("expected_login") or "").strip()
            exp_server = str(payload.get("expected_server") or "").strip()
            if not exp_login or not exp_server:
                return False, "identity_pin_required", {}
        else:
            exp_login = (os.getenv("MT5_EXPECTED_LOGIN", "").strip() or None)
            exp_server = (os.getenv("MT5_EXPECTED_SERVER", "").strip() or None)
        expected = {
            "is_demo": bool(payload.get("is_demo", False)),
            "allow_live": _bridge_allow_live(),
            "expected_login": exp_login,
            "expected_server": exp_server,
        }
        ok, reason = evaluate_binding(acc, term, expected)
        details = {}
        if acc is not None:
            login = acc.get("login")
            details = {
                "account_suffix": ("****" + str(login)[-4:]) if login is not None else None,
                "server": acc.get("server"),
                "trade_mode": acc.get("trade_mode"),
            }
        return ok, reason, details
    except Exception as exc:
        # ANY failure evaluating the binding (broker read, env, or logic) is an UNVERIFIED binding:
        # fail closed, and never surface a login in the message.
        return False, "binding_error_%s" % type(exc).__name__, {}


# ── ADR-0033 Increment 3 — IDENTITY gate for account-mutating operations (close / modify) ──────────
# Opening orders are protected by evaluate_binding/verify_execution_binding (which also enforce demo/live
# classification + trade_allowed). CLOSE and MODIFY are account-mutating too: the position ticket alone
# does NOT authorise the mutation — the terminal's ACTIVE account must be EXACTLY the intended
# (login, server) before the mutation lands, or a terminal that drifted to a different account could have
# the WRONG account's position closed/modified. This gate deliberately does NOT require trade_allowed (a
# risk-reducing close/modify must not be blocked by a transient trading halt — packet E2 "where
# appropriate") and does NOT re-check demo/live classification (close/modify are demo-guarded by callers).
# The pin is MANDATORY on the persistent-workspace path (payload require_identity_pin, or the terminal-level
# MT5_REQUIRE_IDENTITY_PIN); for legacy it matches the opening-order behaviour (process-env pins, enforced
# only when set).
def evaluate_mutation_identity(acc, term, expected):
    """Pure, fail-closed IDENTITY decision for a trade mutation. Returns (ok, reason). ok is True ONLY
    when the terminal is connected, the account is present, and the active (login, server) matches every
    pin that is SET. A None pin is not enforced here — verify_mutation_identity decides mandatory-ness."""
    if term is None:
        return False, "terminal_info_unavailable"
    if not term.get("connected"):
        return False, "terminal_not_connected"
    if acc is None:
        return False, "account_info_unavailable"
    # Re-assert demo AT SEND TIME (close/modify are demo-only): the top-of-function trade_mode check is
    # single-shot, so a terminal that drifted demo→REAL between it and order_send would otherwise mutate a
    # LIVE position. trade_mode: 0=DEMO, 1=CONTEST, 2=REAL — anything but demo fails closed here (parity
    # with the opening-order gate, which re-checks classification before every send).
    if acc.get("trade_mode") != 0:
        return False, "account_not_demo"
    exp_login = expected.get("expected_login")
    if exp_login and str(acc.get("login")) != str(exp_login):
        return False, "account_login_mismatch"
    exp_server = expected.get("expected_server")
    if exp_server and str(acc.get("server")) != str(exp_server):
        return False, "broker_server_mismatch"
    return True, "ok"


def verify_mutation_identity(mt5, identity):
    """Live-read IDENTITY gate for close/modify. ``identity`` (from the request body / job-bound account)
    may carry require_identity_pin + expected_login + expected_server. When the pin is required (the job
    declares it OR MT5_REQUIRE_IDENTITY_PIN is set on this bridge) the expected (login, server) MUST come
    from ``identity`` (never env) and both are mandatory (fail-closed). Otherwise legacy process-env pins
    are used (enforced only when set). Returns (ok, reason, details) with the login redacted; any read
    error fails closed."""
    try:
        acc = _acc_snapshot(mt5.account_info())
        term = _term_snapshot(mt5.terminal_info())
        idobj = identity or {}
        if idobj.get("require_identity_pin") or _require_identity_pin():
            exp_login = str(idobj.get("expected_login") or "").strip()
            exp_server = str(idobj.get("expected_server") or "").strip()
            if not exp_login or not exp_server:
                return False, "identity_pin_required", {}
        else:
            exp_login = (os.getenv("MT5_EXPECTED_LOGIN", "").strip() or None)
            exp_server = (os.getenv("MT5_EXPECTED_SERVER", "").strip() or None)
        ok, reason = evaluate_mutation_identity(
            acc, term, {"expected_login": exp_login, "expected_server": exp_server})
        details = {}
        if acc is not None:
            login = acc.get("login")
            details = {
                "account_suffix": ("****" + str(login)[-4:]) if login is not None else None,
                "server": acc.get("server"),
            }
        return ok, reason, details
    except Exception as exc:
        return False, "mutation_identity_error_%s" % type(exc).__name__, {}


# --- Open-position idempotency guard (Phase 2, Control 4) ------------------------------------------
# A re-delivered / retried PLACE_ORDER (worker retry, job redelivery, agent restart, backend timeout)
# must NOT open a duplicate position. The order comment (e.g. "WAY{plan}L{leg}") is the intent's
# idempotency key; before order_send we check whether an OPEN position already carries it. If so the
# intent already executed — return the existing ticket instead of sending again.
#
# SCOPE / LIMITS (do not overstate — RULE 5/7): this is an OPEN-POSITION check, NOT durable state. The
# primary idempotency guarantee lives in the backend (PLACE_ORDER is never re-enqueued + leg<->job
# OneToOne); this bridge guard is a fail-safe SECOND line. Once a position closes the memory is gone
# (a redelivery after close could re-open — bounded by the backend guarantee). It also assumes the
# broker stores the comment verbatim in the first 31 chars; a broker that mangles comments would make
# the guard miss. Fail SAFE: any read error / non-iterable / None returns None ("no match -> proceed").
def find_existing_execution(mt5, comment):
    """Return {'ticket', 'volume'} for an open position already carrying ``comment``, else None."""
    c = (comment or "")[:31]
    if not c:
        return None
    try:
        positions = mt5.positions_get() or ()
        for p in positions:
            if getattr(p, "comment", "") == c:
                return {"ticket": getattr(p, "ticket", None), "volume": getattr(p, "volume", None)}
    except Exception as exc:
        # Never fail the order on a guard read error — but never stay silent either (a chronic
        # positions_get failure would disable the guard invisibly).
        logger.warning(f"idempotency guard read error ({type(exc).__name__}); proceeding without it")
        return None
    return None


# Timeframe mapping for MT5
TIMEFRAME_MAP = {
    "M1": 1,      # TIMEFRAME_M1
    "M5": 5,      # TIMEFRAME_M5
    "M15": 15,    # TIMEFRAME_M15
    "M30": 30,    # TIMEFRAME_M30
    "H1": 16385,  # TIMEFRAME_H1
    "H4": 16388,  # TIMEFRAME_H4
    "D1": 16408,  # TIMEFRAME_D1
    "W1": 32769,  # TIMEFRAME_W1
    "MN1": 49153, # TIMEFRAME_MN1
}

_consecutive_404_count = 0


def create_http_session() -> requests.Session:
    """Create an HTTP session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_FETCH_RETRIES,
        backoff_factor=RETRY_BASE_DELAY,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = create_http_session()
    return _http_session


def validate_config() -> bool:
    """Validate required configuration is present."""
    errors = []
    if not API_URL:
        errors.append("GUVFX_API_URL is not set")
    if not WORKER_TOKEN:
        errors.append("GUVFX_WORKER_TOKEN is not set")
    if not ACCOUNT_ID:
        errors.append("MT5_ACCOUNT_ID is not set")
    # Fail closed at STARTUP: refuse to run at all unless HTTP authentication is configured, so the bridge
    # can never come up serving protected routes (including the order-placing POSTs) unauthenticated.
    if not HTTP_AUTH_TOKEN:
        errors.append(
            "GUVFX_AGENT_TOKEN is not set (inbound HTTP auth). It has NO fallback: the bridge will not "
            "substitute GUVFX_WORKER_TOKEN or any other credential, and will not start unauthenticated"
        )
    # WS3 startup self-validation: reject obvious placeholder text rather than authenticating with it.
    for _name, _val in (("GUVFX_AGENT_TOKEN", AGENT_TOKEN), ("GUVFX_WORKER_TOKEN", WORKER_TOKEN)):
        if _val and any(m in _val.lower() for m in
                        ("replace", "changeme", "example", "placeholder", "<", "${", "scrubbed")):
            errors.append(f"{_name} looks like placeholder text, not a real secret")

    # Live execution requires an EXACT-ACCOUNT binding pin (Phase 2, Control 1): the binding gate
    # always enforces demo/live classification, but the account-identity half is only enforced when
    # MT5_EXPECTED_LOGIN is set. Refuse to start with live enabled and no account pin (fail closed).
    if _bridge_allow_live() and not os.getenv("MT5_EXPECTED_LOGIN", "").strip():
        errors.append(
            "MT5_ALLOW_LIVE is enabled but MT5_EXPECTED_LOGIN is not set: live execution requires an "
            "exact-account binding pin (set MT5_EXPECTED_LOGIN to the intended broker login)"
        )

    # ADR-0034 Execution Engine (G6) — a HOSTED WORKSPACE execution bridge must refuse to start unless its
    # full safety configuration is present (guarded attach + mandatory pin + demo-only + no credential login).
    # No-op for legacy bridges (MT5_HOSTED_EXECUTION unset). Fail closed: any hosted-config error aborts start.
    errors.extend(evaluate_hosted_startup_config(os.environ))

    if errors:
        for err in errors:
            logger.error(f"Configuration error: {err}")
        return False

    logger.info(f"Configuration validated: API={API_URL}, Account={ACCOUNT_ID}")
    return True


def get_headers() -> Dict[str, str]:
    return {
        "X-Worker-Token": WORKER_TOKEN,
        "Content-Type": "application/json",
    }


def fetch_next_job() -> Optional[Dict[str, Any]]:
    """
    Fetch the next pending PLACE_ORDER job for our account from the API.
    """
    global _consecutive_404_count

    params = {
        "account_id": ACCOUNT_ID,
        "job_type": "PLACE_ORDER",
        "worker_id": f"signal-bridge-{ACCOUNT_ID}",
    }
    query_string = urlencode(params)
    full_url = f"{API_URL}/api/execution/jobs/next/?{query_string}"

    try:
        session = get_http_session()
        response = session.get(
            f"{API_URL}/api/execution/jobs/next/",
            headers=get_headers(),
            params=params,
            timeout=HTTP_TIMEOUT,
        )

        status_code = response.status_code

        if status_code == 204:
            _consecutive_404_count = 0
            return None

        if status_code == 200:
            _consecutive_404_count = 0
            job = response.json()
            logger.info(f"Claimed job {job.get('id')}: {job.get('job_type')}")
            return job

        if status_code == 404:
            _consecutive_404_count += 1
            body_snippet = response.text[:200] if response.text else "(empty)"
            logger.error(
                f"404 Not Found (attempt {_consecutive_404_count}/{MAX_CONSECUTIVE_404})\n"
                f"  URL: {full_url}\n"
                f"  Response: {body_snippet}"
            )
            return None

        body_snippet = response.text[:300] if response.text else "(empty)"
        logger.warning(f"Unexpected HTTP {status_code}: {body_snippet}")
        return None

    except requests.exceptions.Timeout:
        logger.error(f"Timeout ({HTTP_TIMEOUT}s) fetching jobs")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return None


def complete_job(job_id: int, success: bool, result: Dict = None, error_message: str = "") -> bool:
    """Report job completion to the API."""
    url = f"{API_URL}/api/execution/jobs/{job_id}/complete/"
    data = {
        "status": "SUCCESS" if success else "FAILED",
        "result": result or {},
        "error_message": error_message,
    }

    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            session = get_http_session()
            response = session.post(url, headers=get_headers(), json=data, timeout=HTTP_TIMEOUT)

            if response.status_code == 200:
                logger.info(f"Job {job_id} completed: {'SUCCESS' if success else 'FAILED'}")
                return True

            logger.warning(f"Error completing job {job_id} (attempt {attempt}): HTTP {response.status_code}")

        except requests.RequestException as e:
            logger.error(f"Request error completing job {job_id} (attempt {attempt}): {e}")

        if attempt < MAX_FETCH_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            time.sleep(delay)

    logger.error(f"Failed to complete job {job_id} after {MAX_FETCH_RETRIES} attempts")
    return False


def validate_job_safety(job: Dict) -> tuple[bool, str]:
    """
    Validate job against safety rails.
    Returns (is_safe, error_message).
    """
    payload = job.get("payload", {})

    # Check job type
    if job.get("job_type") != "PLACE_ORDER":
        return False, f"Invalid job type: {job.get('job_type')}"

    # Check demo flag
    if not payload.get("is_demo", False):
        return False, "Job not marked as demo. Refusing to execute."

    # Check symbol is present. MT5-native availability (symbol_info/symbol_select) is
    # validated in execute_mt5_trade, where the terminal is initialised — see
    # validate_broker_symbol. No static allowlist: the backend registry owns which
    # symbols are tradable per broker/account.
    symbol = payload.get("symbol", "").upper()
    if not symbol:
        return False, "Symbol is required."

    # Check lot size
    lots = payload.get("lots", 0)
    if lots <= 0:
        return False, f"Invalid lot size: {lots}"
    _mx = _effective_max_lot(payload)
    if lots > _mx:
        return False, f"Lot size {lots} exceeds max {_mx}."

    # Check side
    side = payload.get("side", "").upper()
    if side not in ALLOWED_SIDES:
        return False, f"Side {side} not allowed. Only {ALLOWED_SIDES} permitted."

    # Check SL/TP (required for PLACE_ORDER)
    sl_price = payload.get("sl_price")
    tp_price = payload.get("tp_price")

    if sl_price is None:
        return False, "SL price is required for PLACE_ORDER."
    if tp_price is None:
        return False, "TP price is required for PLACE_ORDER."

    # Validate SL/TP logic
    if side == "BUY":
        # For BUY: SL should be below entry/market, TP above
        entry = payload.get("entry_price")
        if entry:
            if sl_price >= entry:
                return False, f"BUY: SL ({sl_price}) must be below entry ({entry})"
            if tp_price <= entry:
                return False, f"BUY: TP ({tp_price}) must be above entry ({entry})"
    else:  # SELL
        entry = payload.get("entry_price")
        if entry:
            if sl_price <= entry:
                return False, f"SELL: SL ({sl_price}) must be above entry ({entry})"
            if tp_price >= entry:
                return False, f"SELL: TP ({tp_price}) must be below entry ({entry})"

    return True, ""


def execute_mt5_trade(job: Dict) -> tuple[bool, Dict, str]:
    """
    Execute the trade via MetaTrader5 with SL/TP.
    Returns (success, result_dict, error_message).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return False, {}, "MetaTrader5 package not installed. Run: pip install MetaTrader5"

    job_id = job.get("id")
    payload = job.get("payload", {})
    # No implicit default symbol: a blank symbol fails closed in validate_broker_symbol
    # (SYMBOL_NOT_AVAILABLE_ON_MT5) rather than silently defaulting to a EURUSD order.
    symbol = payload.get("symbol", "").upper()
    lots = min(float(payload.get("lots", 0.01)), _effective_max_lot(payload))
    side = payload.get("side", "BUY").upper()
    magic = payload.get("magic", 0)
    sl_price = float(payload.get("sl_price", 0))
    tp_price = float(payload.get("tp_price", 0))
    entry_price = payload.get("entry_price")  # Optional: None = market order
    # Provider (source) symbol — audit-only; preserved through logs/results if present.
    provider_symbol = payload.get("provider_symbol")

    # Use comment from payload or generate one
    comment = payload.get("comment", f"GS{job_id:04d}")
    # Truncate to MT5 limit
    comment = comment[:31]

    prov_str = f" provider_symbol={provider_symbol}" if provider_symbol else ""
    logger.info(f"Job {job_id}: {symbol} {side} {lots} lots, SL={sl_price}, TP={tp_price}, comment='{comment}'{prov_str}")

    # Initialize MT5
    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        error = mt5.last_error()
        return False, {}, f"MT5 initialization failed: {error}"

    try:
        # Phase 2 (Control 1): broker-truth binding gate BEFORE any order. Fail closed — refuse to
        # trade unless the connected terminal is verified as the intended account/classification.
        _bok, _breason, _bdetails = verify_execution_binding(mt5, payload)
        if not _bok:
            logger.error(f"Job {job_id}: EXECUTION BINDING REJECTED: {_breason} {_bdetails}")
            return False, {"error": "binding_rejected", "reason": _breason, **_bdetails}, f"binding_rejected: {_breason}"

        # Phase 2 (Control 4): idempotency guard at the order_send boundary — if this intent's comment
        # is already on an open position, it already executed; return it rather than duplicating.
        _existing = find_existing_execution(mt5, comment)
        if _existing:
            logger.warning(f"Job {job_id}: idempotent no-op, existing ticket={_existing.get('ticket')} comment='{comment}'")
            return True, {"ticket": _existing.get("ticket"), "volume": _existing.get("volume"), "idempotent": True, "comment": comment}, ""

        # MT5-native symbol validation (replaces the old static allowlist). The exact
        # broker symbol resolved by the backend registry must exist and be selectable.
        ok, symbol_info, sym_err = validate_broker_symbol(mt5, symbol)
        if not ok:
            detail = {"error": sym_err}
            if provider_symbol:
                detail["provider_symbol"] = provider_symbol
            return False, detail, f"{symbol}: {sym_err}"

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, {}, f"Failed to get tick for {symbol}"

        # Determine order type and price
        if side == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        # Use entry_price for pending orders (not implemented in MVP)
        # For now, always use market orders
        if entry_price:
            logger.info(f"Note: entry_price={entry_price} specified but using market order at {price}")

        # -----------------------------------------------------------------
        # Force-once override: compute SL/TP from live tick price
        # -----------------------------------------------------------------
        is_forced_once = payload.get("signal_reason") == "forced_once_test"
        forced_sl_pips = 0.0
        forced_tp_pips = 0.0

        if is_forced_once:
            point = symbol_info.point
            digits = symbol_info.digits
            # "pip" = 10 points for 3/5-digit brokers, else 1 point
            pip_value = point * 10 if digits in (3, 5) else point

            forced_sl_pips = FORCE_ONCE_SL_PIPS
            forced_tp_pips = FORCE_ONCE_TP_PIPS
            sl_distance = forced_sl_pips * pip_value
            tp_distance = forced_tp_pips * pip_value

            if side == "BUY":
                sl_price = round(price - sl_distance, digits)
                tp_price = round(price + tp_distance, digits)
            else:
                sl_price = round(price + sl_distance, digits)
                tp_price = round(price - tp_distance, digits)

            logger.info(
                f"FORCE-ONCE override: market_price={price}, "
                f"sl_pips={forced_sl_pips}, tp_pips={forced_tp_pips}, "
                f"pip_value={pip_value}, sl={sl_price}, tp={tp_price}"
            )

        # -----------------------------------------------------------------
        # Enforce broker minimum stop distance (prevents retcode=10016)
        #
        # Uses FOUR inputs to compute the safe stop buffer:
        #   1) trade_stops_level  — broker-mandated minimum (points)
        #   2) trade_freeze_level — broker freeze distance  (points)
        #   3) current spread     — ask-bid in points
        #   4) EXTRA_STOP_BUFFER_POINTS — configurable safety margin
        #
        # If trade_stops_level==0 the broker may still reject stops
        # that fall inside the spread; the retry loop will widen
        # exponentially until order_check passes or retries exhausted.
        # -----------------------------------------------------------------
        point = symbol_info.point
        digits = symbol_info.digits
        stops_level = max(int(symbol_info.trade_stops_level or 0), 0)
        freeze_level = max(int(getattr(symbol_info, "trade_freeze_level", 0) or 0), 0)

        # Current spread in points (integer)
        spread_points = int(round((tick.ask - tick.bid) / point)) if point > 0 else 0

        def _round_price(x):
            """Round price to symbol's digit precision."""
            return round(x, digits)

        # Base buffer = max(stops_level, freeze_level, spread) + extra safety
        # Even when stops_level==0 the spread provides a sane floor so stops
        # are never placed *inside* the spread.
        broker_min = max(stops_level, freeze_level, spread_points)
        buffer_points = broker_min + EXTRA_STOP_BUFFER_POINTS
        initial_buffer_points = buffer_points

        logger.info(
            f"Stop distance calc: stops_level={stops_level} freeze_level={freeze_level} "
            f"spread_pts={spread_points} broker_min={broker_min} "
            f"extra_buffer={EXTRA_STOP_BUFFER_POINTS} => buffer_points={buffer_points} "
            f"(point={point}, digits={digits}, tick_size={symbol_info.trade_tick_size})"
        )

        # Clamp SL/TP to respect minimum distance, with retry loop
        final_sl = sl_price
        final_tp = tp_price
        order_ok = False
        check_rc = None
        check_comment = None

        for clamp_attempt in range(STOP_CLAMP_MAX_RETRIES + 1):
            # Re-fetch tick on retries so price/spread stay current
            if clamp_attempt > 0:
                fresh_tick = mt5.symbol_info_tick(symbol)
                if fresh_tick is not None:
                    tick = fresh_tick
                    if side == "BUY":
                        price = tick.ask
                    else:
                        price = tick.bid
                    spread_points = int(round((tick.ask - tick.bid) / point)) if point > 0 else 0

            buffer_price = buffer_points * point

            if side == "BUY":
                # BUY: SL must be below price, TP above price
                max_sl = _round_price(price - buffer_price)
                min_tp = _round_price(price + buffer_price)
                final_sl = min(sl_price, max_sl) if sl_price > 0 else max_sl
                final_tp = max(tp_price, min_tp) if tp_price > 0 else min_tp
            else:
                # SELL: SL must be above price, TP below price
                min_sl = _round_price(price + buffer_price)
                max_tp = _round_price(price - buffer_price)
                final_sl = max(sl_price, min_sl) if sl_price > 0 else min_sl
                final_tp = min(tp_price, max_tp) if tp_price > 0 else max_tp

            final_sl = _round_price(final_sl)
            final_tp = _round_price(final_tp)

            # Build the order request
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lots,
                "type": order_type,
                "price": price,
                "sl": final_sl,
                "tp": final_tp,
                "deviation": 20,  # 2 pips slippage
                "magic": magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_for(symbol),
            }

            # Pre-flight check
            check_result = mt5.order_check(request)

            if check_result is not None and check_result.retcode == 0:
                order_ok = True
                logger.info(
                    f"order_check PASSED (attempt {clamp_attempt + 1}): "
                    f"SL={final_sl} TP={final_tp} buffer_pts={buffer_points} "
                    f"spread_pts={spread_points} price={price}"
                )
                break

            # order_check failed — log and widen
            check_rc = check_result.retcode if check_result else "None"
            check_comment = check_result.comment if check_result else "None"
            logger.warning(
                f"order_check FAILED (attempt {clamp_attempt + 1}/{STOP_CLAMP_MAX_RETRIES + 1}): "
                f"retcode={check_rc} comment='{check_comment}' "
                f"SL={final_sl} TP={final_tp} buffer_pts={buffer_points} "
                f"spread_pts={spread_points} price={price}"
            )

            if clamp_attempt < STOP_CLAMP_MAX_RETRIES:
                # Exponential widen: double the buffer each retry
                buffer_points = max(buffer_points * 2, initial_buffer_points + (clamp_attempt + 1) * spread_points)

        if not order_ok:
            return False, {
                "ok": False,
                "reason": "order_check_failed",
                "symbol": symbol,
                "side": side,
                "price": price,
                "sl": final_sl,
                "tp": final_tp,
                "stop_distance_info": {
                    "stops_level": stops_level,
                    "freeze_level": freeze_level,
                    "spread_points": spread_points,
                    "initial_buffer_points": initial_buffer_points,
                    "final_buffer_points": buffer_points,
                    "point": point,
                    "digits": digits,
                    "tick_size": symbol_info.trade_tick_size,
                    "last_check_retcode": check_rc,
                    "last_check_comment": check_comment,
                },
            }, (
                f"order_check failed after {STOP_CLAMP_MAX_RETRIES + 1} attempts: "
                f"symbol={symbol} side={side} price={price} sl={final_sl} tp={final_tp} "
                f"stops_level={stops_level} freeze_level={freeze_level} "
                f"spread_pts={spread_points} buffer_pts={buffer_points}"
            )

        # Log any SL/TP adjustments
        if final_sl != sl_price or final_tp != tp_price:
            logger.info(
                f"SL/TP clamped: SL {sl_price}->{final_sl}, TP {tp_price}->{final_tp} "
                f"(buffer_pts={buffer_points}, buffer_price={buffer_price:.{digits}f})"
            )

        logger.info(f"Sending order: {symbol} {side} {lots} @ {price}, SL={final_sl}, TP={final_tp}")

        # ADR-0033 (Tension 3) — TOCTOU close: re-verify the binding as the LAST check IMMEDIATELY before
        # order_send. The pre-flight check at function entry can have gone stale by now (a user can switch
        # the active Navigator account mid-flow); this pre-send verification is AUTHORITATIVE. Additive
        # strengthening only — it re-runs the same fail-closed binding gate and never weakens legacy.
        _psok, _psreason, _psdetails = verify_execution_binding(mt5, payload)
        if not _psok:
            logger.error(f"Job {job_id}: PRE-SEND BINDING REJECTED: {_psreason} {_psdetails}")
            return False, {"error": "binding_rejected", "reason": _psreason, **_psdetails}, f"binding_rejected: {_psreason}"

        # Send order
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            # Lost ACK (Control 2, guaranteed exposure recovery): order_send gave no response, but the
            # broker MAY have filled. If a position with this comment is already visible, the order
            # landed — report it as FILLED rather than a failure that would strand exposure the platform
            # believes does not exist. Fast in-process path ONLY: a settlement-lag miss (position not yet
            # visible) still reports failure and is only PARTIALLY backstopped by periodic SYNC ingest
            # (which fires for accounts holding open plans) — a pre-existing residual this diff shrinks,
            # not a new gap.
            _recovered = find_existing_execution(mt5, comment)
            if _recovered:
                logger.warning(f"Job {job_id}: lost-ACK recovery — position {_recovered.get('ticket')} exists for comment '{comment}' (treating as filled)")
                return True, {"ticket": _recovered.get("ticket"), "volume": _recovered.get("volume"), "lost_ack_recovered": True, "comment": comment}, ""
            return False, {}, f"Order send returned None: {error}"

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # Check for market closed (retcode=10018 or comment contains "Market closed")
            is_market_closed = (
                result.retcode == 10018 or
                (result.comment and "market closed" in result.comment.lower())
            )

            if is_market_closed:
                # Special handling: return structured result so backend knows it's market_closed
                market_closed_result = {
                    "ok": False,
                    "reason": "market_closed",
                    "retcode": result.retcode,
                    "comment": result.comment,
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "sl_price": final_sl,
                    "tp_price": final_tp,
                    "lots": lots,
                    "market_closed": True,
                }
                return False, market_closed_result, f"market_closed retcode={result.retcode}"

            return False, {}, (
                f"Order failed: retcode={result.retcode}, comment={result.comment}, "
                f"sl={final_sl}, tp={final_tp}, price={price}"
            )

        # Success!
        result_dict = {
            "ticket": result.order,
            "price": result.price,
            "volume": result.volume,
            "symbol": symbol,
            "order_type": side,
            "sl": final_sl,
            "tp": final_tp,
            "placed_at": datetime.utcnow().isoformat() + "Z",
            "comment": comment,
            "retcode": result.retcode,
            "stop_distance_info": {
                "stops_level": stops_level,
                "freeze_level": freeze_level,
                "spread_points": spread_points,
                "initial_buffer_points": initial_buffer_points,
                "final_buffer_points": buffer_points,
                "point": point,
                "digits": digits,
                "tick_size": symbol_info.trade_tick_size,
                "original_sl": sl_price,
                "original_tp": tp_price,
                "forced_override": is_forced_once,
                "forced_sl_pips": forced_sl_pips,
                "forced_tp_pips": forced_tp_pips,
                "market_price_used": price,
            },
        }

        logger.info(f"Order executed: ticket={result.order}, price={result.price}, SL={final_sl}, TP={final_tp}")

        # Post-trade delay: sleep before completing job to allow MT5 to commit deal to history
        # This mitigates the race where SYNC_POSITIONS runs before the deal appears
        if POST_TRADE_SYNC_DELAY > 0:
            logger.info(f"Post-trade delay: sleeping {POST_TRADE_SYNC_DELAY}s before completing job (sync race mitigation)")
            time.sleep(POST_TRADE_SYNC_DELAY)

        return True, result_dict, ""

    finally:
        mt5.shutdown()


def process_job(job: Dict) -> None:
    """Process a single execution job."""
    job_id = job.get("id")
    logger.info(f"Processing job {job_id}")

    # Validate safety
    is_safe, safety_error = validate_job_safety(job)
    if not is_safe:
        logger.warning(f"Job {job_id} failed safety check: {safety_error}")
        complete_job(job_id, success=False, error_message=f"SAFETY_CHECK_FAILED: {safety_error}")
        return

    # Execute trade
    success, result, error = execute_mt5_trade(job)

    # Report result
    complete_job(job_id, success=success, result=result, error_message=error)


# =============================================================================
# HTTP Server for OHLC Data
# =============================================================================


def fetch_ohlc_rates(symbol: str, timeframe: str, count: int, start_pos: int = 0) -> Dict[str, Any]:
    """
    Fetch OHLC rates from MT5.

    Args:
        symbol: Trading symbol (e.g., EURUSD)
        timeframe: Timeframe string (H4, D1, etc.)
        count: Number of bars to fetch (max 1000)
        start_pos: Starting position offset (0 = most recent)

    Returns:
        Dict with ok, data, and metadata
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "MetaTrader5 package not installed"}

    # Validate timeframe
    if timeframe not in TIMEFRAME_MAP:
        return {"ok": False, "error": f"Invalid timeframe: {timeframe}. Valid: {list(TIMEFRAME_MAP.keys())}"}

    # Validate count (allow up to 1000 per request for batch support)
    if count <= 0 or count > 1000:
        return {"ok": False, "error": f"Count must be 1-1000, got: {count}"}

    # Initialize MT5
    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        error = mt5.last_error()
        return {"ok": False, "error": f"MT5 initialization failed: {error}"}

    try:
        # Check symbol exists
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"ok": False, "error": f"Symbol {symbol} not found in MT5"}

        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return {"ok": False, "error": f"Failed to select symbol {symbol}"}

        # Get timeframe constant
        tf_value = TIMEFRAME_MAP[timeframe]

        # Fetch rates from specified position (0 = most recent)
        rates = mt5.copy_rates_from_pos(symbol, tf_value, start_pos, count)

        if rates is None or len(rates) == 0:
            error = mt5.last_error()
            return {"ok": False, "error": f"Failed to fetch rates: {error}"}

        # Convert to list of dicts
        data = []
        for rate in rates:
            data.append({
                "time": int(rate[0]),
                "open": float(rate[1]),
                "high": float(rate[2]),
                "low": float(rate[3]),
                "close": float(rate[4]),
                "tick_volume": int(rate[5]),
            })

        return {
            "ok": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(data),
            "data": data,
        }

    finally:
        mt5.shutdown()


# =============================================================================
# Demo Order Execution (POST /mt5/order endpoint handler)
# =============================================================================

def execute_demo_order(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a demo market order via MT5 with strict safety rails.

    Called by the HTTP handler for POST /mt5/order.
    Returns a dict with ok, retcode, order, deal, comment, etc.
    """
    symbol = str(params.get("symbol", "")).upper()
    side = str(params.get("side", "")).upper()
    lots = float(params.get("lots", 0))
    magic = int(params.get("magic", 0))
    comment = str(params.get("comment", ""))
    # Provider (source) symbol — audit-only; preserved in logs/result if present.
    provider_symbol = params.get("provider_symbol")

    # --- Safety validation (MT5-native symbol availability is checked after init) ---
    if not symbol:
        return {"ok": False, "error": "symbol_required"}

    if side not in DEMO_ORDER_ALLOWED_SIDES:
        return {"ok": False, "error": "side_not_allowed", "detail": f"{side} not in {DEMO_ORDER_ALLOWED_SIDES}"}

    _mx = _effective_max_lot(params)
    if lots <= 0 or lots > _mx:
        return {"ok": False, "error": "lots_out_of_range", "detail": f"lots={lots}, max={_mx}"}

    if not comment:
        return {"ok": False, "error": "comment_required"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        error = mt5.last_error()
        return {"ok": False, "error": "mt5_init_failed", "detail": str(error)}

    try:
        # Verify account is demo (the HTTP order path is demo-only; kept FIRST so the existing
        # account_not_demo contract is preserved for non-demo accounts).
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}

        # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo", "detail": f"trade_mode={account_info.trade_mode}"}

        # Phase 2 (Control 1): broker-truth binding gate. Fail closed — adds connected / trade_allowed
        # and optional login/server pin verification on top of the demo check above.
        _bok, _breason, _bdetails = verify_execution_binding(mt5, params)
        if not _bok:
            logger.error(f"[/mt5/order] EXECUTION BINDING REJECTED: {_breason} {_bdetails}")
            return {"ok": False, "error": "binding_rejected", "reason": _breason, **_bdetails}

        # Phase 2 (Control 4): idempotency guard — if this intent's comment is already on an open
        # position, it already executed; return it rather than duplicating.
        _existing = find_existing_execution(mt5, comment)
        if _existing:
            logger.warning(f"[/mt5/order] idempotent no-op, existing ticket={_existing.get('ticket')} comment='{comment[:31]}'")
            return {"ok": True, "idempotent": True, "order": _existing.get("ticket"), "volume": _existing.get("volume"), "comment": comment[:31]}

        # MT5-native symbol validation (replaces the old static allowlist). The exact
        # broker symbol resolved by the backend registry must exist and be selectable.
        ok, symbol_info, sym_err = validate_broker_symbol(mt5, symbol)
        if not ok:
            res = {"ok": False, "error": sym_err, "detail": f"{symbol} not tradable on MT5"}
            if provider_symbol:
                res["provider_symbol"] = provider_symbol
            return res

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"ok": False, "error": "tick_failed"}

        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "deviation": int(params.get("deviation", 20)),
            "magic": magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_for(symbol),
        }

        # Optional SL/TP (from PLACE_ORDER signal jobs)
        sl = params.get("sl")
        tp = params.get("tp")
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        sl_str = f" sl={sl}" if sl else ""
        tp_str = f" tp={tp}" if tp else ""
        prov_str = f" provider_symbol={provider_symbol}" if provider_symbol else ""
        # Phase 2 (Control 3): pre-flight order_check parity with the poller path — validate the request
        # before sending. Fail SAFE: a None/errored check does NOT block (the broker still gives the
        # verdict on send); a check that reports a non-executable retcode blocks the pointless send.
        try:
            _chk = mt5.order_check(request)
        except Exception:
            _chk = None
        if _chk is not None and getattr(_chk, "retcode", 0) not in (0, mt5.TRADE_RETCODE_DONE):
            logger.warning(f"[/mt5/order] order_check rejected pre-send: retcode={getattr(_chk, 'retcode', None)} comment={getattr(_chk, 'comment', '')}")
            return {"ok": False, "error": "order_check_failed", "retcode": getattr(_chk, "retcode", None), "comment": getattr(_chk, "comment", "")}

        logger.info(f"[/mt5/order] Sending: {symbol} {side} {lots} @ {price}{sl_str}{tp_str}{prov_str} comment='{comment[:31]}'")

        # ADR-0033 (Tension 3) — TOCTOU close: authoritative pre-send re-verification immediately before
        # order_send (the pre-flight check above can be stale). Additive strengthening; never weakens legacy.
        _psok, _psreason, _psdetails = verify_execution_binding(mt5, params)
        if not _psok:
            logger.error(f"[/mt5/order] PRE-SEND BINDING REJECTED: {_psreason} {_psdetails}")
            return {"ok": False, "error": "binding_rejected", "reason": _psreason, **_psdetails}
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            # Lost ACK (Control 2): reconcile before declaring failure. Fast path only — a settlement-lag
            # miss still reports failure (only partially backstopped by periodic SYNC ingest); pre-existing
            # residual this shrinks, not a new gap.
            _recovered = find_existing_execution(mt5, comment)
            if _recovered:
                logger.warning(f"[/mt5/order] lost-ACK recovery — position {_recovered.get('ticket')} exists for comment '{comment[:31]}' (treating as filled)")
                return {"ok": True, "lost_ack_recovered": True, "order": _recovered.get("ticket"), "volume": _recovered.get("volume"), "comment": comment[:31]}
            return {"ok": False, "error": "order_send_none", "detail": str(error)}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {
                "ok": False,
                "error": "order_rejected",
                "retcode": result.retcode,
                "comment": result.comment,
            }

        logger.info(f"[/mt5/order] Success: ticket={result.order}, price={result.price}")
        success = {
            "ok": True,
            "retcode": result.retcode,
            "order": result.order,
            "deal": result.deal,
            "price": result.price,
            "volume": result.volume,
            "comment": comment[:31],
        }
        if provider_symbol:
            success["provider_symbol"] = provider_symbol
        return success

    except Exception as e:
        logger.exception(f"[/mt5/order] Exception: {e}")
        return {"ok": False, "error": "exception", "detail": str(e)}

    finally:
        mt5.shutdown()


# =============================================================================
# =============================================================================
# EXEC-E2b — SHADOW dry-run: mt5.order_check() ONLY, never mt5.order_send()
# =============================================================================
# shadow_order_check runs the SAME demo validation and builds the EXACT SAME MT5
# request as execute_demo_order (above), then calls mt5.order_check(request) —
# a broker-side validation that computes margin/retcode WITHOUT placing a trade.
# It NEVER calls mt5.order_send: no order, no ticket, no deal. execute_demo_order
# is left byte-for-byte unchanged; a test pins that shadow_order_check builds an
# identical request. Called by the HTTP handler for POST /mt5/order_check.


def shadow_order_check(params: Dict[str, Any]) -> Dict[str, Any]:
    """SHADOW dry-run of a demo market order: validate + order_check, NO order_send.

    Returns validation diagnostics (retcode, margin, free margin, comment,
    request). Never places a trade — there is no ``mt5.order_send`` call in this
    function.
    """
    symbol = str(params.get("symbol", "")).upper()
    side = str(params.get("side", "")).upper()
    lots = float(params.get("lots", 0))
    magic = int(params.get("magic", 0))
    comment = str(params.get("comment", ""))
    # Provider (source) symbol — audit-only; preserved in logs/result if present.
    provider_symbol = params.get("provider_symbol")

    # --- Safety validation (identical to execute_demo_order — nothing bypassed) ---
    if not symbol:
        return {"ok": False, "shadow": True, "error": "symbol_required"}
    if side not in DEMO_ORDER_ALLOWED_SIDES:
        return {"ok": False, "shadow": True, "error": "side_not_allowed", "detail": f"{side} not in {DEMO_ORDER_ALLOWED_SIDES}"}
    _mx = _effective_max_lot(params)
    if lots <= 0 or lots > _mx:
        return {"ok": False, "shadow": True, "error": "lots_out_of_range", "detail": f"lots={lots}, max={_mx}"}
    if not comment:
        return {"ok": False, "shadow": True, "error": "comment_required"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "shadow": True, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        return {"ok": False, "shadow": True, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        # Verify account is demo (broker truth — same check as the live path).
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "shadow": True, "error": "account_info_failed"}
        if account_info.trade_mode != 0:  # 0=DEMO, 1=CONTEST, 2=REAL
            return {"ok": False, "shadow": True, "error": "account_not_demo", "detail": f"trade_mode={account_info.trade_mode}"}

        # MT5-native symbol validation (replaces the old static allowlist) — same as
        # the live path, so the request built below stays byte-identical.
        ok, symbol_info, sym_err = validate_broker_symbol(mt5, symbol)
        if not ok:
            res = {"ok": False, "shadow": True, "error": sym_err, "detail": f"{symbol} not tradable on MT5"}
            if provider_symbol:
                res["provider_symbol"] = provider_symbol
            return res

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"ok": False, "shadow": True, "error": "tick_failed"}

        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid

        # EXACT SAME request dict as execute_demo_order.
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "deviation": int(params.get("deviation", 20)),
            "magic": magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_for(symbol),
        }
        sl = params.get("sl")
        tp = params.get("tp")
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        prov_str = f" provider_symbol={provider_symbol}" if provider_symbol else ""
        logger.info(
            f"[/mt5/order_check] SHADOW validating (NO order): {symbol} {side} {lots} @ {price}{prov_str} "
            f"comment='{comment[:31]}'"
        )
        # DRY RUN — validation only. There is deliberately NO mt5.order_send here.
        check = mt5.order_check(request)
        if check is None:
            return {"ok": False, "shadow": True, "error": "order_check_none",
                    "detail": str(mt5.last_error()), "request": request}

        # order_check retcode 0 == request is valid (would be accepted).
        result = {
            "ok": bool(check.retcode == 0),
            "shadow": True,
            "suppressed": True,
            "order_send_called": False,
            "retcode": int(check.retcode),
            "comment": getattr(check, "comment", ""),
            "margin": getattr(check, "margin", None),
            "free_margin": getattr(check, "margin_free", None),
            "balance": getattr(check, "balance", None),
            # Projected POST-trade account state (from order_check) — the free-margin guard
            # rejects a promotion whose projected margin_level would fall below the floor.
            "equity": getattr(check, "equity", None),
            "margin_level": getattr(check, "margin_level", None),
            "request": request,
        }
        if provider_symbol:
            result["provider_symbol"] = provider_symbol
        return result

    except Exception as e:
        logger.exception(f"[/mt5/order_check] Exception: {e}")
        return {"ok": False, "shadow": True, "error": "exception", "detail": str(e)}

    finally:
        mt5.shutdown()


# Deals Snapshot (GET /mt5/snapshots/deals — used by SYNC_POSITIONS worker)
# =============================================================================

def fetch_deals_snapshot(username: str) -> Dict[str, Any]:
    """Fetch deal history from MT5 for the SYNC_POSITIONS worker."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo"}

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc) + timedelta(days=1)
        since = now - timedelta(days=90)

        deals = mt5.history_deals_get(since, now)
        if deals is None:
            deals = ()

        deal_list = []
        for d in deals:
            deal_list.append({
                "ticket": str(d.ticket),
                "order": d.order,
                "time": d.time,
                "time_utc": datetime.utcfromtimestamp(d.time).isoformat() + "Z" if d.time else None,
                "type": d.type,
                "side": "BUY" if d.type == 0 else "SELL" if d.type == 1 else str(d.type),
                "symbol": d.symbol,
                "volume": d.volume,
                "price": d.price,
                "profit": d.profit,
                "commission": d.commission,
                "swap": d.swap,
                "magic": d.magic,
                "comment": d.comment,
                "position_id": d.position_id,
                "entry": d.entry,  # DEAL_ENTRY_* (0=IN/open,1=OUT/close,2=INOUT,3=OUT_BY) — lets the ingest worker split open vs close deals per position
            })

        return {"ok": True, "deals": deal_list, "count": len(deal_list)}

    except Exception as e:
        logger.exception(f"[deals] Exception: {e}")
        return {"ok": False, "error": "exception", "detail": str(e)}
    finally:
        mt5.shutdown()


# =============================================================================
# Positions + Close Position (for cleanup/management)
# =============================================================================

def fetch_positions(symbol: str = "") -> Dict[str, Any]:
    """Fetch open positions from MT5."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo"}

        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()

        if positions is None:
            positions = ()

        pos_list = []
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "side": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "profit": p.profit,
                "magic": p.magic,
                "comment": p.comment,
            })

        return {"ok": True, "positions": pos_list, "count": len(pos_list)}

    except Exception as e:
        return {"ok": False, "error": "exception", "detail": str(e)}
    finally:
        mt5.shutdown()


def close_position(ticket: int, identity: Dict[str, Any] = None) -> Dict[str, Any]:
    """Close an open position by ticket. Demo accounts only. ADR-0033 Inc3: ``identity`` (from the
    request body / job-bound account) drives the pre-send IDENTITY gate so a drifted terminal cannot
    close the WRONG account's position; legacy callers pass None ⇒ process-env pins."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo"}

        # Control 2 (guaranteed exposure recovery, close side): a single order_send leaves the position
        # OPEN on any transient reject. Retry a BOUNDED number of times with a FRESH price on retryable
        # retcodes, re-checking each attempt whether the position has already gone (a lost-ACK close may
        # have landed). On persistent failure return an EXPLICIT residual_exposure marker so the caller/
        # operator knows the position is STILL OPEN.
        last_err, last_rc, last_detail = "close_failed", None, None
        for _attempt in range(CLOSE_MAX_ATTEMPTS):
            positions = mt5.positions_get(ticket=ticket)
            if positions is None:
                # A query/IPC error is NOT proof the position is flat — NEVER infer "closed" from it
                # (that would strand exposure silently). Retry; persistent failure -> residual_exposure.
                last_err, last_rc, last_detail = "positions_get_failed", None, str(mt5.last_error())
                continue
            if len(positions) == 0:
                if _attempt == 0:
                    return {"ok": False, "error": "position_not_found", "detail": f"ticket={ticket}"}
                # EXPLICITLY empty after a prior attempt -> the close landed (or a lost-ACK close did)
                logger.info(f"[close] ticket={ticket} confirmed flat on re-check (attempt {_attempt})")
                return {"ok": True, "ticket": ticket, "closed": True,
                        "close_order": None, "close_deal": None, "close_price": None, "volume": None,
                        "note": "position confirmed flat on re-check"}

            pos = positions[0]
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                last_err, last_rc, last_detail = "tick_failed", None, None
                continue

            if pos.type == mt5.POSITION_TYPE_BUY:
                close_type = mt5.ORDER_TYPE_SELL
                close_price = tick.bid
            else:
                close_type = mt5.ORDER_TYPE_BUY
                close_price = tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": ticket,
                "price": close_price,
                "deviation": 20,
                "magic": pos.magic,
                "comment": "GUVFX_CLOSE",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_for(pos.symbol),
            }

            # ADR-0033 Inc3 — IDENTITY gate immediately before the close order_send (TOCTOU-narrowed): the
            # ticket alone is not authorisation; the terminal's ACTIVE account must be the intended one.
            _idok, _idreason, _iddetails = verify_mutation_identity(mt5, identity)
            if not _idok:
                # A HARD mismatch (wrong/real account, or a missing mandatory pin) is a non-retryable
                # REJECT — never close the wrong account's position. A TRANSIENT failure (not connected /
                # info unavailable / read error) is retried like the loop's other transient errors, so a
                # single flap does not abort the close and, on exhaustion, surfaces the explicit
                # residual_exposure marker rather than a misleading authorization error.
                if _idreason in ("account_login_mismatch", "broker_server_mismatch",
                                 "identity_pin_required", "account_not_demo"):
                    logger.error(f"[close] ticket={ticket} IDENTITY REJECTED: {_idreason} {_iddetails}")
                    return {"ok": False, "error": "identity_rejected", "reason": _idreason,
                            **_iddetails, "ticket": ticket}
                last_err, last_rc, last_detail = f"identity_{_idreason}", None, None
                continue

            result = mt5.order_send(request)
            if result is None:
                # lost ACK — the close MAY have landed; re-check the position next iteration
                last_err, last_rc, last_detail = "order_send_none", None, str(mt5.last_error())
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[close] Closed ticket={ticket}: order={result.order} deal={result.deal} price={result.price}")
                return {
                    "ok": True,
                    "ticket": ticket,
                    "close_order": result.order,
                    "close_deal": result.deal,
                    "close_price": result.price,
                    "volume": result.volume,
                }
            last_err, last_rc, last_detail = "close_rejected", result.retcode, result.comment
            if result.retcode not in CLOSE_RETRYABLE_RC:
                break  # hard rejection — retrying won't help

        # Exhausted attempts (or hard-rejected): as far as we can tell the position is STILL OPEN.
        logger.error(f"[close] FAILED to close ticket={ticket} after {CLOSE_MAX_ATTEMPTS} attempts: "
                     f"{last_err} retcode={last_rc}")
        return {"ok": False, "error": "close_failed_position_open", "residual_exposure": True,
                "last_error": last_err, "retcode": last_rc, "comment": last_detail, "ticket": ticket}

    except Exception as e:
        return {"ok": False, "error": "exception", "detail": str(e)}
    finally:
        mt5.shutdown()


def modify_position(ticket: int, sl: float, tp: float = None, identity: Dict[str, Any] = None) -> Dict[str, Any]:
    """WS-B AUTO-BREAKEVEN — move an OPEN position's stop-loss (and optionally keep its
    take-profit) via ``TRADE_ACTION_SLTP``. Demo accounts only. Never opens/closes a position:
    ``TRADE_ACTION_SLTP`` only edits SL/TP on an existing position.

    Fail-closed and idempotent-friendly: the caller (backend breakeven sweep) has already
    verified the move is risk-reducing; here we additionally RE-READ the position after the
    modify and return the broker's post-modify ``sl`` (``verified_sl``) so the caller can
    prove the modification actually landed. If the position's SL already equals the requested
    value (a retry after a successful-but-unconfirmed send), we report success without resending.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not guarded_initialize(mt5, init_kwargs):
        return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}
        if account_info.trade_mode != 0:  # 0 == demo; refuse on real accounts
            return {"ok": False, "error": "account_not_demo"}

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"ok": False, "error": "position_not_found", "detail": f"ticket={ticket}"}
        pos = positions[0]

        # Keep the existing TP unless the caller supplied one explicitly.
        target_tp = pos.tp if tp is None else tp

        # Precision: round SL/TP to the symbol's digits so order_send doesn't reject on noise.
        info = mt5.symbol_info(pos.symbol)
        digits = getattr(info, "digits", 5) if info else 5
        req_sl = round(float(sl), digits)
        req_tp = round(float(target_tp), digits)
        eps = (10 ** -digits) / 2

        # Idempotency: if the SL is already at (or past) the requested breakeven, do not resend.
        cur_sl = float(pos.sl)
        if abs(cur_sl - req_sl) < eps:
            return {"ok": True, "ticket": ticket, "prior_sl": cur_sl, "requested_sl": req_sl,
                    "verified_sl": cur_sl, "unchanged": True}

        # Defense-in-depth FAIL-SAFE — refuse any move that would INCREASE risk. A BUY's SL may
        # only move up (toward/above entry); a SELL's SL may only move down. A position with no SL
        # (cur_sl == 0) may always receive one (strictly safer than unbounded). This is a hard
        # broker-side backstop under the backend sweep's own risk-reducing guard.
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        if cur_sl != 0.0:
            if is_buy and req_sl < cur_sl - eps:
                return {"ok": False, "error": "would_increase_risk",
                        "current_sl": cur_sl, "requested_sl": req_sl}
            if (not is_buy) and req_sl > cur_sl + eps:
                return {"ok": False, "error": "would_increase_risk",
                        "current_sl": cur_sl, "requested_sl": req_sl}

        # Broker stops/freeze band — a TP2-lock SL can sit near live market when TP2 has just closed
        # (the SL == the TP2 price, which is right where price is). order_send would reject it with
        # INVALID_STOPS. Rather than let that surface as a generic hard failure (which the backend
        # sweep escalates to a CRITICAL page), detect it here and return a DISTINCT, RETRYABLE reason.
        # The remaining position is heading toward TP3 — away from this SL — so the band clears within
        # a tick or two and the lock lands on a later sweep. FAIL-CLOSED: on any missing tick/point we
        # proceed to order_send (the broker stays the final authority; it just rejects if invalid).
        point = getattr(info, "point", 0.0) if info else 0.0
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is not None and point and point > 0:
            stops_level = max(int(getattr(info, "trade_stops_level", 0) or 0), 0)
            freeze_level = max(int(getattr(info, "trade_freeze_level", 0) or 0), 0)
            spread_points = int(round((tick.ask - tick.bid) / point))
            min_points = max(stops_level, freeze_level, spread_points, 0)
            min_dist = min_points * point
            # BUY closes at Bid (SL below Bid); SELL closes at Ask (SL above Ask). Distance must clear
            # the band; a negative distance means the SL is already on/through market → also too close.
            dist = (tick.bid - req_sl) if is_buy else (req_sl - tick.ask)
            if dist < min_dist - eps:
                return {"ok": False, "error": "sl_within_stops_level", "retryable": True,
                        "ticket": ticket, "requested_sl": req_sl, "prior_sl": cur_sl,
                        "bid": tick.bid, "ask": tick.ask, "min_points": min_points,
                        "dist_points": (dist / point) if point else None,
                        "stops_level": stops_level, "freeze_level": freeze_level}

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": req_sl,
            "tp": req_tp,
            "magic": pos.magic,
        }

        # ADR-0033 Inc3 — IDENTITY gate immediately before the modify order_send (TOCTOU-narrowed).
        _idok, _idreason, _iddetails = verify_mutation_identity(mt5, identity)
        if not _idok:
            logger.error(f"[modify] ticket={ticket} IDENTITY REJECTED: {_idreason} {_iddetails}")
            return {"ok": False, "error": "identity_rejected", "reason": _idreason, **_iddetails, "ticket": ticket}

        result = mt5.order_send(request)
        if result is None:
            return {"ok": False, "error": "order_send_none", "detail": str(mt5.last_error())}
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # A position can close in the window between the pre-check and order_send (e.g. TP3 fills
            # while we modify it) → the broker rejects with a generic retcode. Re-read: if the
            # position is genuinely gone, this is the same benign no-op as position_not_found (there
            # is nothing left to protect), not a hard failure worth retrying/paging.
            still_open = mt5.positions_get(ticket=ticket)
            if not still_open:
                return {"ok": False, "error": "position_not_found",
                        "detail": f"closed during modify; retcode={result.retcode}", "ticket": ticket}
            return {"ok": False, "error": "modify_rejected", "retcode": result.retcode,
                    "comment": result.comment}

        # Broker-side VERIFICATION: re-read the position and confirm the SL actually moved.
        after = mt5.positions_get(ticket=ticket)
        verified_sl = float(after[0].sl) if after else None
        verified = verified_sl is not None and abs(verified_sl - req_sl) < (10 ** -digits) / 2
        logger.info(f"[modify] ticket={ticket} sl->{req_sl} retcode={result.retcode} verified={verified}")
        return {
            "ok": bool(verified),
            "ticket": ticket,
            "prior_sl": cur_sl,          # broker SL before this modify (protection-evidence)
            "requested_sl": req_sl,
            "verified_sl": verified_sl,
            "retcode": result.retcode,
            "comment": getattr(result, "comment", ""),
            "error": None if verified else "sl_not_verified",
        }

    except Exception as e:
        return {"ok": False, "error": "exception", "detail": str(e)}
    finally:
        mt5.shutdown()


# =============================================================================
# Login-and-Validate (POST /mt5/login-and-validate)
# =============================================================================

def login_and_validate(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Log into an MT5 account and validate credentials.
    Does NOT execute trades — read-only validation only.

    Accepts: username, login, password, server
    Returns: ok, valid, login, server, balance, currency, trade_mode, etc.
    """
    login_str = str(params.get("login", "")).strip()
    password = str(params.get("password", "")).strip()
    server = str(params.get("server", "")).strip()

    if not login_str or not password or not server:
        return {"ok": False, "valid": False, "reason": "missing_fields",
                "detail": "login, password, and server are required"}

    try:
        login_int = int(login_str)
    except ValueError:
        return {"ok": False, "valid": False, "reason": "invalid_login",
                "detail": "login must be numeric"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "valid": False, "reason": "mt5_not_installed"}

    init_kwargs = {
        "login": login_int,
        "password": password,
        "server": server,
    }
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    # Initialize and login in one step — handles terminals with no saved session
    if not guarded_initialize(mt5, init_kwargs):
        err = mt5.last_error()
        # Distinguish init failure from auth failure
        err_code = err[0] if isinstance(err, tuple) else 0
        if err_code == -6:  # Authorization failed
            return {"ok": True, "valid": False, "reason": "login_failed",
                    "detail": str(err)}
        return {"ok": False, "valid": False, "reason": "mt5_init_failed",
                "detail": str(err)}

    try:

        info = mt5.account_info()
        if info is None:
            return {"ok": True, "valid": False, "reason": "account_info_failed"}

        result = {
            "ok": True,
            "valid": True,
            "reason": "ok",
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "currency": info.currency,
            "trade_mode": info.trade_mode,
            "trade_allowed": info.trade_allowed,
            "trade_expert": info.trade_expert,
            "name": info.name,
            "leverage": info.leverage,
        }

        logger.info(f"[login-validate] OK: login={info.login} server={info.server} "
                     f"mode={info.trade_mode} balance={info.balance}")
        return result

    except Exception as e:
        logger.exception(f"[login-validate] Exception: {e}")
        return {"ok": False, "valid": False, "reason": "exception", "detail": str(e)}

    finally:
        mt5.shutdown()


class OHLCRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for OHLC data, deals snapshots, and demo order execution."""

    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.debug(f"HTTP: {args[0]}")

    def _send_json_response(self, data: Dict, status_code: int = 200):
        """Send JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _validate_token(self) -> bool:
        """
        Validate the agent token for EVERY protected route. **Fails closed.**

        Authentication is mandatory and uses GUVFX_AGENT_TOKEN ONLY (``HTTP_AUTH_TOKEN``) — there is no
        fallback to another service's credential. If it is not configured — missing, empty, or
        whitespace-only — every protected request is DENIED. There is deliberately NO mode in which an
        unconfigured bridge serves
        protected routes unauthenticated: the previous implementation ended with a permissive allow-all
        fallback, so a bridge started without its env var accepted every request — including the
        order-placing POST routes.

        The comparison is constant-time so a caller cannot recover the token from response timing.
        """
        provided_token = self.headers.get("X-GuvFX-Agent-Token", "") or ""

        if not HTTP_AUTH_TOKEN:
            # Fail closed. Startup validation should already have prevented this state; deny regardless.
            logger.error("HTTP auth denied: no bridge auth token configured (fail-closed)")
            return False
        if not provided_token:
            logger.warning("HTTP auth denied: no credential presented")
            return False
        # Compare as BYTES. http.server decodes request headers as latin-1, so a credential containing any
        # byte >= 0x80 yields a non-ASCII str and hmac.compare_digest(str, str) would raise TypeError —
        # turning a bad credential into a 500 instead of a clean 401. Encoding both sides makes the
        # comparison total: a non-matching credential simply fails, and never raises.
        try:
            provided_bytes = provided_token.encode("latin-1")
        except UnicodeEncodeError:  # pragma: no cover — defensive; header text is latin-1 by construction
            provided_bytes = provided_token.encode("utf-8", "replace")
        if not hmac.compare_digest(provided_bytes, HTTP_AUTH_TOKEN.encode("utf-8")):
            # Never log the presented or expected value — only that a rejection happened, so that the
            # post-rotation proof ("no auth errors after the window") is actually observable.
            logger.warning("HTTP auth denied: credential mismatch")
            return False
        return True

    def do_GET(self):
        """Handle GET requests."""
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)

            # Token validation
            if not self._validate_token():
                self._send_json_response({"ok": False, "error": "unauthorized"}, 401)
                return

            if path == "/mt5/snapshots/rates":
                self._handle_rates_request(params)
            elif path == "/mt5/snapshots/deals":
                username = params.get("username", [""])[0]
                result = fetch_deals_snapshot(username)
                self._send_json_response(result, 200 if result.get("ok") else 400)
            elif path == "/mt5/symbols":
                result = self._handle_symbols_request()
                self._send_json_response(result, 200 if result.get("ok") else 400)
            elif path == "/mt5/positions":
                symbol = params.get("symbol", [""])[0]
                result = fetch_positions(symbol)
                self._send_json_response(result, 200 if result.get("ok") else 400)
            elif path == "/health":
                self._send_json_response({"ok": True, "status": "healthy"})
            else:
                self._send_json_response({"ok": False, "error": "not_found"}, 404)

        except Exception as e:
            logger.exception(f"HTTP handler error: {e}")
            self._send_json_response({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        """Handle POST requests."""
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if not self._validate_token():
                self._send_json_response({"ok": False, "error": "unauthorized"}, 401)
                return

            if path == "/mt5/order":
                self._handle_order_request()
            elif path == "/mt5/order_check":
                self._handle_order_check_request()
            elif path == "/mt5/close-position":
                self._handle_close_position_request()
            elif path == "/mt5/modify-position":
                self._handle_modify_position_request()
            elif path == "/mt5/login-and-validate":
                self._handle_login_validate_request()
            else:
                self._send_json_response({"ok": False, "error": "not_found"}, 404)

        except Exception as e:
            logger.exception(f"HTTP POST handler error: {e}")
            self._send_json_response({"ok": False, "error": str(e)}, 500)

    def _handle_order_request(self):
        """Handle POST /mt5/order — execute a demo market order."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response({"ok": False, "error": "empty_body"}, 400)
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json_response({"ok": False, "error": "invalid_json"}, 400)
            return

        required = ["symbol", "side", "lots", "comment"]
        missing = [k for k in required if k not in body]
        if missing:
            self._send_json_response({"ok": False, "error": "missing_fields", "detail": missing}, 400)
            return

        result = execute_demo_order(body)
        status_code = 200 if result.get("ok") else 400
        self._send_json_response(result, status_code)

    def _handle_order_check_request(self):
        """Handle POST /mt5/order_check — SHADOW dry-run (order_check, no order_send)."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response({"ok": False, "error": "empty_body"}, 400)
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json_response({"ok": False, "error": "invalid_json"}, 400)
            return

        required = ["symbol", "side", "lots", "comment"]
        missing = [k for k in required if k not in body]
        if missing:
            self._send_json_response({"ok": False, "error": "missing_fields", "detail": missing}, 400)
            return

        result = shadow_order_check(body)
        status_code = 200 if result.get("ok") else 400
        self._send_json_response(result, status_code)

    def _handle_close_position_request(self):
        """Handle POST /mt5/close-position — close a position by ticket."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response({"ok": False, "error": "empty_body"}, 400)
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json_response({"ok": False, "error": "invalid_json"}, 400)
            return

        ticket = body.get("ticket")
        if not ticket:
            self._send_json_response({"ok": False, "error": "missing_ticket"}, 400)
            return

        identity = {k: body.get(k) for k in ("require_identity_pin", "expected_login", "expected_server")}
        result = close_position(int(ticket), identity=identity)
        status_code = 200 if result.get("ok") else 400
        self._send_json_response(result, status_code)

    def _handle_modify_position_request(self):
        """Handle POST /mt5/modify-position — move an OPEN position's SL (breakeven). Demo only."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response({"ok": False, "error": "empty_body"}, 400)
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json_response({"ok": False, "error": "invalid_json"}, 400)
            return

        ticket = body.get("ticket")
        sl = body.get("sl")
        if ticket is None or sl is None:
            self._send_json_response({"ok": False, "error": "missing_fields", "detail": ["ticket", "sl"]}, 400)
            return

        tp = body.get("tp")
        identity = {k: body.get(k) for k in ("require_identity_pin", "expected_login", "expected_server")}
        result = modify_position(int(ticket), float(sl), None if tp is None else float(tp), identity=identity)
        status_code = 200 if result.get("ok") else 400
        self._send_json_response(result, status_code)

    def _handle_login_validate_request(self):
        """Handle POST /mt5/login-and-validate — validate MT5 credentials."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json_response({"ok": False, "valid": False, "reason": "empty_body"}, 400)
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json_response({"ok": False, "valid": False, "reason": "invalid_json"}, 400)
            return

        required = ["login", "password", "server"]
        missing = [k for k in required if not body.get(k)]
        if missing:
            self._send_json_response(
                {"ok": False, "valid": False, "reason": "missing_fields", "detail": missing}, 400)
            return

        result = login_and_validate(body)
        status_code = 200 if result.get("ok") else 400
        self._send_json_response(result, status_code)

    def _handle_symbols_request(self) -> Dict:
        """GET /mt5/symbols — list all available MT5 symbols with metadata."""
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return {"ok": False, "error": "mt5_not_installed"}

        init_kwargs = {}
        if MT5_TERMINAL_PATH:
            init_kwargs["path"] = MT5_TERMINAL_PATH

        if not guarded_initialize(mt5, init_kwargs):
            return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

        try:
            symbols = mt5.symbols_get()
            if symbols is None:
                return {"ok": False, "error": "symbols_get_failed"}

            result = []
            for s in symbols:
                result.append({
                    "name": s.name,
                    "description": s.description,
                    "path": s.path,
                    "visible": s.visible,
                    "spread": s.spread,
                    "digits": s.digits,
                    "point": s.point,
                    "trade_mode": s.trade_mode,
                    "contract_size": s.trade_contract_size,
                    "tick_size": s.trade_tick_size,
                    "tick_value": s.trade_tick_value,
                    "volume_min": s.volume_min,
                    "volume_step": s.volume_step,
                    "volume_max": s.volume_max,
                    "currency_base": s.currency_base,
                    "currency_profit": s.currency_profit,
                    "currency_margin": s.currency_margin,
                })

            return {"ok": True, "count": len(result), "symbols": result}

        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            mt5.shutdown()

    def _handle_rates_request(self, params: Dict):
        """Handle /mt5/snapshots/rates endpoint."""
        # Extract parameters
        symbol = params.get("symbol", [""])[0]  # preserve case for index symbols
        timeframe = params.get("timeframe", ["H4"])[0].upper()
        count_str = params.get("count", ["300"])[0]
        start_pos_str = params.get("start_pos", ["0"])[0]

        # Validate required params
        if not symbol:
            self._send_json_response({"ok": False, "error": "symbol parameter required"}, 400)
            return

        try:
            count = int(count_str)
            start_pos = int(start_pos_str)
        except ValueError:
            self._send_json_response({"ok": False, "error": f"Invalid count/start_pos"}, 400)
            return

        # Fetch OHLC data
        result = fetch_ohlc_rates(symbol, timeframe, count, start_pos)

        if result.get("ok"):
            self._send_json_response(result)
        else:
            self._send_json_response(result, 400)


def start_http_server():
    """Start the HTTP server in a background thread."""
    try:
        server = HTTPServer(("0.0.0.0", HTTP_SERVER_PORT), OHLCRequestHandler)
        logger.info(f"HTTP server started on port {HTTP_SERVER_PORT}")
        server.serve_forever()
    except Exception as e:
        logger.exception(f"HTTP server error: {e}")


def main_loop() -> None:
    """Main polling loop."""
    logger.info("=" * 60)
    logger.info("GuvFX MT5 Signal Execution Bridge Starting")
    logger.info("=" * 60)
    logger.info(f"Safety rails active:")
    logger.info(f"  - Symbol validation: MT5-native (symbol_info/symbol_select; no static allowlist)")
    logger.info(f"  - Max lot size: {MAX_LOT_SIZE}")
    logger.info(f"  - Allowed sides: {ALLOWED_SIDES}")
    logger.info(f"  - Poll interval: {POLL_INTERVAL}s")
    logger.info(f"  - SL/TP required: Yes")
    logger.info(f"  - Auto-close: No (strategy trades stay open)")
    logger.info(f"HTTP server:")
    logger.info(f"  - Port: {HTTP_SERVER_PORT}")
    logger.info(f"  - OHLC endpoint: /mt5/snapshots/rates")
    # Never log a token value — only which variable supplied it. Startup aborts if neither is set, so there
    # is no "auth disabled" state to report.
    if AGENT_TOKEN:
        logger.info(f"  - HTTP auth: REQUIRED (GUVFX_AGENT_TOKEN)")
    else:
        logger.info(f"  - HTTP auth: REQUIRED (GUVFX_AGENT_TOKEN, no fallback)")
    logger.info("=" * 60)

    while True:
        try:
            job = fetch_next_job()

            if job:
                process_job(job)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            break
        except Exception as e:
            logger.exception(f"Unexpected error in main loop: {e}")
            time.sleep(RETRY_DELAY_SECONDS)


def main() -> int:
    """Entry point."""
    if not validate_config():
        logger.error("Configuration validation failed. Exiting.")
        return 1

    # Start HTTP server in background thread for OHLC data requests
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    logger.info(f"HTTP server thread started (port {HTTP_SERVER_PORT})")

    try:
        main_loop()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
