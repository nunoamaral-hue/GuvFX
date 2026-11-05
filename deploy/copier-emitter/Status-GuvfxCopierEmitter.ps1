# ASCII-only. GuvFX Copy Trading host emitter status probe (ADR-0076). Repository artifact; DARK.
# READ-ONLY: reports the account-scoped task's presence and state. Writes nothing, launches nothing, reads no
# positions. One compact JSON object is the final line.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$AccountId
)
$ErrorActionPreference = "Stop"

function Emit($obj) { $obj | ConvertTo-Json -Compress }
if ($AccountId -le 1) { Emit(@{ ok = $false; reason = "account_id_reserved" }); exit 1 }

$taskName = "GuvFX_CopierEmitter_$AccountId"
$present = $false
$state = "absent"
try {
    # Locale-independent: read the registered task XML and check <Enabled> (never parse a localised status string).
    $xml = (& schtasks /query /tn "\$taskName" /xml ONE 2>$null) -join "`n"
    if ($LASTEXITCODE -eq 0 -and $xml) {
        $present = $true
        if ($xml -match "<Enabled>false</Enabled>") { $state = "disabled" } else { $state = "enabled" }
    }
}
catch { $present = $false; $state = "query_error" }

Emit(@{ ok = $true; task = $taskName; account_id = $AccountId; present = $present; state = $state })
exit 0
