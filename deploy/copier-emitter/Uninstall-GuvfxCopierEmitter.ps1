# ASCII-only. GuvFX Copy Trading host emitter uninstaller (ADR-0076). Repository artifact; DARK.
# Account-scoped, idempotent, reversible teardown: removes ONLY this account's emitter task (never by image,
# never a broad recursive delete, never another account's unit). Dry-run by default. One compact JSON final line.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$AccountId,
    [switch]$Apply
)
$ErrorActionPreference = "Stop"

function Emit($obj) { $obj | ConvertTo-Json -Compress }
function Fail($reason) { Emit(@{ ok = $false; reason = [string]$reason }); exit 1 }

if ($AccountId -le 1) { Fail "account_id_reserved" }
$taskName = "GuvFX_CopierEmitter_$AccountId"

$present = $false
try { $null = (& schtasks /query /tn "\$taskName" 2>$null); $present = ($LASTEXITCODE -eq 0) } catch { $present = $false }

if (-not $Apply) {
    Emit(@{ ok = $true; plan = $true; task = $taskName; present = $present; action = "would_remove_task_only" })
    exit 0
}

$removed = "absent"
if ($present) {
    try { & schtasks /end /tn "\$taskName" 2>$null | Out-Null } catch { }
    try {
        & schtasks /delete /tn "\$taskName" /f | Out-Null
        $removed = "ok"
    }
    catch { Fail "task_delete_failed" }
}

# Confirm no residual task remains (idempotent success).
$still = $false
try { $null = (& schtasks /query /tn "\$taskName" 2>$null); $still = ($LASTEXITCODE -eq 0) } catch { $still = $false }
if ($still) { Fail "task_residual" }
Emit(@{ ok = $true; apply = $true; task = $taskName; removed = $removed; residual = $false })
exit 0
