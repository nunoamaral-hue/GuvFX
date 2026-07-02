# Broker-Server Timezone Evidence v1 — TradersWay-Demo

**Packet:** GFX-PKT-006D-TZ-PROBE (Option A, Nuno-authorised, read-only)
**Date probed:** 2026-07-01
**Result:** Broker server time = **UTC+3** (EEST / summer offset) — DST-dependent (see caveat).

## Method (read-only, no order)

Compared a *fresh* EURUSD **M1** bar timestamp (which MT5 reports in **broker-server
time**) against NTP-synced UTC captured around the same instant, via the existing
read-only bridge endpoint `GET /mt5/snapshots/rates`. No `order_send`, no
`order_check`, no account change, no service restart, no code change. Offset rounded
to the nearest whole hour (FX broker offsets are integer hours).

## Evidence

| Field | Value |
|---|---|
| Probe method | Read-only `/mt5/snapshots/rates?symbol=EURUSD&timeframe=M1&count=3`; server-bar vs UTC |
| Account / terminal | TradersWay-Demo acct **1121106**, `terminal64`, Windows MT5 box `100.79.101.19`, bridge :8788 (DEMO, `trade_mode=0`) |
| Host clock | NTP-synchronised: **yes** (`timedatectl`) |
| UTC now (epoch / human) | `1782935784` = `2026-07-01 19:56:24 UTC` (Wednesday, FX market open) |
| Latest server bar (epoch / labelled) | `1782946560` = `2026-07-01 22:56:00` (server-time-labelled) |
| Raw difference | `10776 s` ≈ `2h 59m 36s` |
| **Computed offset** | **UTC+3 hours** (`round(10776 / 3600) = 3`) |
| Freshness residual | `24 s` into the current server minute (0–120 s ⇒ fresh / market-open ⇒ valid) |
| Capture window | `0 s` (tight; UTC captured immediately around the call) |
| Bar cadence check | bar times `[…946440, …946500, …946560]` — 60 s apart ⇒ genuine M1 series |
| Validity | `VALID = True` (offset within [−12,+14]; residual within [0,180]) |
| order_send called | **No** — read-only rates fetch only |
| Any trade/order/deal/position | **No** |

## Confidence

**HIGH** for the current season: NTP-synced host, market open, fresh current-minute
bar (24 s residual), tight capture window, whole-hour offset unambiguous.

## Caveat — DST dependence (year-round mapping incomplete)

This is a **summer** observation (2026-07-01, EEST). Many EET/EEST brokers run
**UTC+3 in summer** and **UTC+2 in winter**. This evidence establishes the summer
offset only. For a full year-round mapping, **re-probe after the next DST transition**
(EU winter time begins late October 2026) and record a v2 evidence entry.

## Usage rule

Downstream code/research must **read this recorded offset**, not hardcode it.
Any normalisation of broker bar times to UTC uses `server − 3h` for dates in the
EEST window; a winter (`−2h`) entry must be added before relying on the mapping
across a DST boundary.
