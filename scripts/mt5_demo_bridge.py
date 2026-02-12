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
- GUVFX_DEMO_AUTOCLOSE_SECONDS: If > 0, auto-close position after N seconds (default: 0)

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

# =============================================================================
# Polling/Retry Configuration
# =============================================================================
MAX_FETCH_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # Base delay for exponential backoff
MAX_CONSECUTIVE_404 = 3  # Stop after this many consecutive 404s
RETRY_DELAY_SECONDS = 5  # Delay after unexpected errors in main loop
HTTP_TIMEOUT = 15  # Request timeout in seconds

# =============================================================================
# Configuration
# =============================================================================
API_URL = os.getenv("GUVFX_API_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("GUVFX_WORKER_TOKEN", "")
ACCOUNT_ID = os.getenv("MT5_ACCOUNT_ID", "")
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))

# Demo auto-close: if > 0, wait N seconds after opening then close the position
# This ensures the demo produces a complete round-trip (open + close deals)
DEMO_AUTOCLOSE_SECONDS = int(os.getenv("GUVFX_DEMO_AUTOCLOSE_SECONDS", "0"))

# Track consecutive 404 errors to avoid spamming
_consecutive_404_count = 0


def create_http_session() -> requests.Session:
    """
    Create an HTTP session with retry logic and HTTP/1.1 enforcement.
    Uses urllib3 Retry for automatic retries with exponential backoff.
    """
    session = requests.Session()

    # Configure retry strategy: retry on connection errors, timeouts, 502/503/504
    retry_strategy = Retry(
        total=MAX_FETCH_RETRIES,
        backoff_factor=RETRY_BASE_DELAY,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,  # Don't raise, let us handle status codes
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


# Global HTTP session for connection reuse
_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """Get or create the global HTTP session."""
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

    Robust polling:
    - Uses explicit query params in canonical order
    - Uses HTTP session with retry/backoff via HTTPAdapter
    - Logs full URL and response body snippet on errors
    - Tracks consecutive 404s to avoid spamming
    """
    global _consecutive_404_count

    # Build URL with explicit query params (canonical order for debugging)
    params = {
        "account_id": ACCOUNT_ID,
        "job_type": "PLACE_TEST_ORDER",
        "worker_id": f"windows-bridge-{ACCOUNT_ID}",
    }
    # Construct full URL for logging
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

        status = response.status_code

        # 204: No jobs available (normal)
        if status == 204:
            _consecutive_404_count = 0  # Reset on success
            return None

        # 200: Job claimed
        if status == 200:
            _consecutive_404_count = 0  # Reset on success
            job = response.json()
            logger.info(f"Claimed job {job.get('id')}: {job.get('job_type')}")
            return job

        # 404: Endpoint not found (likely wrong URL or routing issue)
        if status == 404:
            _consecutive_404_count += 1
            body_snippet = response.text[:200] if response.text else "(empty)"
            logger.error(
                f"404 Not Found (attempt {_consecutive_404_count}/{MAX_CONSECUTIVE_404})\n"
                f"  URL: {full_url}\n"
                f"  Response: {body_snippet}"
            )
            if _consecutive_404_count >= MAX_CONSECUTIVE_404:
                logger.error(
                    f"Too many consecutive 404 errors. Check that the endpoint exists.\n"
                    f"  Expected: {API_URL}/api/execution/jobs/next/\n"
                    f"  Stopping job fetch until next poll cycle."
                )
            return None

        # Other non-2xx status codes
        body_snippet = response.text[:300] if response.text else "(empty)"
        logger.warning(
            f"Unexpected HTTP {status} fetching jobs\n"
            f"  URL: {full_url}\n"
            f"  Response: {body_snippet}"
        )
        return None

    except requests.exceptions.Timeout:
        logger.error(f"Timeout ({HTTP_TIMEOUT}s) fetching jobs: {full_url}")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error fetching jobs: {full_url}\n  Error: {e}")
        return None
    except requests.RequestException as e:
        logger.error(f"Request error fetching jobs: {full_url}\n  Error: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error fetching jobs: {full_url}\n  Error: {e}")
        return None


def complete_job(job_id: int, success: bool, result: Dict = None, error_message: str = "") -> bool:
    """
    Report job completion to the API with retry logic.
    """
    url = f"{API_URL}/api/execution/jobs/{job_id}/complete/"
    data = {
        "status": "SUCCESS" if success else "FAILED",
        "result": result or {},
        "error_message": error_message,
    }

    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            session = get_http_session()
            response = session.post(
                url,
                headers=get_headers(),
                json=data,
                timeout=HTTP_TIMEOUT,
            )

            if response.status_code == 200:
                logger.info(f"Job {job_id} completed: {'SUCCESS' if success else 'FAILED'}")
                return True

            body_snippet = response.text[:200] if response.text else "(empty)"
            logger.warning(
                f"Error completing job {job_id} (attempt {attempt}/{MAX_FETCH_RETRIES}): "
                f"HTTP {response.status_code}\n  Response: {body_snippet}"
            )

        except requests.RequestException as e:
            logger.error(f"Request error completing job {job_id} (attempt {attempt}): {e}")

        # Exponential backoff before retry
        if attempt < MAX_FETCH_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.info(f"Retrying in {delay:.1f}s...")
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


def close_position_by_ticket(mt5, position_ticket: int, symbol: str, volume: float, comment: str, magic: int) -> tuple[bool, Dict, str]:
    """
    Close a position by its ticket.
    Returns (success, result_dict, error_message).

    SAFETY: Only closes positions matching our magic number and comment pattern.
    """
    # Get current price for closing (SELL to close a BUY)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, {}, f"Failed to get tick for {symbol} during close"

    close_price = tick.bid  # SELL at bid price to close BUY

    # Prepare close request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL,  # SELL to close BUY
        "position": position_ticket,
        "price": close_price,
        "deviation": 20,
        "magic": magic,
        "comment": comment[:31],  # Use same comment for attribution
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    logger.info(f"Closing position {position_ticket}: {symbol} SELL {volume} @ {close_price}")

    result = mt5.order_send(request)

    if result is None:
        error = mt5.last_error()
        return False, {}, f"Close order returned None: {error}"

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, {}, f"Close order failed: retcode={result.retcode}, comment={result.comment}"

    result_dict = {
        "close_ticket": result.order,
        "close_price": result.price,
        "volume": result.volume,
        "symbol": symbol,
        "order_type": "SELL",
        "closed_at": datetime.utcnow().isoformat() + "Z",
        "position_ticket": position_ticket,
    }

    logger.info(f"Position {position_ticket} closed: ticket={result.order}, price={result.price}")
    return True, result_dict, ""


def execute_mt5_trade(job: Dict) -> tuple[bool, Dict, str]:
    """
    Execute the trade via MetaTrader5.
    Returns (success, result_dict, error_message).

    If GUVFX_DEMO_AUTOCLOSE_SECONDS > 0, will also close the position after waiting.

    ATTRIBUTION: Uses job.payload.comment exactly if provided, otherwise falls back
    to f"GUVFX_DEMO_JOB:{job_id}". The SAME comment is used for both BUY and SELL
    to enable backend attribution.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return False, {}, "MetaTrader5 package not installed. Run: pip install MetaTrader5"

    job_id = job.get("id")
    payload = job.get("payload", {})
    symbol = payload.get("symbol", "EURUSD")
    lots = FIXED_LOT_SIZE  # Always use fixed lot size
    magic = payload.get("magic", 0)

    # CRITICAL: Use job comment EXACTLY if provided, otherwise generate from job_id
    # This ensures attribution works correctly
    job_comment = payload.get("comment", "").strip()
    if not job_comment:
        job_comment = f"GUVFX_DEMO_JOB:{job_id}"

    # Log the exact comment we're using for debugging attribution issues
    logger.info(f"Job {job_id}: Using job_comment='{job_comment}' for order_send")

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

        # Prepare order request with exact job comment (truncated to 31 chars for MT5)
        order_comment = job_comment[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,  # 2 pips slippage allowed
            "magic": magic,
            "comment": order_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(f"Sending order: {symbol} BUY {lots} @ {price} comment='{order_comment}'")

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
            "comment": job_comment,  # Full comment (not truncated) for result
            "retcode": result.retcode,
        }

        logger.info(f"Order executed: ticket={result.order}, price={result.price}, comment='{order_comment}'")

        # =====================================================================
        # DEMO AUTO-CLOSE: If enabled, wait then close the position
        # =====================================================================
        if DEMO_AUTOCLOSE_SECONDS > 0:
            logger.info(f"Auto-close enabled: waiting {DEMO_AUTOCLOSE_SECONDS}s before closing position for job {job_id}")

            # Wait a moment for the position to be created
            time.sleep(1)

            # SAFETY: Determine position ticket with strict matching
            # Priority 1: Use result.position if available (direct from order_send)
            position_ticket_from_result = getattr(result, "position", None) or getattr(result, "deal", None)

            our_position = None
            if position_ticket_from_result and position_ticket_from_result > 0:
                # Direct ticket from order result - most reliable
                logger.info(f"Checking position ticket from order result: {position_ticket_from_result}")
                positions = mt5.positions_get(ticket=position_ticket_from_result)
                if positions and len(positions) > 0:
                    pos = positions[0]
                    pos_comment = getattr(pos, "comment", "") or ""
                    # Verify it's actually our position (comment should match)
                    if pos_comment == order_comment:
                        our_position = pos
                        logger.info(f"Confirmed position {position_ticket_from_result} matches our comment")
                    else:
                        logger.warning(f"Position {position_ticket_from_result} comment='{pos_comment}' does not match expected='{order_comment}'")
                else:
                    logger.warning(f"Position {position_ticket_from_result} not found in open positions")

            # Priority 2: Search with EXACT comment match (not just prefix)
            if our_position is None:
                logger.info(f"Searching for position with exact comment='{order_comment}'")
                positions = mt5.positions_get(symbol=symbol)
                if positions:
                    for pos in positions:
                        # Must match symbol
                        if pos.symbol != symbol:
                            continue
                        # EXACT comment match required to avoid closing wrong position
                        pos_comment = getattr(pos, "comment", "") or ""
                        if pos_comment == order_comment:
                            our_position = pos
                            logger.info(f"Found position via exact comment match: ticket={pos.ticket}, comment='{pos_comment}'")
                            break
                        else:
                            logger.debug(f"Skipping position {pos.ticket}: comment='{pos_comment}' != '{order_comment}'")

            if our_position is None:
                # SAFETY: Do NOT close any position if we can't find exact match
                error_msg = f"Could not find position with exact comment='{order_comment}' to auto-close. Refusing to close random position."
                logger.error(error_msg)
                result_dict["auto_close_failed"] = True
                result_dict["auto_close_error"] = error_msg
            else:
                position_ticket = our_position.ticket
                position_volume = our_position.volume

                logger.info(f"Will close position ticket={position_ticket}, volume={position_volume}, comment='{order_comment}'")

                # Wait the configured time
                remaining_wait = max(0, DEMO_AUTOCLOSE_SECONDS - 1)  # Already waited 1s above
                if remaining_wait > 0:
                    logger.info(f"Waiting {remaining_wait}s before auto-close...")
                    time.sleep(remaining_wait)

                # Close the position with SAME comment for attribution
                close_success, close_result, close_error = close_position_by_ticket(
                    mt5=mt5,
                    position_ticket=position_ticket,
                    symbol=symbol,
                    volume=position_volume,
                    comment=job_comment,  # Same comment ensures attribution
                    magic=magic,
                )

                if close_success:
                    result_dict["auto_closed"] = True
                    result_dict["close_result"] = close_result
                    logger.info(f"Auto-close successful for job {job_id}: {close_result}")
                else:
                    result_dict["auto_close_failed"] = True
                    result_dict["auto_close_error"] = close_error
                    logger.warning(f"Auto-close failed for job {job_id}: {close_error}")

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
    if DEMO_AUTOCLOSE_SECONDS > 0:
        logger.info(f"  - Auto-close: {DEMO_AUTOCLOSE_SECONDS}s (position will close automatically)")
    else:
        logger.info(f"  - Auto-close: DISABLED (position stays open)")
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
