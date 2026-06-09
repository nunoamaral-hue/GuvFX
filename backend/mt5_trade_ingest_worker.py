import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

# --- Django ORM init ---
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guvfx_backend.settings")
import django
django.setup()

from django.utils.dateparse import parse_datetime
from trading.models import TradingAccount, Trade

API_BASE = os.getenv("GUVFX_API_BASE", "http://guvfx-backend:8000")
API_HOST = os.getenv("GUVFX_API_HOST", "api.guvfx.com")
WORKER_ID = os.getenv("MT5_WORKER_ID", "mt5-trade-ingest-1")

WORKER_TOKEN = os.getenv("MT5_WORKER_TOKEN", "")
AGENT_BASE = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or os.getenv("GUVFX_AGENT_URL") or os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
AGENT_ORDER_BASE = AGENT_BASE
AGENT_TOKEN = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip().strip('"')

SLEEP_SEC = float(os.getenv("MT5_WORKER_SLEEP", "2.0"))

# Retry settings for auto-sync when expected deal not found
AUTO_SYNC_MAX_RETRIES = int(os.getenv("AUTO_SYNC_MAX_RETRIES", "5"))
AUTO_SYNC_RETRY_DELAY = float(os.getenv("AUTO_SYNC_RETRY_DELAY", "2.0"))

def api_headers():
    headers = {
        "Host": API_HOST,
        "X-Forwarded-Proto": "https",
        "X-Worker-Id": WORKER_ID,
        "X-Worker-Secret": WORKER_TOKEN,
    }
    if os.getenv("GUVFX_USE_LEGACY_AUTH"):
        headers = {
            "Host": API_HOST,
            "X-Forwarded-Proto": "https",
            "X-Worker-Token": WORKER_TOKEN,
        }
    return headers

