# Node 2 per-node order bridge (Closed-Beta, temporary shared-token exception)

Stands up the **beta** pin-enforcing MT5 order bridge for `TerminalNode(pk=2)` so the first
supervised beta user can complete the end-to-end acceptance journey (register -> ... -> first
Wayond DEMO trade). It is the operational activation of the ADR-0046 per-node order-transport
seam on a real beta node.

**This is a bounded, TEMPORARY Closed-Beta implementation, not the long-term architecture.**
Approved by the Sponsor (2026-08-15) solely to unblock Beta User #1's acceptance journey.
After Closed Beta validates the product, the per-node inbound-token model (a distinct token per
node, wired through `TerminalNode` + the dispatch worker) replaces the shared-token exception as
Post-Beta engineering, and this package retires with the ADR-0044 co-residency posture.

## What it is

- A **second** `mt5_signal_bridge.py` process (the current, pin-capable repo build), living in its
  own directory `C:\GuvFX\node2\`, listening on its **own port `:8789`**, supervised by its **own
  watchdog** and scheduled tasks. **Customer Zero's `:8788` bridge, its binary, its tasks, its
  watchdog, and its secrets file are never modified** - this package only *reads* the shared token.
- Runs pin-enforcing + demo-only + guarded-attach: `MT5_REQUIRE_IDENTITY_PIN=1`,
  `MT5_GUARDED_ATTACH=1`, `MT5_ALLOW_LIVE` unset. Every order must carry the server-derived identity
  pin (`expected_login`/`expected_server`) or it fails closed; it never launches a terminal and never
  logs in.

## Shared-token exception (documented, temporary)

The dispatch worker (`mt5_trade_ingest_worker`) currently holds one global inbound agent token
(`GUVFX_AGENT_TOKEN` / `GUVFX_WINDOWS_AGENT_TOKEN`) and sends it to whichever per-node URL it
resolves. Until the per-node inbound-token model is built (Post-Beta), the node 2 bridge **reuses
Customer Zero's inbound + worker tokens** by calling `C:\GuvFX\secrets\bridge.tokens.bat`. This is
the substitution Security RULE 3 normally forbids; it is accepted here as an **explicit, recorded,
time-boxed Closed-Beta exception** (see `docs/SECRET_INVENTORY.md`) because:

- routing (`execution.order_transport`) sends a beta order ONLY to node 2's `:8789` and a Customer
  Zero order ONLY to the global `:8788` - neither can reach the other; and
- the per-job **identity pin** is the real per-tenant order authority: the bridge re-reads live
  `account_info()`/`terminal_info()` and refuses unless the attached terminal is the tenant's own
  demo account. The shared token is only transport auth on the same trusted host.

## Why it can only be fully activated at materialise time

The bridge's whole identity is the tenant's account: `MT5_ACCOUNT_ID` scopes which jobs it claims,
`MT5_TERMINAL_PATH` is the exact tenant terminal it attaches to, and `MT5_EXPECTED_LOGIN` /
`MT5_EXPECTED_SERVER` are the demo account it verifies. Those four values only exist once the beta
user has registered and his workspace has materialised (`derive_slot` ->
`C:\GuvFX\accounts\<account_id>\terminal`). The bridge refuses to start without them. So this
package is **staged** ahead of time; the single account-specific **activation** happens when the
workspace materialises.

## CZ-safety by construction

Configured entirely with the **beta** account's identity (id != 1, its own terminal, its own login),
the node 2 bridge can only ever poll/execute the beta account's jobs and attach to the beta
terminal. It never claims Customer Zero's account-1 jobs (the poll is account-scoped) and never
attaches to CZ's terminal (the path is fixed to the tenant's). The watchdog is **port-specific**: it
only ever restarts the process bound to `:8789` and NEVER issues a blanket `python` kill, so it
cannot touch Customer Zero's bridge.

## Files

| File | Role |
|------|------|
| `start_node2_bridge.bat` | Launcher: sets pin/guarded/demo + port 8789, reuses CZ tokens (exception), runs the node2 binary. Bind-guarded against a double start. |
| `node2_bridge.env.template.bat` | Template for the 4 account-specific values; `Activate-Node2Bridge.ps1` fills it at materialise time. |
| `node2_bridge_watchdog.ps1` | Port-specific watchdog for `:8789` only. Never a blanket python kill. |
| `Activate-Node2Bridge.ps1` | Materialise-time activation: writes the env, registers the tasks, starts the bridge, verifies `/health`. |
| `Deactivate-Node2Bridge.ps1` | Clean teardown: stops + unregisters the node2 tasks and stops the process (full reversibility). CZ untouched. |

## Activation (materialise time - one command)

Run on the host once the beta user's workspace has materialised and his demo terminal is running +
logged in:

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\GuvFX\node2\Activate-Node2Bridge.ps1 `
  -AccountId <beta_trading_account_id> `
  -TerminalPath "C:\GuvFX\accounts\<beta_trading_account_id>\terminal\terminal64.exe" `
  -ExpectedLogin "<beta_demo_login>" `
  -ExpectedServer "<beta_demo_server>"
```

`TerminalNode(pk=2).order_bridge_base_url` is set to `http://100.79.101.19:8789` (done at staging).

## Retirement

`powershell ... -File C:\GuvFX\node2\Deactivate-Node2Bridge.ps1` stops and removes the node 2 bridge
and its tasks; clear `TerminalNode(pk=2).order_bridge_base_url` to revert routing. Nothing about
Customer Zero changes.
