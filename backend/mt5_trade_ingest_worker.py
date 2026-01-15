import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal

# --- Django ORM init ---
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guvfx_backend.settings")
import django
django.setup()

from django.utils.dateparse import parse_datetime
from trading.models import TradingAccount, Trade

API_BASE = "http://127.0.0.1:8000"
API_HOST = "api.guvfx.com"
WORKER_ID = os.getenv("MT5_WORKER_ID", "mt5-trade-ingest-1")

WORKER_TOKEN = os.getenv("MT5_WORKER_TOKEN", "")
AGENT_BASE = (os.getenv("GUVFX_AGENT_URL") or os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
AGENT_TOKEN = (os.getenv("GUVFX_AGENT_TOKEN") or os.getenv("WINDOWS_AGENT_TOKEN") or "").strip().strip('"')

SLEEP_SEC = float(os.getenv("MT5_WORKER_SLEEP", "2.0"))

def api_headers():
    return {
        "Host": API_HOST,
        "X-Forwarded-Proto": "https",
        "X-Worker-Token": WORKER_TOKEN,
    }

def claim_next_job():
    url = f"{API_BASE}/api/execution/jobs/next/?worker_id={urllib.parse.quote(WORKER_ID)}"
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

def to_dt(x):
    if not x:
        return None
    dt = parse_datetime(x)
    return dt

def to_dec(x, default="0"):
    if x is None:
        return Decimal(default)
    return Decimal(str(x))

def upsert_trades(account: TradingAccount, deals: list[dict]):
    inserted = 0
    updated = 0

    for d in deals:
        ticket = str(d.get("ticket") or d.get("position_ticket") or d.get("deal_id") or "").strip()
        if not ticket:
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

        if changed:
            obj.save()
            updated += 1

    return inserted, updated

def main():
    if not WORKER_TOKEN:
        raise SystemExit("MT5_WORKER_TOKEN missing")
    if not AGENT_BASE or not AGENT_TOKEN:
        raise SystemExit("GUVFX_AGENT_URL/TOKEN (or WINDOWS_AGENT_BASE/TOKEN) missing")

    print("worker:", WORKER_ID)
    while True:
        try:
            job = claim_next_job()
            if not job:
                time.sleep(SLEEP_SEC)
                continue

            job_id = int(job["id"])
            jt = job.get("job_type")
            payload = job.get("payload") or {}
            account_id = int(job.get("account"))

            if jt != "SYNC_POSITIONS":
                complete_job(job_id, "FAILED", {"ok": False, "reason": "unsupported_job_type", "job_type": jt}, "Worker only supports SYNC_POSITIONS in A2.1")
                continue

            # MVP: require windows_username in payload. (We can later fetch from MT5 instance.)
            windows_username = payload.get("windows_username")
            if not windows_username:
                complete_job(job_id, "FAILED", {"ok": False, "reason": "missing_windows_username"}, "payload.windows_username required")
                continue

            account = TradingAccount.objects.get(id=account_id)

            deals_resp = agent_get("deals", windows_username)
            # allow different response shapes
            deals = deals_resp.get("deals") or (deals_resp.get("data") or {}).get("deals") or []
            inserted, updated = upsert_trades(account, deals)

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
