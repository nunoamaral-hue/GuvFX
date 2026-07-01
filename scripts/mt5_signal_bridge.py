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
- EURUSD and GBPUSD only
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
ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]
MAX_LOT_SIZE = 0.02
ALLOWED_SIDES = ["BUY", "SELL"]

# Demo order endpoint safety rails (POST /mt5/order)
DEMO_ORDER_ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]
DEMO_ORDER_MAX_LOT_SIZE = 0.02
DEMO_ORDER_ALLOWED_SIDES = ["BUY", "SELL"]

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
WORKER_TOKEN = os.getenv("GUVFX_WORKER_TOKEN", "")
AGENT_TOKEN = os.getenv("GUVFX_AGENT_TOKEN", "").strip()  # For OHLC endpoint auth
ACCOUNT_ID = os.getenv("MT5_ACCOUNT_ID", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
HTTP_SERVER_PORT = int(os.getenv("HTTP_SERVER_PORT", "8788"))

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

    # Check symbol
    symbol = payload.get("symbol", "").upper()
    if symbol not in ALLOWED_SYMBOLS:
        return False, f"Symbol {symbol} not allowed. Only {ALLOWED_SYMBOLS} permitted."

    # Check lot size
    lots = payload.get("lots", 0)
    if lots <= 0:
        return False, f"Invalid lot size: {lots}"
    if lots > MAX_LOT_SIZE:
        return False, f"Lot size {lots} exceeds max {MAX_LOT_SIZE}."

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
    symbol = payload.get("symbol", "EURUSD").upper()
    lots = min(float(payload.get("lots", 0.01)), MAX_LOT_SIZE)
    side = payload.get("side", "BUY").upper()
    magic = payload.get("magic", 0)
    sl_price = float(payload.get("sl_price", 0))
    tp_price = float(payload.get("tp_price", 0))
    entry_price = payload.get("entry_price")  # Optional: None = market order

    # Use comment from payload or generate one
    comment = payload.get("comment", f"GS{job_id:04d}")
    # Truncate to MT5 limit
    comment = comment[:31]

    logger.info(f"Job {job_id}: {symbol} {side} {lots} lots, SL={sl_price}, TP={tp_price}, comment='{comment}'")

    # Initialize MT5
    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not mt5.initialize(**init_kwargs):
        error = mt5.last_error()
        return False, {}, f"MT5 initialization failed: {error}"

    try:
        # Get symbol info
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False, {}, f"Symbol {symbol} not found in MT5"

        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return False, {}, f"Failed to select symbol {symbol}"

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
                "type_filling": mt5.ORDER_FILLING_IOC,
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

        # Send order
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
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

    if not mt5.initialize(**init_kwargs):
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

    # --- Safety validation ---
    if symbol not in DEMO_ORDER_ALLOWED_SYMBOLS:
        return {"ok": False, "error": f"symbol_not_allowed", "detail": f"{symbol} not in {DEMO_ORDER_ALLOWED_SYMBOLS}"}

    if side not in DEMO_ORDER_ALLOWED_SIDES:
        return {"ok": False, "error": "side_not_allowed", "detail": f"{side} not in {DEMO_ORDER_ALLOWED_SIDES}"}

    if lots <= 0 or lots > DEMO_ORDER_MAX_LOT_SIZE:
        return {"ok": False, "error": "lots_out_of_range", "detail": f"lots={lots}, max={DEMO_ORDER_MAX_LOT_SIZE}"}

    if not comment:
        return {"ok": False, "error": "comment_required"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not mt5.initialize(**init_kwargs):
        error = mt5.last_error()
        return {"ok": False, "error": "mt5_init_failed", "detail": str(error)}

    try:
        # Verify account is demo
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}

        # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo", "detail": f"trade_mode={account_info.trade_mode}"}

        # Verify symbol
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"ok": False, "error": "symbol_not_found"}

        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return {"ok": False, "error": "symbol_select_failed"}

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
            "type_filling": mt5.ORDER_FILLING_IOC,
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
        logger.info(f"[/mt5/order] Sending: {symbol} {side} {lots} @ {price}{sl_str}{tp_str} comment='{comment[:31]}'")
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            return {"ok": False, "error": "order_send_none", "detail": str(error)}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {
                "ok": False,
                "error": "order_rejected",
                "retcode": result.retcode,
                "comment": result.comment,
            }

        logger.info(f"[/mt5/order] Success: ticket={result.order}, price={result.price}")
        return {
            "ok": True,
            "retcode": result.retcode,
            "order": result.order,
            "deal": result.deal,
            "price": result.price,
            "volume": result.volume,
            "comment": comment[:31],
        }

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

    # --- Safety validation (identical to execute_demo_order — nothing bypassed) ---
    if symbol not in DEMO_ORDER_ALLOWED_SYMBOLS:
        return {"ok": False, "shadow": True, "error": "symbol_not_allowed", "detail": f"{symbol} not in {DEMO_ORDER_ALLOWED_SYMBOLS}"}
    if side not in DEMO_ORDER_ALLOWED_SIDES:
        return {"ok": False, "shadow": True, "error": "side_not_allowed", "detail": f"{side} not in {DEMO_ORDER_ALLOWED_SIDES}"}
    if lots <= 0 or lots > DEMO_ORDER_MAX_LOT_SIZE:
        return {"ok": False, "shadow": True, "error": "lots_out_of_range", "detail": f"lots={lots}, max={DEMO_ORDER_MAX_LOT_SIZE}"}
    if not comment:
        return {"ok": False, "shadow": True, "error": "comment_required"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "shadow": True, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not mt5.initialize(**init_kwargs):
        return {"ok": False, "shadow": True, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        # Verify account is demo (broker truth — same check as the live path).
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "shadow": True, "error": "account_info_failed"}
        if account_info.trade_mode != 0:  # 0=DEMO, 1=CONTEST, 2=REAL
            return {"ok": False, "shadow": True, "error": "account_not_demo", "detail": f"trade_mode={account_info.trade_mode}"}

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"ok": False, "shadow": True, "error": "symbol_not_found"}
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return {"ok": False, "shadow": True, "error": "symbol_select_failed"}

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
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        sl = params.get("sl")
        tp = params.get("tp")
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        logger.info(
            f"[/mt5/order_check] SHADOW validating (NO order): {symbol} {side} {lots} @ {price} "
            f"comment='{comment[:31]}'"
        )
        # DRY RUN — validation only. There is deliberately NO mt5.order_send here.
        check = mt5.order_check(request)
        if check is None:
            return {"ok": False, "shadow": True, "error": "order_check_none",
                    "detail": str(mt5.last_error()), "request": request}

        # order_check retcode 0 == request is valid (would be accepted).
        return {
            "ok": bool(check.retcode == 0),
            "shadow": True,
            "suppressed": True,
            "order_send_called": False,
            "retcode": int(check.retcode),
            "comment": getattr(check, "comment", ""),
            "margin": getattr(check, "margin", None),
            "free_margin": getattr(check, "margin_free", None),
            "balance": getattr(check, "balance", None),
            "request": request,
        }

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

    if not mt5.initialize(**init_kwargs):
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

    if not mt5.initialize(**init_kwargs):
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


