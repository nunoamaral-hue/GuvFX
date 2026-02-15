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

USAGE:
    python mt5_signal_bridge.py
"""

import os
import sys
import time
import logging
import random
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlencode

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
ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD"]
MAX_LOT_SIZE = 0.02
ALLOWED_SIDES = ["BUY", "SELL"]

# =============================================================================
# Polling/Retry Configuration
# =============================================================================
MAX_FETCH_RETRIES = 3
RETRY_BASE_DELAY = 2.0
MAX_CONSECUTIVE_404 = 3
RETRY_DELAY_SECONDS = 5
HTTP_TIMEOUT = 15

# =============================================================================
# Configuration
# =============================================================================
API_URL = os.getenv("GUVFX_API_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("GUVFX_WORKER_TOKEN", "")
ACCOUNT_ID = os.getenv("MT5_ACCOUNT_ID", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))

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

        # Prepare order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 20,  # 2 pips slippage
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(f"Sending order: {symbol} {side} {lots} @ {price}, SL={sl_price}, TP={tp_price}")

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
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "lots": lots,
                    "market_closed": True,
                }
                return False, market_closed_result, f"market_closed retcode={result.retcode}"

            return False, {}, f"Order failed: retcode={result.retcode}, comment={result.comment}"

        # Success!
        result_dict = {
            "ticket": result.order,
            "price": result.price,
            "volume": result.volume,
            "symbol": symbol,
            "order_type": side,
            "sl": sl_price,
            "tp": tp_price,
            "placed_at": datetime.utcnow().isoformat() + "Z",
            "comment": comment,
            "retcode": result.retcode,
        }

        logger.info(f"Order executed: ticket={result.order}, price={result.price}, SL={sl_price}, TP={tp_price}")

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

    try:
        main_loop()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
