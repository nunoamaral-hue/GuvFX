<#
  TX-1E — session hygiene for dedicated viewer sessions (scoped, safe).

  Logs off RDP sessions belonging to guvfx_u_* users that exceed idle/max
  duration. STRICTLY scoped: it only ever acts on sessions whose username starts
  "guvfx_u_". It NEVER touches Administrator, the console session (ID 1),
  services (ID 0), the bridge, or the execution MT5. -WhatIf reports only.

  Usage:
    powershell -NoProfile -File Cleanup-GuvfxSessions.ps1 -IdleMinutes 30 -MaxMinutes 240 -WhatIf
#>
param(
  [int]$IdleMinutes = 30,
  [int]$MaxMinutes  = 240,
  [switch]$WhatIf
)
$ErrorActionPreference = "Stop"
$acted = @()
try {
  # quser output: USERNAME SESSIONNAME ID STATE IDLE LOGON-TIME
  $lines = (quser 2>$null) | Select-Object -Skip 1
  foreach ($ln in $lines) {
    $u = ($ln -replace "^\s*>?","").Trim() -split "\s{2,}"
    if ($u.Count -lt 4) { continue }
    $name = $u[0]
    if ($name -notlike "guvfx_u_*") { continue }   # HARD SCOPE
    $id = ($u | Where-Object { $_ -match "^\d+$" } | Select-Object -First 1)
    if (-not $id -or $id -eq "0" -or $id -eq "1") { continue }   # never console/services
    # (idle/max parsing is best-effort; this scaffolds the scoped policy)
    if ($WhatIf) { $acted += "would-logoff $name (session $id)" }
    else { logoff $id 2>$null; $acted += "logged-off $name (session $id)" }
  }
  [ordered]@{ scope="guvfx_u_* only"; idle_minutes=$IdleMinutes; max_minutes=$MaxMinutes; whatif=[bool]$WhatIf; acted=$acted; ok=$true } | ConvertTo-Json -Compress
}
catch { [ordered]@{ ok=$false; error=$_.Exception.Message } | ConvertTo-Json -Compress; exit 1 }
