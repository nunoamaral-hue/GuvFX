# ADR-0063 — Account-scoped, execution-free MT5 symbol visibility

- **Status:** Accepted (repository-only; DARK). No production deploy; execution OFF.
- **Date:** 2026-10-29
- **Workstream:** Copy Trading (catalogue readiness)
- **Supersedes / relates to:** ADR-0049 (per-tenant execution transport), ADR-0062 (reviewed mapping
  controls), the P0 data-isolation read firewall (`execution/snapshot_transport.py`).

## Context

Copy Trading's symbol authority is each account's `BrokerInstrument` catalogue, captured by
`sync_broker_instruments` from the account's OWN identity-verified bridge (`GET /mt5/symbols` →
`mt5.symbols_get()`). A symbol is stored `enabled` only when the terminal reports it **visible** (in
Market Watch) **and** `trade_mode != 0`.

Account #50 (`IS6Technologies-Live`, login …3832) correctly connects and the broker offers `XAUUSD.std`
with `trade_mode=FULL`, but the bridge reports it `visible=false` / `tradeable=false`, so it is captured
`enabled=False` and cannot become a copier candidate. The account's terminal runs **headless** (a background
Windows session, no interactive Market Watch GUI). The read bridge (`_handle_symbols_request`) is pure-read
and never alters visibility; the only code that calls `mt5.symbol_select(symbol, True)` is the **order /
order-validation path** (`validate_broker_symbol`, the order bridges). There was therefore **no supported,
execution-free, account-isolated way** to reveal a Market Watch symbol — the earlier recovery packet correctly
STOPPED here (headless terminal, no supported Show path).

## Decision

Introduce a dedicated, narrow administrative capability — **symbol visibility is a terminal-state operation,
separate from catalogue mapping and from order execution** — with two layers:

1. **Bridge op `ensure_symbol_visible(mt5, symbol, *, init_kwargs)`** (`scripts/mt5_signal_bridge.py`) and the
   authenticated route **`POST /mt5/ensure-symbol-visible`** (single explicit `symbol`). It attaches read-only
   via `guarded_initialize` (never launches, never authenticates), calls `mt5.symbol_select(symbol, True)`
   **only** when the symbol exists and is not already visible, **proves** the postcondition
   (`symbol_info(...).visible`), always detaches, and returns structured before/after evidence. It is
   deliberately **not** `validate_broker_symbol` and shares no code with any order path.

2. **Governed command `python manage.py ensure_symbol_visible --account <id> --symbol <SYM> [--commit]`**
   (`backend/execution/management/commands/`). Dry-run by default. It resolves ONLY the account's OWN bridge
   (`resolve_account_snapshot_base`), proves the observed MT5 identity (`verify_snapshot_identity`, login ==
   `account_number` + server) **before and after** the op, refuses a blank/wildcard/list/bulk symbol, an
   unavailable symbol, an unresolved/stale/mismatched identity, a timeout and an unproven postcondition, and
   emits a secret-free audit line (correlation id, masked login, server, symbol, before/after).

### Authorization boundary

- Reuses the bridge's existing fail-closed auth (`_validate_token`, `GUVFX_AGENT_TOKEN`); no new/unauthenticated
  endpoint; **not** a general-purpose MT5 command executor — the operation is symbol visibility only.
- Separate from execution: imports/calls no `order_send` / `order_check` / position-modify / execution
  producer / consumer / job-claim path; creates no `ExecutionJob` / `Trade` / copier record; works while every
  Copy Trading execution flag is OFF. A negative-control test fails if an order path is reached.

### `is_active` is intentionally NOT a gate (Amber decision)

`TradingAccount.is_active` encodes execution-**authorisation** state, not connectivity/validity; legitimate
live-monitoring accounts (incl. #50) run `is_active=False`. Gating on it would make the command refuse its own
target. The load-bearing protection is the identity firewall (observed login == `account_number`, plus
server). An account with no `account_number` is refused as ambiguous. This is the one deliberate departure from
a literal reading of "refuse inactive accounts" and is called out in the handoff.

## Consequences

- A later, separately-authorised live operation can make one symbol visible and then capture it with the
  existing account-scoped `sync_broker_instruments` — no new execution surface.
- The capability touches the live trading host when (and only when) `--commit` is run against production; this
  ADR and PR deliver it DARK (no deploy, no live call). The documented production sequence is in
  `docs/ENSURE_SYMBOL_VISIBLE_RUNBOOK.md` and must not be executed without a Sponsor GO.
- Risk retained: a future high-frequency Vantage-master use case needs its own latency/execution-safety
  certification and LIVE authorisation (out of scope here).
