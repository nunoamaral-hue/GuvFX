$ErrorActionPreference = "Stop"
$bat = "C:\GuvFX\guvfx_autostart.bat"
Copy-Item $bat "$bat.bak.rx3a" -Force
$c = Get-Content $bat -Raw

$mt5old = 'start "" "C:\GuvFX\terminals\account_001\instance\terminal64.exe" /portable'
$mt5new = 'tasklist /FI "IMAGENAME eq terminal64.exe" | find /I "terminal64.exe" >NUL && (echo [%date% %time%] RX-3A: MT5 already running - skip >> %LOGFILE%) || (echo [%date% %time%] RX-3A: launching MT5 >> %LOGFILE% & start "" "C:\GuvFX\terminals\account_001\instance\terminal64.exe" /portable)'

$brold = '"C:\Program Files\Python311\python.exe" C:\GuvFX\mt5_signal_bridge.py >> %LOGFILE% 2>&1'
$brnew = 'netstat -ano | find ":8788" | find "LISTENING" >NUL && (echo [%date% %time%] RX-3A: bridge 8788 already active - skip >> %LOGFILE%) || (echo [%date% %time%] RX-3A: launching bridge >> %LOGFILE% & "C:\Program Files\Python311\python.exe" C:\GuvFX\mt5_signal_bridge.py >> %LOGFILE% 2>&1)'

$n1 = $c.Contains($mt5old)
$n2 = $c.Contains($brold)
$c = $c.Replace($mt5old, $mt5new).Replace($brold, $brnew)
Set-Content $bat $c -Encoding ASCII
Write-Output ("mt5_marker_found=" + $n1 + " bridge_marker_found=" + $n2)
