# intelligence — GuvFX Intelligence Producer (Phase 7A / 7B)

GuvFX-side intelligence **producer** (ADR-009: *GuvFX creates intelligence,
WIMS consumes it*). This app packages authoritative GuvFX inputs into immutable
intelligence **envelopes** and delivers them into the existing WIMS consumption
pipeline.

- **Phase 7A** — Wayond signal → Signal Intelligence Envelope (Wayond only).
- **Phase 7B** — closed trade → Trade Result Intelligence Envelope.

Both are **packaging + delivery, not generation/execution** — the producer
never invents a signal nor opens/closes a trade; it wraps inputs it is given.

## Flows

```
Wayond Signal (external)
  → Signal Intelligence Envelope (SIGNAL, v1.0)
  → WIMS Consumption Contract (source_type=WAYOND)
  → existing WIMS pipeline

Closed Trade (trading.models.Trade)
  → Trade Result Intelligence Envelope (TRADE_RESULT, v1.0)
  → WIMS Consumption Contract (source_type=TRADE_RESULT)
  → existing WIMS pipeline
```

## Components (no persistence models)

- `envelope.py` — frozen, immutable envelopes + payloads (`Signal*` and
  `TradeResult*`). Header: `intelligence_id`, `intelligence_type`,
  `version="1.0"`, `source`, `timestamp`, `confidence`, `summary`,
  `structured_payload`. No registry.
- `producer.py` — `SignalIntelligenceProducer.produce(signal)` (7A, pure).
- `trade_result_producer.py` — `TradeResultProducer.produce(trade)` (7B, pure).
  Accepts a `trading.models.Trade` instance (authoritative source) or an
  equivalent mapping; requires a *closed* trade; derives outcome/pnl/pips.
- `delivery.py` — `ingest_wayond_signal` (7A) and `ingest_trade_result` (7B) run
  the full audited path and return `(envelope, contract)`. The deliver functions
  are the GuvFX→WIMS boundary crossing; they consume via the **unchanged**
  `wims.services.create_contract` (WAYOND / WP-3 TRADE_RESULT paths).

The envelope is **transient** — never persisted as its own object. The first
persisted artefact is the WIMS `ConsumptionContract`.

## Audit (reuses existing WIMS capability)

No new audit framework, no `IntelligenceAuditRecord`. Four lifecycle events per
run are recorded via the existing `wims` audit (`AuditEvent` +
`record_audit_ref`), correlated by `intelligence_id`:

```
7A:  SIGNAL_RECEIVED → ENVELOPE_CREATED → ENVELOPE_DELIVERED → ENVELOPE_CONSUMED
7B:  TRADE_DETECTED  → ENVELOPE_CREATED → ENVELOPE_DELIVERED → ENVELOPE_CONSUMED
```

(plus WIMS' own `CONTRACT_CREATED` at consumption).

## Dependency direction (ADR-009)

`intelligence` (GuvFX) → `wims` (consumer). WIMS never imports `intelligence`.
`intelligence` persists **no** models; WIMS persists no Trade/Position/Deal/
Execution/MT5/Broker object. Both are asserted by the demo and tests.

## Demonstrate / test

Runs on the isolated SQLite settings shim (no Postgres role; production DB
untouched):

```bash
cd backend
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py migrate
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py produce_wayond_signal
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py test wims intelligence
```

`produce_wayond_signal` (7A) reads a Wayond signal (default fixture
`intelligence/fixtures/wayond_signal_sample.json`, or `--fixture` / `--signal`).
`produce_trade_result` (7B) reads a closed trade — in production via
`--ticket <t> [--account <id>]` from the real `trading.models.Trade`; offline via
the default fixture `intelligence/fixtures/closed_trade_sample.json` (or
`--fixture`/`--trade`). Both produce + deliver the envelope, run the existing
WIMS pipeline, print the five evidence points + audit trail, and end in `PASS`.
`--no-pipeline` stops after consumption.

In deployment, run against Postgres unchanged (ORM-only), e.g.
`python manage.py migrate wims && python manage.py produce_trade_result --ticket <t>`.

## Wayond content + winning-trade card (content-only — NO trading)

Two additive, content-side capabilities. Neither places a trade.

- **`telegram_source.py`** — pure parser for the "Wayond | FX Signals" channel.
  `parse_message` → `ParsedSignal` (SIGNAL / UPDATE / UNKNOWN); `classify_messages`
  dedups by message id and **quarantines** anything unparseable. `to_producer_signal`
  maps a SIGNAL onto the Phase 7A producer shape.
- **`results_card.py`** — dependency-free **SVG** of the MT5-style results/history
  row(s) filtered to one order/day (not a chart, not a screen-capture). Generated
  from GuvFX trade data, so there is no broker-ToS / VPS-automation concern.
- **`delivery.ingest_wayond_telegram_signal`** — a parsed Wayond signal → WIMS
  *content* `ConsumptionContract` (reuses Phase 7A `ingest_wayond_signal`).
- **`delivery.ingest_winning_trade`** — **WIN-only**: a closed winning trade →
  trade-result envelope + results card (attached as content-side `ConsumptionContract.media`)
  → WP-3 → Content → **human review** → publish. Losers raise `ValueError` and
  never enter the pipeline; nothing publishes without the mandatory review approval.

Commands:

```bash
# Telegram messages -> WIMS content (0 trades placed)
python manage.py ingest_wayond_telegram [--file export.json]
# Winning trade -> results card -> WIMS packet, held at the human-review gate
python manage.py publish_winning_trade [--ticket <t> --account <id>] [--out card.svg]
```

**Boundary:** these feed WIMS *content* only. Wiring a Wayond signal to actual
order execution is a separate, human-gated, governance-Red path (live-order
authority, demo/live routing, kill-switch, broker-timezone, Blueprint-06) and is
**not** built here — see the Notion packet `docs/packets/wayond-telegram-strategy.md`.
