@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ===========================================================================
REM GuvFX TIM winter broker-timezone probe launcher.
REM Packet: GFX-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011. ASCII only (RULE 9).
REM
REM Runs the already-merged, read-only winter-timezone probe on the bridge host,
REM reusing the bridge's OWN governed secrets loader to populate this process's
REM GUVFX_AGENT_TOKEN opaquely. The token value is never typed, pasted, echoed,
REM copied, persisted, or reconstructed; the secrets file is never read or
REM printed. No trading / order / withdrawal / NAS / dataset / deployment /
REM persistence capability. Does not start, restart, or stop the bridge, MT5,
REM any task, or any service. Fixed paths only; accepts no arguments.
REM
REM Exit codes:
REM   10  an argument was supplied (none are accepted)
REM   11  the governed secrets loader is absent
REM   12  the governed installed probe is absent
REM   13  python is unavailable
REM   14  the token is still unavailable after the opaque loader call
REM   else  the probe's own exit code, propagated unchanged
REM ===========================================================================

REM --- Reject any argument (fixed, non-configurable behaviour) ---
if not "%~1"=="" (
    echo [tim-winter-tz-launcher] ERROR: no arguments are accepted.>&2
    endlocal
    exit /b 10
)

set "SECRETS_LOADER=C:\GuvFX\secrets\bridge.tokens.bat"
set "PROBE=C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py"

REM --- Fail closed: the governed secrets loader must exist ---
if not exist "%SECRETS_LOADER%" (
    echo [tim-winter-tz-launcher] ERROR: secrets loader not found.>&2
    endlocal
    exit /b 11
)

REM --- Fail closed: the governed installed probe must exist ---
if not exist "%PROBE%" (
    echo [tim-winter-tz-launcher] ERROR: probe not found.>&2
    endlocal
    exit /b 12
)

REM --- Fail closed: python must be available ---
where python >nul 2>&1
if errorlevel 1 (
    echo [tim-winter-tz-launcher] ERROR: python not available.>&2
    endlocal
    exit /b 13
)

REM --- Load the token opaquely via the bridge's own governed loader. It sets
REM     GUVFX_AGENT_TOKEN inside THIS process environment. We never read or
REM     print the file and never echo the token value. ---
call "%SECRETS_LOADER%"

REM --- Verify token presence value-blindly (presence only, never the value) ---
if not defined GUVFX_AGENT_TOKEN (
    echo [tim-winter-tz-launcher] ERROR: token unavailable after loader.>&2
    endlocal
    exit /b 14
)

REM --- On-host confirmation + run the read-only probe (DEMO, fixed server). ---
set "TIM_WINTER_TZ_PROBE_ON_HOST=1"
python "%PROBE%" --execute --expect-server TradersWay-Demo
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
