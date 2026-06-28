# Packet — Wayond Telegram Signal Strategy (WIMS content + gated execution)

- **Status:** PROPOSED (lifecycle owned by PM / Nuno — not self-advanced)
- **Requested by:** WIMS
- **Authoritative lifecycle:** Notion (this file is the concise Git-side record)
- **ADR-009:** in force — GuvFX executes/produces intelligence; WIMS consumes/publishes.

## Objective

A strategy that consumes the "Wayond | FX Signals" Telegram channel and:
1. (content) turns each signal into WIMS educational content,
2. (content) packages **winning** closed trades — with a results card — for WIMS
   to publish as social content, and **never** publishes losers,
3. (execution, GATED) optionally places trades from the signals.

## Two independent flows from one source (the clean design)

```
Wayond Telegram ──┬─▶ content flow  : parse → SignalIntelligenceEnvelope → WIMS ConsumptionContract → Context → Content → human review → publish
                  └─▶ execution flow: parse → PendingSignalApproval → HUMAN APPROVES → ExecutionJob → Windows Agent → broker
Closed winning trade ─▶ TradeResult envelope + results card → WIMS packet → human review → publish (WIN-only)
```

The content flow and the execution flow must stay **decoupled**: the WIMS
`ConsumptionContract` / `raw_signal` is advisory/educational and must never become
an order trigger (ADR-009: WIMS never trades).

## Delivered in this packet (content-only, no trading) — branch `feat/wayond-telegram-wims-content`

- `intelligence/telegram_source.py` — Wayond message parser, dedup, quarantine.
- `intelligence/results_card.py` — mobile **trade result card**: one layout model
  → **PNG** (Telegram/Instagram-ready, via Pillow) + internal SVG. White app-style
  background, green winner bar, symbol + buy/sell + lot, entry→close (vector arrow),
  close time, prominent blue profit, and a Total Profit summary. Supports one row
  or multiple partial-close rows. Generated from trade data — not a screenshot, no
  broker branding.
- `intelligence/caption.py` — `build_caption`: win statement, symbol, direction,
  pips (where computable), net profit + currency, non-overclaiming automation
  wording. Becomes the audience-facing `Content.content_text`.
- `intelligence/delivery.ingest_wayond_telegram_signal` — signal → WIMS content.
- `intelligence/delivery.ingest_winning_trade` — WIN-only winner (single trade or a
  partial-close list) → card (PNG) + caption → WIMS packet. Rejects total pnl ≤ 0
  (losers AND breakeven).
- `wims.ConsumptionContract.media` (additive JSONField; migration `0006`) — carries
  `{results_card:{png_base64,data_uri,svg,format}, caption}` on the content side only.
- Management commands `ingest_wayond_telegram`, `publish_winning_trade` (`--out` writes
  the PNG; `--fixture` accepts a single trade or a partial-close list).
- Tests (41 wims+intelligence) + fixtures (incl. `xauusd_partial_closes.json`).
- New dependency: `Pillow>=10.1` (PNG rendering; bundled scalable font, no font files committed).

Loser-suppression is enforced twice: `ingest_winning_trade` rejects non-winners/breakeven,
and WIMS' mandatory human-review gate (`services.py`) blocks any unapproved publish.

## NOT delivered here — execution flow (governance Red, blocked)

Auto-placing orders from Telegram is **out of scope of this code** and requires,
before any line is written:

1. **Explicit human-gated control path** — `PendingSignalApproval` queue; an
   operator approves each signal (or arms a strategy) before an `ExecutionJob` is
   created. No automatic PENDING→RUNNING for Telegram-sourced orders.
2. **Demo/live routing enforced** — `is_demo` is currently passed but NOT enforced
   in the worker; "paper" can reach the live bridge. Must be enforced + tested.
3. **Kill-switch** — one-command halt for Telegram-driven trading, independent of
   other signals.
4. **Broker-server timezone verification** (still BLOCKED, `docs/NEXT.md`).
5. **Blueprint doc-06** execution-architecture reconciliation.
6. **Source trust** — sender/signature verification + message-id dedup against
   replay; the channel is a low-trust, ~13-subscriber source.
7. **Approved Notion packet + Nuno's out-of-band sign-off** for live-order authority.

## Open questions

- "Winner" definition for publishing: net pnl > 0 (current), or > N pips, or after
  a holding-period? (currently net = profit + commission + swap > 0)
- Channel/account identifiers and the real Telegram export/listener wiring
  (`python-telegram-bot`/Telethon + `TELEGRAM_*` secrets in env, never in Git/Notion).
- PNG rendering of the SVG card for platforms that need raster (downstream).

## Evidence

- `python manage.py test wims intelligence` → 37 passed.
- `python manage.py ingest_wayond_telegram` → 3 content contracts, 1 update skipped,
  1 quarantined, 0 trades placed.
- `python manage.py publish_winning_trade` → WIN packaged with results card, held at
  human-review gate; loser path rejected. `manage.py check` clean.
