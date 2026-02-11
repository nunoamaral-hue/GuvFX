#!/usr/bin/env python3
"""
GuvFX MT5 Demo Trade Bridge

A safety-first execution bridge that:
1. Polls the GuvFX API for pending PLACE_TEST_ORDER jobs
2. Executes them via MetaTrader5 Python package
3. Reports results back to the API

SAFETY RAILS (hard-coded, cannot be bypassed):
- Demo accounts only
- EURUSD only
- 0.01 lot only
- BUY market orders only
- Max 3 trades per day per account (enforced by API)

REQUIREMENTS:
- Python 3.8+
- MetaTrader5 package: pip install MetaTrader5
- requests package: pip install requests
- MT5 terminal running with Algo Trading enabled
- Demo account credentials configured

ENVIRONMENT VARIABLES (required):
- GUVFX_API_URL: API base URL (e.g., https://api.guvfx.com)
- GUVFX_WORKER_TOKEN: Worker authentication token (matches MT5_WORKER_TOKEN on server)
- MT5_ACCOUNT_ID: TradingAccount ID to poll jobs for

OPTIONAL:
- MT5_TERMINAL_PATH: Path to MT5 terminal (if non-standard location)
- POLL_INTERVAL_SECONDS: Polling interval (default: 2)

USAGE:
    python mt5_demo_bridge.py

TO RUN AS WINDOWS SERVICE:
    pip install pywin32
    python mt5_demo_bridge.py --install-service

Or use NSSM (Non-Sucking Service Manager):
    nssm install GuvFXBridge "C:\\Python312\\python.exe" "C:\\path\\to\\mt5_demo_bridge.py"
    nssm start GuvFXBridge
"""

import os
import sys
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mt5_demo_bridge.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# =============================================================================
# HARD-CODED SAFETY RAILS (DO NOT MODIFY)
# =============================================================================
ALLOWED_SYMBOLS = ["EURUSD"]
FIXED_LOT_SIZE = 0.01
ALLOWED_SIDES = ["BUY"]  # Only BUY for demo
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# =============================================================================
# Configuration
# =============================================================================
API_URL = os.getenv("GUVFX_API_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("GUVFX_WORKER_TOKEN", "")
ACCOUNT_ID = os.getenv("MT5_ACCOUNT_ID", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))


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
    """Get HTTP headers for API requests."""
    return {
        "X-Worker-Token": WORKER_TOKEN,
        "Content-Type": "application/json",
    }


def fetch_next_job() -> Optional[Dict[str, Any]]:
    """
    Fetch the next pending PLACE_TEST_ORDER job for our account from the API.
    Returns job data or None if no jobs available.

    Note: job_type=PLACE_TEST_ORDER is explicitly requested to prevent Linux
    ingest workers from claiming demo trade jobs (they only handle SYNC_POSITIONS).
    """
    try:
        url = f"{API_URL}/api/execution/jobs/next/"
        params = {
            "worker_id": f"mt5-bridge-{ACCOUNT_ID}",
            "account_id": ACCOUNT_ID,
            "job_type": "PLACE_TEST_ORDER",
        }

        response = requests.get(url, headers=get_headers(), params=params, timeout=10)

        if response.status_code == 204:
            # No jobs available
            return None

        if response.status_code == 200:
            job = response.json()
            logger.info(f"Claimed job {job.get('id')}: {job.get('job_type')}")
            return job

        logger.warning(f"Unexpected response fetching jobs: {response.status_code} - {response.text}")
        return None

    except requests.RequestException as e:
        logger.error(f"Error fetching jobs: {e}")
        return None


def complete_job(job_id: int, success: bool, result: Dict = None, error_message: str = "") -> bool:
    """
    Report job completion to the API.
    """
    try:
        url = f"{API_URL}/api/execution/jobs/{job_id}/complete/"
        data = {
            "status": "SUCCESS" if success else "FAILED",
            "result": result or {},
            "error_message": error_message,
        }

        response = requests.post(url, headers=get_headers(), json=data, timeout=10)

        if response.status_code == 200:
            logger.info(f"Job {job_id} completed: {'SUCCESS' if success else 'FAILED'}")
            return True

        logger.warning(f"Error completing job {job_id}: {response.status_code} - {response.text}")
        return False

    except requests.RequestException as e:
        logger.error(f"Error completing job {job_id}: {e}")
        return False


def validate_job_safety(job: Dict) -> tuple[bool, str]:
    """
    Validate job against safety rails.
    Returns (is_safe, error_message).
    """
    payload = job.get("payload", {})

    # Check job type
    if job.get("job_type") != "PLACE_TEST_ORDER":
        return False, f"Invalid job type: {job.get('job_type')}"

    # Check symbol
    symbol = payload.get("symbol", "").upper()
    if symbol not in ALLOWED_SYMBOLS:
        return False, f"Symbol {symbol} not allowed. Only {ALLOWED_SYMBOLS} permitted."

    # Check lot size
    lots = payload.get("lots", 0)
    if lots != FIXED_LOT_SIZE:
        return False, f"Lot size {lots} not allowed. Fixed at {FIXED_LOT_SIZE}."

    # Check side
    side = payload.get("side", "").upper()
    if side not in ALLOWED_SIDES:
        return False, f"Side {side} not allowed. Only {ALLOWED_SIDES} permitted."

    # Check demo flag
    if not payload.get("is_demo", False):
        return False, "Job not marked as demo. Refusing to execute."

    return True, ""


def execute_mt5_trade(job: Dict) -> tuple[bool, Dict, str]:
    """
    Execute the trade via MetaTrader5.
    Returns (success, result_dict, error_message).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return False, {}, "MetaTrader5 package not installed. Run: pip install MetaTrader5"

    payload = job.get("payload", {})
    symbol = payload.get("symbol", "EURUSD")
    lots = FIXED_LOT_SIZE  # Always use fixed lot size
    comment = payload.get("comment", f"GUVFX_DEMO_JOB:{job.get('id')}")
    magic = payload.get("magic", 0)

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

        price = tick.ask  # BUY at ask price

        # Prepare order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,  # 2 pips slippage allowed
            "magic": magic,
            "comment": comment[:31],  # MT5 comment limit is 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(f"Sending order: {symbol} BUY {lots} @ {price}")

        # Send order
        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            return False, {}, f"Order send returned None: {error}"

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, {}, f"Order failed: retcode={result.retcode}, comment={result.comment}"

        # Success!
        result_dict = {
            "ticket": result.order,
            "price": result.price,
            "volume": result.volume,
            "symbol": symbol,
            "order_type": "BUY",
            "placed_at": datetime.utcnow().isoformat() + "Z",
            "comment": comment,
            "retcode": result.retcode,
        }

        logger.info(f"Order executed: ticket={result.order}, price={result.price}")
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
    logger.info("GuvFX MT5 Demo Bridge Starting")
    logger.info("=" * 60)
    logger.info(f"Safety rails active:")
    logger.info(f"  - Allowed symbols: {ALLOWED_SYMBOLS}")
    logger.info(f"  - Fixed lot size: {FIXED_LOT_SIZE}")
    logger.info(f"  - Allowed sides: {ALLOWED_SIDES}")
    logger.info(f"  - Poll interval: {POLL_INTERVAL}s")
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
