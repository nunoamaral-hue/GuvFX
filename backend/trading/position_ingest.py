"""Canonical MT5 deal->position builder — the single source of truth for turning a raw MT5 deal snapshot
into POSITION-level rows (one record per ``position_id``, with authoritative open/close price+profit).

Both trade writers persist through this so the Trade table has ONE consistent shape (one row per position):
  * the continuous ``mt5_trade_ingest_worker`` (SYNC_POSITIONS), and
  * the on-demand ``trading.views.SyncNowView``.

This matters for the read model: ``analytics.views_trade_history`` renders each CLOSED position as one
round-trip. A per-DEAL writer (the old SyncNowView behaviour) would emit entry+exit legs as two rows and
corrupt round-trip counts / observed_stats. Keeping the position shape in one place removes that divergence.

Pure + side-effect-free (safe to import from a Django request path — no ``django.setup``).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

# MT5 deal entry types (DEAL_ENTRY_*): IN opens a position, OUT/OUT_BY close it.
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3


def to_dec(x, default="0") -> Decimal:
    if x is None:
        return Decimal(default)
    return Decimal(str(x))


def fnum(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def deal_entry_type(d: dict):
    """Deal entry type as int (0=IN/open, 1=OUT/close, 2=INOUT, 3=OUT_BY), or None if absent."""
    e = d.get("entry")
    try:
        return int(e) if e is not None else None
    except (TypeError, ValueError):
        return None


def deal_time_to_utc(d: dict):
    """Extract deal.time (unix seconds) as an aware UTC datetime, else None."""
    raw = d.get("time")
    if raw and isinstance(raw, (int, float)):
        try:
            return _dt.datetime.utcfromtimestamp(raw).replace(tzinfo=_dt.timezone.utc)
        except (ValueError, OSError):
            pass
    return None


def build_positions_from_deals(deals: list[dict]) -> list[dict]:
    """Group raw MT5 deals by ``position_id`` into one position record each, with AUTHORITATIVE open/close
    prices taken from the entry (DEAL_ENTRY_IN) and exit (DEAL_ENTRY_OUT/OUT_BY) deals — NEVER inferred from
    profit, target or market price.

    A position is reported CLOSED only when its total OUT volume >= its total IN volume; otherwise it is
    FAIL-CLOSED to OPEN (``close_time``/``close_price`` = None) so a partial or one-sided fill never produces
    a premature outcome. Deals with no ``position_id`` (or a position with no IN deal — e.g. a balance/credit
    record) cannot form a trade and are skipped. One malformed position is skipped, never aborting the batch.
    Prices are volume-weighted across multiple deals on the same side; commission/swap sum across all deals."""
    by_pos: dict[str, list] = {}
    for d in deals:
        if not isinstance(d, dict):
            continue  # skip a malformed element (None / non-dict) — never abort the whole batch
        pid = str(d.get("position_id") or "").strip()
        if pid and pid != "0":
            by_pos.setdefault(pid, []).append(d)
    positions = []
    for pid, dl in by_pos.items():
        try:
            ins = [d for d in dl if deal_entry_type(d) == DEAL_ENTRY_IN]
            outs = [d for d in dl if deal_entry_type(d) in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)]
            if not ins:
                continue  # no opening leg -> not a tradeable position
            in_vol = sum(fnum(d.get("volume")) for d in ins) or fnum(ins[0].get("volume"))
            open_price = (sum(fnum(d.get("price")) * fnum(d.get("volume")) for d in ins) / in_vol) \
                if in_vol else fnum(ins[0].get("price"))
            open_time = min((deal_time_to_utc(d) for d in ins if deal_time_to_utc(d)), default=None)
            first_in = ins[0]
            symbol = (first_in.get("symbol") or "").strip()
            side = "BUY" if str(first_in.get("type")) == "0" else "SELL"
            comment = str(first_in.get("comment") or "").strip()
            m = first_in.get("magic") if first_in.get("magic") is not None else first_in.get("magic_number")
            try:
                magic = int(m) if m is not None else None
            except (TypeError, ValueError):
                magic = None
            out_vol = sum(fnum(d.get("volume")) for d in outs)
            commission = to_dec(sum(fnum(d.get("commission")) for d in dl))
            swap = to_dec(sum(fnum(d.get("swap")) for d in dl))
            is_closed = bool(outs) and out_vol + 1e-9 >= in_vol  # fail-closed: partial => still open
            if is_closed:
                close_price = (sum(fnum(d.get("price")) * fnum(d.get("volume")) for d in outs) / out_vol) \
                    if out_vol else fnum(outs[-1].get("price"))
                close_time = max((deal_time_to_utc(d) for d in outs if deal_time_to_utc(d)), default=None)
                profit = to_dec(sum(fnum(d.get("profit")) for d in outs))
            else:
                close_price, close_time = None, None
                profit = to_dec(sum(fnum(d.get("profit")) for d in dl))
            positions.append({
                "position_id": pid, "symbol": symbol, "side": side, "volume": to_dec(in_vol),
                "open_time": open_time, "open_price": to_dec(round(open_price, 5)),
                "close_time": close_time,
                "close_price": (to_dec(round(close_price, 5)) if close_price is not None else None),
                "profit": profit, "commission": commission, "swap": swap,
                "comment": comment, "magic": magic,
            })
        except Exception as exc:  # one malformed position must not abort the batch
            print(f"[position_ingest] skipped malformed position_id={pid}: {exc!r}")
            continue
    return positions
