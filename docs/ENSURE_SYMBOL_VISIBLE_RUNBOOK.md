# Runbook — `ensure_symbol_visible` (account-scoped, execution-free symbol visibility)

Companion to ADR-0063. Makes **one** MT5 symbol visible in **one** account's OWN identity-verified terminal
so a later account-scoped catalogue sync can capture it. It places **no** order and creates **no**
`ExecutionJob`/`Trade`/copier record. Copy Trading execution stays OFF throughout.

> **STOP conditions (fail-closed — the command refuses and exits non-zero):** unresolved/not-READY bridge;
> observed MT5 login ≠ `account_number` or server mismatch; stale/absent identity; symbol absent on the
> terminal; blank/whitespace/wildcard/list/bulk symbol; bridge timeout; bridge refusal; postcondition not
> proven (`after_visible` not true). Never restart the terminal/bridge to "fix" a STOP — investigate the
> terminal/bridge boundary first (a headless terminal has no GUI; the operator and the bridge may be looking
> at different sessions).

## Prerequisites

- `GUVFX_WINDOWS_AGENT_TOKEN` set in the backend environment (same credential `sync_broker_instruments` uses).
- The account has a READY per-tenant `HostedExecutionEndpoint` and a provisioned Windows identity.
- Run inside the backend container: `docker exec -i guvfx-backend python manage.py …`.

## Dry-run (default — writes nothing, sends no mutation)

```bash
python manage.py ensure_symbol_visible --account 50 --symbol XAUUSD.std
```

Expected output (JSON): `{"mode": "dry-run", "would_change": <bool>, "account": 50, "login": "***3832",
"server": "IS6Technologies-Live", "symbol": "XAUUSD.std", "before_visible": <bool>, "trade_mode": 4}`.
`would_change=false` means the symbol is already visible (nothing to do).

## Commit (performs the reveal — requires a Sponsor GO)

```bash
python manage.py ensure_symbol_visible --account 50 --symbol XAUUSD.std --commit
```

The command: proves identity → `POST /mt5/ensure-symbol-visible {"symbol": "XAUUSD.std"}` on the account's
OWN bridge → proves identity AGAIN → proves `after_visible=true`. Output:
`{"mode": "commit", "changed": <bool>, "after_visible": true, …}`. Idempotent: re-running on an
already-visible symbol returns `changed=false`.

## Identity proof

Every run prints the masked login (`***<last4>`) and server it verified. Confirm they match the intended
account (e.g. `***3832` / `IS6Technologies-Live` for #50) before trusting a commit result.

## Rollback

Visibility is reversible (`symbol_select(symbol, False)` on the same terminal); the reveal creates no
persistent GuvFX state. If a catalogue sync was run afterward, `sync_broker_instruments` marks rows
(`enabled=False`), never deletes, so reverting the Market-Watch change and re-syncing restores the prior
catalogue state. Capture the pre-sync `BrokerInstrument` export (per account) before any sync.

## Documented production sequence (DO NOT EXECUTE without a Sponsor GO — ADR-0063)

1. Capture fresh recovery/rollback evidence (pre-sync `BrokerInstrument` export for the account).
2. Confirm account identity and exactly ONE terminal instance for it.
3. `ensure_symbol_visible --account <id> --symbol <SYM>`  (dry-run).
4. `ensure_symbol_visible --account <id> --symbol <SYM> --commit`  (one bounded reveal).
5. Verify `after_visible=true` and `trade_mode=FULL`.
6. Verify zero new jobs/orders/trades and all execution flags OFF.
7. `sync_broker_instruments --account <id>`  (account-scoped catalogue capture).
8. Reconcile the catalogue against the live symbols.
9. Proceed to directional mapping review ONLY under separate authorization.
