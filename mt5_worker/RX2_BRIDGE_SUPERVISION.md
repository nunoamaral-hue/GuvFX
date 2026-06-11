# RX-2B — Windows Bridge Supervision Endpoint (`GET /mt5/supervision`)

The MT5 signal bridge (`C:\GuvFX\mt5_signal_bridge.py`) runs only on the Windows
host and is not otherwise in version control. This directory holds the canonical,
source-controlled material to (re)apply the reliability supervision endpoint.

## What the endpoint provides
`GET /mt5/supervision` (auth header `X-GuvFX-Agent-Token`) — read-only MT5 state
consumed by the backend `reliability` app (RX-2B):
```json
{ "ok": true, "server_up": true, "mt5_initialized": true, "broker_connected": true,
  "trade_allowed": true, "account_login": 1121106, "equity": 9998.19,
  "last_tick_age_s": 0, "server_time": 1781191405 }
```
`/health` is unchanged (the `GuvFX_BridgeWatchdog` task depends on its unconditional
`{"ok": true}`). The order/rates path is untouched.

## Apply / re-apply (idempotent)
```powershell
"C:\Program Files\Python311\python.exe" C:\GuvFX\bridge_supervision_patch.py
# -> PATCHED_OK   (or ALREADY_PATCHED on re-run; a timestamped .bak.rx2_* is made)
```
The patch: (1) adds the `/mt5/supervision` route just before the `/health` route,
(2) defines `_rx2_supervision_snapshot()` **before** the blocking
`if __name__ == "__main__":` server start, importing `MetaTrader5` locally
(the bridge imports it per-handler, not at module level).

## Restart + verify
```powershell
# restart only the 8788 bridge (terminal stays up); watchdog also restarts it
$p=(Get-NetTCPConnection -LocalPort 8788 -State Listen).OwningProcess | Select -First 1
Stop-Process -Id $p -Force; Start-Sleep 3
Start-Process -FilePath C:\GuvFX\guvfx_autostart_bridge_only.bat -WindowStyle Minimized
```
Then (from the Linux worker container, with `$GUVFX_WINDOWS_AGENT_TOKEN`):
```sh
curl -s -H "X-GuvFX-Agent-Token: $T" http://100.79.101.19:8788/mt5/supervision
curl -s -H "X-GuvFX-Agent-Token: $T" http://100.79.101.19:8788/health   # still ok:true
```

## Deploying `bridge_supervision_patch.py` to the host
Copy this file to the host build location and keep it beside the bridge:
```sh
scp mt5_worker/bridge_supervision_patch.py Administrator@<windows-tailscale-ip>:C:/GuvFX/
```
Re-run after any future bridge replacement; it is safe to run unconditionally
(idempotent). Supersedes the earlier fragmented patch scripts.