def claim_next_job(job_type: str | None = None):
    params = f"worker_id={urllib.parse.quote(WORKER_ID)}"
    if job_type:
        params += f"&job_type={urllib.parse.quote(job_type)}"
    url = f"{API_BASE}/api/execution/jobs/next/?{params}"
    req = urllib.request.Request(url, method="GET", headers=api_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 204:
                return None
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return None
        raise

def complete_job(job_id: int, status: str, result: dict, error_message: str = ""):
    url = f"{API_BASE}/api/execution/jobs/{job_id}/complete/"
    payload = {"status": status, "result": result or {}, "error_message": error_message or ""}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={**api_headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8", "ignore") or "{}")

def agent_get(kind: str, username: str):
    url = f"{AGENT_BASE}/mt5/snapshots/{kind}?username={urllib.parse.quote(username)}"
    req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": AGENT_TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}

def agent_order(payload: dict) -> dict:
    """POST /mt5/order on the Windows agent (signal bridge port 8788)."""
    url = f"{AGENT_ORDER_BASE}/mt5/order"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"X-GuvFX-Agent-Token": AGENT_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {"ok": False, "error": "empty_response"}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:500]
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": f"http_{e.code}", "detail": body}
    except Exception as e:
        return {"ok": False, "error": "agent_unreachable", "detail": str(e)}

def to_dt(x):
    if not x:
        return None
    dt = parse_datetime(x)
    return dt

def to_dec(x, default="0"):
    if x is None:
        return Decimal(default)
    return Decimal(str(x))

def find_expected_tag_in_deals(deals: list[dict], expected_tag: str) -> bool:
    """Check if expected_tag exists in any deal's comment field."""
    if not expected_tag:
        return True  # No tag to find, consider it found
    for d in deals:
        comment = str(d.get("comment") or "").strip()
        if comment == expected_tag:
            return True
    return False


def get_expected_tag_from_trigger_job(trigger_job_id: int) -> str | None:
    """
    Get the expected comment tag from a trigger job (PLACE_ORDER).
    Returns the comment from payload or result, or None if not found.
    """
    from execution.models import ExecutionJob
    try:
        trigger_job = ExecutionJob.objects.get(id=trigger_job_id)
        # Try payload.comment first (where we set it)
        payload = trigger_job.payload or {}
        tag = payload.get("comment")
        if tag:
            return str(tag).strip()
        # Fallback to result.comment (if MT5 modified it)
        result = trigger_job.result or {}
        tag = result.get("comment")
        if tag:
            return str(tag).strip()
        return None
    except ExecutionJob.DoesNotExist:
        return None
    except Exception:
        return None


import re
import datetime as dt_module
from execution.models import ExecutionJob

# Regex for GJ/GS tags
_GJ_TAG_RE = re.compile(r"^GJ\d{4}$")
_GS_TAG_RE = re.compile(r"^GS(\d{4})$")


def _worker_infer_source_stage(comment: str) -> str:
    """Infer Trade.source_stage from comment tag in ingest worker."""
    if not comment:
        return "UNKNOWN"
    if _GJ_TAG_RE.match(comment):
        return "TEST"
    gs_match = _GS_TAG_RE.match(comment)
    if gs_match:
        job_id = int(gs_match.group(1))
        try:
            job = ExecutionJob.objects.get(id=job_id)
            payload = job.payload or {}
            stage = payload.get("assignment_stage")
            if stage in ("TEST", "LIVE"):
                return stage
            if payload.get("signal_reason") == "forced_once_test":
                return "TEST"
            if payload.get("signal_reason") == "trendline_break_pocket_signal":
                return "LIVE"
        except Exception:
            pass
    return "UNKNOWN"


def _deal_time_to_utc(d: dict):
    """Extract deal.time (unix seconds) as aware UTC datetime."""
    raw = d.get("time")
    if raw and isinstance(raw, (int, float)):
        try:
            return dt_module.datetime.utcfromtimestamp(raw).replace(tzinfo=dt_module.timezone.utc)
        except (ValueError, OSError):
            pass
    return None


def upsert_trades(account: TradingAccount, deals: list[dict]):
    inserted = 0
    updated = 0
    skipped = 0

    cutover = account.ingest_cutover_time

    for d in deals:
        ticket = str(d.get("ticket") or d.get("position_ticket") or d.get("deal_id") or "").strip()
        if not ticket:
            continue

        # Cutover check (deal.time is unix seconds)
        deal_time_utc = _deal_time_to_utc(d)
        if cutover and deal_time_utc and deal_time_utc < cutover:
            skipped += 1
            continue

        symbol = (d.get("symbol") or "").strip()
        side = (d.get("side") or d.get("type") or "").strip().upper() or "BUY"
        vol = to_dec(d.get("volume") or d.get("lots") or "0")

        open_time = to_dt(d.get("open_time_utc") or d.get("open_time") or d.get("t_open_utc"))
        close_time = to_dt(d.get("close_time_utc") or d.get("close_time") or d.get("t_close_utc") or d.get("time_utc"))

        open_price = to_dec(d.get("open_price") or "0")
        close_price = d.get("close_price")
        close_price = to_dec(close_price, "0") if close_price is not None else None

        profit = to_dec(d.get("profit") or d.get("pnl") or "0")
        commission = to_dec(d.get("commission") or "0")
        swap = to_dec(d.get("swap") or "0")

        magic = d.get("magic") if d.get("magic") is not None else d.get("magic_number")
        try:
            magic = int(magic) if magic is not None else None
        except Exception:
            magic = None

        comment = str(d.get("comment") or "").strip()
        source_stage = _worker_infer_source_stage(comment)

        obj, created = Trade.objects.get_or_create(
            account=account,
            ticket=ticket,
            defaults={
                "symbol": symbol,
                "side": side if side in ("BUY","SELL") else "BUY",
                "volume": vol,
                "open_time": open_time or close_time,
                "close_time": close_time,
                "open_price": open_price,
                "close_price": close_price,
                "profit": profit,
                "commission": commission,
                "swap": swap,
                "magic_number": magic,
                "comment": comment,
                "opened_by": "EA",
                "source_stage": source_stage,
            },
        )
        if created:
            inserted += 1
            continue

        # Update mutable fields (idempotent)
        changed = False
        for field, val in [
            ("close_time", close_time),
            ("close_price", close_price),
            ("profit", profit),
            ("commission", commission),
            ("swap", swap),
            ("magic_number", magic),
            ("comment", comment),
        ]:
            if val is not None and getattr(obj, field) != val:
                setattr(obj, field, val)
                changed = True

        # Update source_stage if UNKNOWN and we now have a valid one
        if obj.source_stage == "UNKNOWN" and source_stage != "UNKNOWN":
            obj.source_stage = source_stage
            changed = True

        if changed:
            obj.save()
            updated += 1

    return inserted, updated

def main():
    if not WORKER_TOKEN:
        raise SystemExit("MT5_WORKER_TOKEN missing")
    if not AGENT_BASE or not AGENT_TOKEN:
        raise SystemExit("GUVFX_AGENT_URL/TOKEN (or WINDOWS_AGENT_BASE/TOKEN) missing")

    print("worker:", WORKER_ID, "agent_order_base:", AGENT_ORDER_BASE)
    while True:
        try:
            # Claim priority: PLACE_TEST_ORDER > PLACE_ORDER > SYNC_POSITIONS
            job = (
                claim_next_job("PLACE_TEST_ORDER")
                or claim_next_job("PLACE_ORDER")
                or claim_next_job()
            )
            if not job:
                time.sleep(SLEEP_SEC)
                continue

            job_id = int(job["id"])
            jt = job.get("job_type")
            payload = job.get("payload") or {}
            account_id = int(job.get("account"))

            if jt in ("PLACE_TEST_ORDER", "PLACE_ORDER"):
                # --- Order execution via Windows agent ---
                windows_username = payload.get("windows_username")
                symbol = payload.get("symbol")
                side = payload.get("side")
                lots = payload.get("lots")
                magic = payload.get("magic", 0)
                comment = payload.get("comment", "")

                if not all([windows_username, symbol, side, lots, comment]):
                    complete_job(job_id, "FAILED", {"ok": False, "reason": "missing_payload_fields"},
                                 f"{jt} requires windows_username, symbol, side, lots, comment")
                    continue

                # Safety: enforce lot cap
                max_lots = 0.02
                if float(lots) > max_lots:
                    complete_job(job_id, "FAILED", {"ok": False, "reason": "lots_exceeded",
                                 "lots": lots, "max": max_lots}, f"Lot size {lots} exceeds max {max_lots}")
                    continue

                label = "SIGNAL" if jt == "PLACE_ORDER" else "DEMO"
                print(f"[{label}] Executing {jt} job_id={job_id}: {symbol} {side} {lots}")

                # Build agent order payload — include SL/TP for signal orders
                agent_payload = {
                    "username": windows_username,
                    "symbol": symbol,
                    "side": side,
                    "lots": lots,
                    "magic": magic,
                    "comment": comment,
                }
                # Pass SL/TP if present (PLACE_ORDER from signal engine)
                sl = payload.get("sl_price")
                tp = payload.get("tp_price")
                if sl is not None:
                    agent_payload["sl"] = float(sl)
                if tp is not None:
                    agent_payload["tp"] = float(tp)

                order_result = agent_order(agent_payload)

                if order_result.get("ok"):
                    print(f"[{label}] SUCCESS job_id={job_id}: order={order_result.get('order')}, price={order_result.get('price')}")
                    complete_job(job_id, "SUCCESS", {
                        "ok": True,
                        "order": order_result.get("order"),
                        "deal": order_result.get("deal"),
                        "price": order_result.get("price"),
                        "volume": order_result.get("volume"),
                        "retcode": order_result.get("retcode"),
                        "comment": order_result.get("comment"),
                    }, "")
                else:
                    error_msg = order_result.get("error", "unknown_error")
                    detail = order_result.get("detail", "")
                    print(f"[{label}] FAILED job_id={job_id}: {error_msg} {detail}")
                    complete_job(job_id, "FAILED", order_result, f"Agent order failed: {error_msg}")
                continue

            if jt != "SYNC_POSITIONS":
                complete_job(job_id, "FAILED", {"ok": False, "reason": "unsupported_job_type", "job_type": jt},
                             "Worker supports SYNC_POSITIONS, PLACE_TEST_ORDER, and PLACE_ORDER")
                continue

            # MVP: require windows_username in payload. (We can later fetch from MT5 instance.)
            windows_username = payload.get("windows_username")
            if not windows_username:
                complete_job(job_id, "FAILED", {"ok": False, "reason": "missing_windows_username"}, "payload.windows_username required")
                continue

            account = TradingAccount.objects.get(id=account_id)

            # Check if this is an auto-sync triggered by a PLACE_ORDER job
            is_auto_sync = payload.get("auto_sync", False)
            trigger_job_id = payload.get("trigger_job_id")
            expected_tag = None

            if is_auto_sync and trigger_job_id:
                expected_tag = get_expected_tag_from_trigger_job(trigger_job_id)
                print(f"[SYNC] Auto-sync for trigger_job_id={trigger_job_id}, expected_tag={expected_tag}")

            # Fetch deals with retry logic for auto-sync
            deals = []
            retry_count = 0
            max_retries = AUTO_SYNC_MAX_RETRIES if (is_auto_sync and expected_tag) else 1

            while retry_count < max_retries:
                deals_resp = agent_get("deals", windows_username)
                # allow different response shapes
                deals = deals_resp.get("deals") or (deals_resp.get("data") or {}).get("deals") or []

                # For auto-sync: check if expected deal is present
                if expected_tag:
                    if find_expected_tag_in_deals(deals, expected_tag):
                        print(f"[SYNC] Found expected deal with tag={expected_tag} on attempt {retry_count + 1}")
                        break
                    else:
                        retry_count += 1
                        if retry_count < max_retries:
                            print(f"[SYNC] Expected deal tag={expected_tag} not found, retry {retry_count}/{max_retries} in {AUTO_SYNC_RETRY_DELAY}s")
                            time.sleep(AUTO_SYNC_RETRY_DELAY)
                        else:
                            print(f"[SYNC] Expected deal tag={expected_tag} NOT FOUND after {max_retries} retries")
                else:
                    break  # No expected tag, single pass

            inserted, updated = upsert_trades(account, deals)

            # Determine if we should fail due to missing expected deal
            if expected_tag and not find_expected_tag_in_deals(deals, expected_tag):
                complete_job(
                    job_id,
                    "FAILED",
                    {
                        "ok": False,
                        "reason": "expected_deal_not_found_after_retries",
                        "expected_tag": expected_tag,
                        "trigger_job_id": trigger_job_id,
                        "retries": max_retries,
                        "deals_count": len(deals),
                    },
                    f"Expected deal with tag {expected_tag} not found after {max_retries} retries",
                )
            else:
                complete_job(
                    job_id,
                    "SUCCESS",
                    {"ok": True, "account_id": account_id, "inserted": inserted, "updated": updated, "deals_count": len(deals)},
                    "",
                )

        except Exception as e:
            print("loop_error:", repr(e))
            time.sleep(2.0)

if __name__ == "__main__":
    main()
