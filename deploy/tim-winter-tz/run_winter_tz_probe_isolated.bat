@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ===========================================================================
REM GuvFX TIM ISOLATED winter-timezone probe launcher.
REM Packet: GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121. ASCII only (RULE 9).
REM
REM Thin, no-argument opaque wrapper. Loads GUVFX_AGENT_TOKEN via the bridge's
REM own governed secrets loader (opaque; never typed/echoed/persisted; the file
REM is never read or printed) and runs the reviewed read-only probe against the
REM ISOLATED loopback bridge only: base URL is hard-coded to
REM http://127.0.0.1:8977 (the probe also refuses any non-loopback endpoint) and
REM the expected server is hard-coded to TradersWay-Demo. DEMO classification and
REM server confirmation are fail-closed. No trading capability.
REM
REM Exit codes: 10 argument supplied / 11 loader absent / 12 probe absent /
REM 13 python unavailable / 14 token unavailable; else the probe's own code.
REM ===========================================================================

if not "%~1"=="" (
    echo [tim-isolated-probe] ERROR: no arguments are accepted.>&2
    endlocal
    exit /b 10
)

set "SECRETS_LOADER=C:\GuvFX\secrets\bridge.tokens.bat"
set "PROBE=C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py"

if not exist "%SECRETS_LOADER%" (
    echo [tim-isolated-probe] ERROR: secrets loader not found.>&2
    endlocal
    exit /b 11
)

if not exist "%PROBE%" (
    echo [tim-isolated-probe] ERROR: probe not found.>&2
    endlocal
    exit /b 12
)

where python >nul 2>&1
if errorlevel 1 (
    echo [tim-isolated-probe] ERROR: python not available.>&2
    endlocal
    exit /b 13
)

call "%SECRETS_LOADER%"

if not defined GUVFX_AGENT_TOKEN (
    echo [tim-isolated-probe] ERROR: token unavailable after loader.>&2
    endlocal
    exit /b 14
)

set "TIM_WINTER_TZ_PROBE_ON_HOST=1"
python "%PROBE%" --execute --expect-server TradersWay-Demo --base-url http://127.0.0.1:8977
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
