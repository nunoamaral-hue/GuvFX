@echo off
REM ===========================================================================
REM Node 2 bridge account-specific config TEMPLATE. Do NOT edit by hand.
REM Activate-Node2Bridge.ps1 writes C:\GuvFX\node2\node2_bridge.env.bat from this
REM template, substituting the four __PLACEHOLDER__ values at materialise time.
REM All four identify the BETA tenant's demo account (never Customer Zero / id 1).
REM ===========================================================================
set MT5_ACCOUNT_ID=__ACCOUNT_ID__
set MT5_TERMINAL_PATH=__TERMINAL_PATH__
set MT5_EXPECTED_LOGIN=__EXPECTED_LOGIN__
set MT5_EXPECTED_SERVER=__EXPECTED_SERVER__
