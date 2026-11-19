@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ===========================================================================
REM GuvFX TIM isolated winter-timezone bridge STOP launcher.
REM Packet: GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121. ASCII only (RULE 9).
REM
REM Thin, no-argument wrapper (NO token needed). Invokes the reviewed stdlib
REM controller, which validates the dedicated state file and terminates ONLY the
REM bridge/terminal PIDs it created, each after a live-identity re-check (PID
REM reuse is refused). It never kills by process name, never touches the
REM production :8788 bridge/terminal, and refuses on missing/malformed/
REM identity-mismatched PID evidence.
REM
REM Exit codes: 10 argument supplied / 12 controller absent / 13 python
REM unavailable; else the controller's code (non-zero = refused/failed).
REM ===========================================================================

if not "%~1"=="" (
    echo [tim-isolated-stop] ERROR: no arguments are accepted.>&2
    endlocal
    exit /b 10
)

set "CTL=C:\GuvFX\TIM\winter-tz-probe\tim_isolated_bridge_ctl.py"

if not exist "%CTL%" (
    echo [tim-isolated-stop] ERROR: controller not found.>&2
    endlocal
    exit /b 12
)

where python >nul 2>&1
if errorlevel 1 (
    echo [tim-isolated-stop] ERROR: python not available.>&2
    endlocal
    exit /b 13
)

python "%CTL%" stop
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
