@echo off
REM ===========================================================================
REM GuvFX Node 2 (Closed-Beta) per-node pin-enforcing order bridge launcher.
REM TEMPORARY shared-token beta exception (see README.md). ASCII only (RULE 9).
REM Customer Zero's :8788 bridge / tasks / watchdog / secrets are NOT modified.
REM ===========================================================================

set GUVFX_API_URL=https://api.guvfx.com

REM --- Shared-token exception (documented, temporary): reuse CZ's inbound + worker
REM     tokens. This is the ONLY line that reads CZ's secrets file, read-only. ---
call "C:\GuvFX\secrets\bridge.tokens.bat"

REM --- Account-specific config, written by Activate-Node2Bridge.ps1 at materialise
REM     time (MT5_ACCOUNT_ID / MT5_TERMINAL_PATH / MT5_EXPECTED_LOGIN /
REM     MT5_EXPECTED_SERVER). The bridge REFUSES to start if these are absent. ---
if not exist "C:\GuvFX\node2\node2_bridge.env.bat" (
    echo [node2] node2_bridge.env.bat missing - run Activate-Node2Bridge.ps1 first. Not starting.
    exit /b 1
)
call "C:\GuvFX\node2\node2_bridge.env.bat"

REM --- Fixed safety posture: distinct port, mandatory pin, guarded attach, DEMO only.
REM     MT5_ALLOW_LIVE is intentionally UNSET and must never be set here. ---
set HTTP_SERVER_PORT=8789
set MT5_REQUIRE_IDENTITY_PIN=1
set MT5_GUARDED_ATTACH=1
set POLL_INTERVAL_SECONDS=2

REM --- Bind-guard: never start a 2nd node2 bridge if :8789 is already owned. ---
netstat -ano | find ":8789" | find "LISTENING" >nul && (
    echo [node2] bridge already listening on 8789 - skipping start.
    exit /b 0
)

echo [node2] starting Closed-Beta pin-enforcing bridge on port %HTTP_SERVER_PORT% (account %MT5_ACCOUNT_ID%)
"C:\Program Files\Python311\python.exe" "C:\GuvFX\node2\mt5_signal_bridge.py"
