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
from core.observability import emit_metric, log_stage  # structured lifecycle logging (fail-open)

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

# E3-RUNTIME-RISK-CONTROLS: a shadow job whose signal aged past this at execution
# time is refused before validation (runtime staleness re-check). Default matches
# execution.models.SIGNAL_MAX_AGE_SECONDS.
STALE_MAX_AGE_SEC = int(os.getenv("MT5_SIGNAL_MAX_AGE_SECONDS", "120"))


def _env_truthy(name: str) -> bool:
    """True only for an explicit truthy value of env var ``name`` (default OFF)."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# EXEC-E2b-R1: PLACE_ORDER_SHADOW polling is OPT-IN. Only a dedicated shadow
# worker (MT5_SHADOW_WORKER set) makes the extra shadow claim each loop; the
# normal ingest worker keeps its pre-E2b 3-claim sequence, so its request rate
# stays under the API throttle. Default OFF. This gates only *polling* — the
# next_job endpoint still independently requires worker_permissions.shadow_worker.
SHADOW_WORKER_ENABLED = _env_truthy("MT5_SHADOW_WORKER")

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


def agent_order_check(payload: dict) -> dict:
    """EXEC-E2b: POST /mt5/order_check on the bridge — SHADOW dry-run.

    The bridge validates via mt5.order_check() and NEVER calls mt5.order_send().
    This is the ONLY bridge call the shadow path makes; it never touches
    agent_order (the live /mt5/order → order_send endpoint).
    """
    url = f"{AGENT_ORDER_BASE}/mt5/order_check"
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


def handle_shadow_job(job: dict) -> dict:
    """EXEC-E2b: process a PLACE_ORDER_SHADOW job — validate only, place no order.

    Reads ``execution_mode`` (fail-closed on anything but SHADOW), calls the
    bridge SHADOW dry-run (``agent_order_check`` → ``mt5.order_check``, never
    ``order_send``), and completes the job SUCCESS (storing the validation
    diagnostics — no ticket/deal/order id) or FAILED (invalid symbol, margin,
    market closed, invalid stops). Returns the completion result for testability.
    Never routes into the live execution path.
    """
    job_id = int(job["id"])
    payload = job.get("payload") or {}
    mode = str(payload.get("execution_mode", "")).upper()

    # OPS-OBSERVABILITY (stages 6-9): correlation id from the payload + a helper to
    # compute execution duration (now − job.created_at). All emissions are fail-open.
    correlation_id = payload.get("correlation_id", "")
    _created = to_dt(job.get("created_at"))

    def _duration_ms():
        if not _created:
            return None
        try:
            return int((dt_module.datetime.now(dt_module.timezone.utc) - _created).total_seconds() * 1000)
        except Exception:
            return None

    def _finalize_outcome(ok, **extra):
        log_stage("validation_outcome", correlation_id, job_id=job_id, ok=ok, **extra)
        emit_metric("validation_success" if ok else "validation_failure", 1,
                    correlation_id=correlation_id, **extra)
        dur = _duration_ms()
        log_stage("cleanup_complete", correlation_id, job_id=job_id, execution_duration_ms=dur)
        if dur is not None:
            emit_metric("execution_duration", dur, unit="ms", correlation_id=correlation_id)

    # execution_mode handling: only SHADOW is executed here; LIVE and unknown
    # fail closed (E2b never places an order — LIVE placement is a later, gated
    # packet). Nothing is ever routed to the live agent_order/order_send path.
    if mode != "SHADOW":
        result = {"ok": False, "shadow": True, "order_send_called": False,
                  "error": "execution_mode_not_shadow", "execution_mode": mode or "(missing)"}
        complete_job(job_id, "FAILED", result,
                     f"Shadow job {job_id}: execution_mode={mode or '(missing)'} is not SHADOW — refused")
        print(f"[SHADOW] REFUSED job_id={job_id}: execution_mode={mode or '(missing)'} (fail-closed, no order)")
        _finalize_outcome(False, reason="execution_mode_not_shadow")
        return result

    symbol = payload.get("symbol")
    side = payload.get("side")
    lots = payload.get("lots")
    comment = payload.get("comment", "")
    if not all([symbol, side, lots, comment]):
        result = {"ok": False, "shadow": True, "order_send_called": False,
                  "error": "missing_payload_fields"}
        complete_job(job_id, "FAILED", result,
                     f"Shadow job {job_id} requires symbol, side, lots, comment")
        _finalize_outcome(False, reason="missing_payload_fields")
        return result

    # E3-RUNTIME-RISK-CONTROLS (runtime staleness re-check): if the signal aged
    # past STALE_MAX_AGE_SEC while the job waited in the queue, refuse it BEFORE
    # validation (no order_check). Only applies when a signal_timestamp is present
    # (directly-created dry-run jobs without one are unaffected). Fail-closed: an
    # unparseable timestamp is treated as stale.
    sig_ts_raw = payload.get("signal_timestamp")
    if sig_ts_raw:
        try:
            sig_ts = to_dt(sig_ts_raw)
            age = (dt_module.datetime.now(dt_module.timezone.utc) - sig_ts).total_seconds()
            stale = age > STALE_MAX_AGE_SEC
        except Exception:
            age, stale = -1, True  # fail-closed: cannot determine age → refuse
        if stale:
            result = {"ok": False, "shadow": True, "order_send_called": False,
                      "error": "stale_at_execution", "age_seconds": int(age)}
            complete_job(job_id, "FAILED", result,
                         f"Shadow job {job_id}: signal aged {int(age)}s > {STALE_MAX_AGE_SEC}s at execution — refused")
            print(f"[SHADOW] STALE job_id={job_id}: age={int(age)}s (no order_check, no order)")
            _finalize_outcome(False, reason="stale_at_execution")
            return result

    check_payload = {
        "symbol": symbol, "side": side, "lots": lots,
        "magic": payload.get("magic", 0), "comment": comment,
    }
    if payload.get("sl_price") is not None:
        check_payload["sl"] = float(payload["sl_price"])
    if payload.get("tp_price") is not None:
        check_payload["tp"] = float(payload["tp_price"])

    print(f"[SHADOW] Validating (NO order) job_id={job_id}: {symbol} {side} {lots}")
    log_stage("order_check_request", correlation_id, job_id=job_id,
              symbol=symbol, side=side, lots=str(lots))
    t0 = time.monotonic()
    check = agent_order_check(check_payload)  # bridge → mt5.order_check, never order_send
    latency_ms = int((time.monotonic() - t0) * 1000)
    log_stage("order_check_response", correlation_id, job_id=job_id,
              ok=bool(check.get("ok")), retcode=check.get("retcode"),
              mt5_response_latency_ms=latency_ms)
    emit_metric("mt5_response_latency", latency_ms, unit="ms", correlation_id=correlation_id)

    validation = {
        "shadow": True,
        "order_send_called": False,   # structural: the shadow path calls only order_check
        "execution_mode": "SHADOW",
        "validation_latency_ms": latency_ms,
        "retcode": check.get("retcode"),
        "margin": check.get("margin"),
        "free_margin": check.get("free_margin"),
        "comment": check.get("comment"),
        "request": check.get("request"),
        "error": check.get("error"),
    }

    if check.get("ok"):
        complete_job(job_id, "SUCCESS", {**validation, "ok": True}, "")
        # EXEC-E2b metric line (shadow jobs completed + validation latency).
        print(f"[SHADOW_METRIC] completed job_id={job_id} retcode={check.get('retcode')} "
              f"margin={check.get('margin')} latency_ms={latency_ms}")
        _finalize_outcome(True, retcode=check.get("retcode"))
        return {**validation, "ok": True}

    err = check.get("error", "order_check_failed")
    complete_job(job_id, "FAILED", {**validation, "ok": False},
                 f"Shadow validation failed: {err}")
    print(f"[SHADOW_METRIC] failed job_id={job_id} error={err} latency_ms={latency_ms}")
    _finalize_outcome(False, reason=str(err))
    return {**validation, "ok": False}


def to_dt(x):
    if not x:
        return None
    dt = parse_datetime(x)
    return dt

def to_dec(x, default="0"):
    if x is None:
        return Decimal(default)
    return Decimal(str(x))

def find_expected_tag_in_deals(
    deals: list[dict],
    expected_tag: str,
    expected_order=None,
    expected_deal=None,
) -> bool:
    """Check whether the placed trade is present in the deals snapshot.

    MT5 / the broker truncates or strips the order comment on the stored
    history deal (e.g. "GUVFX_DEMO_JOB:30" is persisted as "GUVFX_DEMO_JOB:3",
    a hard 16-char cut), so matching purely on comment fails for any job id
    >= 10. We therefore match primarily on the order / position / deal ticket
    returned by the PLACE result (which MT5 never mutates), and fall back to an
    exact comment match for legacy callers that pass only a tag.
    """
    def _norm(v):
        s = str(v).strip() if v is not None else ""
        return s if s and s != "0" else None

    eo = _norm(expected_order)
    ed = _norm(expected_deal)
    tag = str(expected_tag or "").strip()
    if not (eo or ed or tag):
        return True  # nothing to match against, consider it found
    for d in deals:
        if eo and (str(d.get("order")).strip() == eo or str(d.get("position_id")).strip() == eo):
            return True
        if ed and str(d.get("ticket")).strip() == ed:
            return True
        if tag and str(d.get("comment") or "").strip() == tag:
            return True
    return False


def get_expected_ids_from_trigger_job(trigger_job_id: int):
    """Return (tag, order_ticket, deal_ticket) for the placed trade.

    The order/deal tickets come from the PLACE job's result (set by the agent
    when the order is filled) and are the reliable join key for SYNC, since the
    comment tag is truncated by MT5 in history deals.
    """
    from execution.models import ExecutionJob
    try:
        job = ExecutionJob.objects.get(id=trigger_job_id)
    except ExecutionJob.DoesNotExist:
        return (None, None, None)
    except Exception:
        return (None, None, None)
    payload = job.payload or {}
    result = job.result or {}
    tag = payload.get("comment") or result.get("comment")
    tag = str(tag).strip() if tag else None
    order_ticket = result.get("order") or payload.get("order")
    deal_ticket = result.get("deal")
    return (tag, order_ticket, deal_ticket)


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


def claim_worker_job():
    """Claim the next job for this worker, by mode.

    A **dedicated shadow worker** (``SHADOW_WORKER_ENABLED`` / ``MT5_SHADOW_WORKER``)
    claims **only** ``PLACE_ORDER_SHADOW`` — never the executable
    ``PLACE_TEST_ORDER``/``PLACE_ORDER`` types and never the default SYNC. This
    is structural: a shadow worker can never win a real order and route it to
    the live ``order_send`` path (E2b-R2), and its poll rate is one claim per
    loop (well under the API throttle). Its claim is still gated server-side by
    the next_job endpoint, which requires ``worker_permissions.shadow_worker``.

    The **normal worker** (flag OFF, the default) keeps its exact pre-E2b claim
    sequence: PLACE_TEST_ORDER > PLACE_ORDER > SYNC_POSITIONS (default). It never
    claims ``PLACE_ORDER_SHADOW``.
    """
    if SHADOW_WORKER_ENABLED:
        return claim_next_job("PLACE_ORDER_SHADOW")
    return (
        claim_next_job("PLACE_TEST_ORDER")
        or claim_next_job("PLACE_ORDER")
        or claim_next_job()
    )


def main():
    if not WORKER_TOKEN:
        raise SystemExit("MT5_WORKER_TOKEN missing")
    if not AGENT_BASE or not AGENT_TOKEN:
        raise SystemExit("GUVFX_AGENT_URL/TOKEN (or WINDOWS_AGENT_BASE/TOKEN) missing")

    print("worker:", WORKER_ID, "agent_order_base:", AGENT_ORDER_BASE)
    while True:
        try:
            try:  # RX-2C: heartbeat (never break the worker loop)
                from reliability.services.heartbeat import record_beat
                record_beat("ingest_worker", interval_s=60)
            except Exception:
                pass
            # Claim by mode (see claim_worker_job): a dedicated shadow worker
            # (MT5_SHADOW_WORKER) claims ONLY PLACE_ORDER_SHADOW; the normal
            # worker (default) claims PLACE_TEST_ORDER > PLACE_ORDER >
            # SYNC_POSITIONS and never a shadow job.
            job = claim_worker_job()
            if not job:
                time.sleep(SLEEP_SEC)
                continue

            job_id = int(job["id"])
            jt = job.get("job_type")
            payload = job.get("payload") or {}
            account_id = int(job.get("account"))

            # EXEC-E2b: SHADOW jobs go to the dry-run path (order_check only),
            # NEVER the live PLACE_ORDER/order_send path below.
            if jt == "PLACE_ORDER_SHADOW":
                handle_shadow_job(job)
                continue

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
            expected_order = None
            expected_deal = None

            if is_auto_sync and trigger_job_id:
                expected_tag, expected_order, expected_deal = get_expected_ids_from_trigger_job(trigger_job_id)
                print(f"[SYNC] Auto-sync for trigger_job_id={trigger_job_id}, expected_tag={expected_tag} order={expected_order} deal={expected_deal}")

            # Has the placed trade landed in the deals snapshot yet?
            have_expected = lambda dl: find_expected_tag_in_deals(dl, expected_tag, expected_order, expected_deal)

            # Fetch deals with retry logic for auto-sync
            has_expected_key = bool(expected_tag or expected_order or expected_deal)
            deals = []
            retry_count = 0
            max_retries = AUTO_SYNC_MAX_RETRIES if (is_auto_sync and has_expected_key) else 1

            while retry_count < max_retries:
                deals_resp = agent_get("deals", windows_username)
                # allow different response shapes
                deals = deals_resp.get("deals") or (deals_resp.get("data") or {}).get("deals") or []

                # For auto-sync: check if expected deal is present (by ticket, comment fallback)
                if has_expected_key:
                    if have_expected(deals):
                        print(f"[SYNC] Found expected trade (order={expected_order} deal={expected_deal} tag={expected_tag}) on attempt {retry_count + 1}")
                        break
                    else:
                        retry_count += 1
                        if retry_count < max_retries:
                            print(f"[SYNC] Expected trade order={expected_order} not found, retry {retry_count}/{max_retries} in {AUTO_SYNC_RETRY_DELAY}s")
                            time.sleep(AUTO_SYNC_RETRY_DELAY)
                        else:
                            print(f"[SYNC] Expected trade order={expected_order} deal={expected_deal} tag={expected_tag} NOT FOUND after {max_retries} retries")
                else:
                    break  # No expected key, single pass

            inserted, updated = upsert_trades(account, deals)

            # Determine if we should fail due to missing expected deal
            if has_expected_key and not have_expected(deals):
                complete_job(
                    job_id,
                    "FAILED",
                    {
                        "ok": False,
                        "reason": "expected_deal_not_found_after_retries",
                        "expected_tag": expected_tag,
                        "expected_order": expected_order,
                        "expected_deal": expected_deal,
                        "trigger_job_id": trigger_job_id,
                        "retries": max_retries,
                        "deals_count": len(deals),
                    },
                    f"Expected trade order={expected_order} deal={expected_deal} not found after {max_retries} retries",
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
