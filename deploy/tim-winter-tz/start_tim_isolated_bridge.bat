@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ===========================================================================
REM GuvFX TIM isolated winter-timezone bridge START launcher.
REM Packet: GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121. ASCII only (RULE 9).
REM
REM Thin, no-argument opaque wrapper: loads GUVFX_AGENT_TOKEN via the bridge's
REM own governed secrets loader (opaque; never typed/echoed/persisted; the file
REM is never read or printed) and invokes the reviewed stdlib controller, which
REM starts ONLY the dormant account_001 terminal + an isolated loopback-only,
REM read-only (HTTP-only) bridge on 127.0.0.1:8977. It never touches the
REM production :8788 bridge/terminal or any live tenant. No trading capability.
REM
REM Exit codes: 10 argument supplied / 11 loader absent / 12 controller absent /
REM 13 python unavailable / 14 token unavailable; else the controller's code.
REM ===========================================================================

if not "%~1"=="" (
    echo [tim-isolated-start] ERROR: no arguments are accepted.>&2
    endlocal
    exit /b 10
)

set "SECRETS_LOADER=C:\GuvFX\secrets\bridge.tokens.bat"
set "CTL=C:\GuvFX\TIM\winter-tz-probe\tim_isolated_bridge_ctl.py"

if not exist "%SECRETS_LOADER%" (
    echo [tim-isolated-start] ERROR: secrets loader not found.>&2
    endlocal
    exit /b 11
)

if not exist "%CTL%" (
    echo [tim-isolated-start] ERROR: controller not found.>&2
    endlocal
    exit /b 12
)

where python >nul 2>&1
if errorlevel 1 (
    echo [tim-isolated-start] ERROR: python not available.>&2
    endlocal
    exit /b 13
)

call "%SECRETS_LOADER%"

if not defined GUVFX_AGENT_TOKEN (
    echo [tim-isolated-start] ERROR: token unavailable after loader.>&2
    endlocal
    exit /b 14
)

python "%CTL%" start
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