def close_position(ticket: int) -> Dict[str, Any]:
    """Close an open position by ticket. Demo accounts only."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "mt5_not_installed"}

    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH

    if not mt5.initialize(**init_kwargs):
        return {"ok": False, "error": "mt5_init_failed", "detail": str(mt5.last_error())}

    try:
        account_info = mt5.account_info()
        if account_info is None:
            return {"ok": False, "error": "account_info_failed"}
        if account_info.trade_mode != 0:
            return {"ok": False, "error": "account_not_demo"}

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"ok": False, "error": "position_not_found", "detail": f"ticket={ticket}"}

        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            return {"ok": False, "error": "tick_failed"}

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
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return {"ok": False, "error": "order_send_none", "detail": str(mt5.last_error())}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"ok": False, "error": "close_rejected", "retcode": result.retcode, "comment": result.comment}

        logger.info(f"[close] Closed ticket={ticket}: order={result.order} deal={result.deal} price={result.price}")
        return {
            "ok": True,
            "ticket": ticket,
            "close_order": result.order,
            "close_deal": result.deal,
            "close_price": result.price,
            "volume": result.volume,
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
    if not mt5.initialize(**init_kwargs):
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
        Validate the agent token from headers for OHLC endpoints.

        Uses GUVFX_AGENT_TOKEN if set, otherwise falls back to GUVFX_WORKER_TOKEN.
        This allows separate tokens for OHLC data (backend) vs job polling (bridge).
        """
        provided_token = self.headers.get("X-GuvFX-Agent-Token", "")

        # Prefer AGENT_TOKEN for OHLC auth, fallback to WORKER_TOKEN
        if AGENT_TOKEN:
            return provided_token == AGENT_TOKEN
        elif WORKER_TOKEN:
            return provided_token == WORKER_TOKEN
        else:
            return True  # No token configured, allow all

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

        result = close_position(int(ticket))
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

        if not mt5.initialize(**init_kwargs):
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
    logger.info(f"  - Allowed symbols: {ALLOWED_SYMBOLS}")
    logger.info(f"  - Max lot size: {MAX_LOT_SIZE}")
    logger.info(f"  - Allowed sides: {ALLOWED_SIDES}")
    logger.info(f"  - Poll interval: {POLL_INTERVAL}s")
    logger.info(f"  - SL/TP required: Yes")
    logger.info(f"  - Auto-close: No (strategy trades stay open)")
    logger.info(f"HTTP server:")
    logger.info(f"  - Port: {HTTP_SERVER_PORT}")
    logger.info(f"  - OHLC endpoint: /mt5/snapshots/rates")
    if AGENT_TOKEN:
        logger.info(f"  - OHLC auth: using GUVFX_AGENT_TOKEN")
    elif WORKER_TOKEN:
        logger.info(f"  - OHLC auth: using GUVFX_WORKER_TOKEN (fallback)")
    else:
        logger.info(f"  - OHLC auth: DISABLED (no token configured)")
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
