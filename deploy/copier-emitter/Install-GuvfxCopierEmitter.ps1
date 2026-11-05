# ASCII-only. GuvFX Copy Trading host emitter installer (ADR-0076). Repository artifact; DARK.
# Registers an account-scoped scheduled task that is DISABLED (no trigger, no watchdog, no auto-restart) so the
# emitter is installed INERT: it can only run via an explicit, separately-authorised operator start. Dry-run by
# default (PLAN unless -Apply). Refuses reserved account ids. One compact JSON object is the final line.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$AccountId,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$EntryPoint,
    [string]$PythonExe = "python",
    [string]$RunAsUser = "",
    [switch]$Apply
)
$ErrorActionPreference = "Stop"

function Emit($obj) { $obj | ConvertTo-Json -Compress }
function Fail($reason) { Emit(@{ ok = $false; reason = [string]$reason }); exit 1 }

if ($AccountId -le 1) { Fail "account_id_reserved" }
if (-not (Test-Path -LiteralPath $ConfigPath)) { Fail "config_missing" }
if (-not (Test-Path -LiteralPath $EntryPoint)) { Fail "entrypoint_missing" }

$taskName = "GuvFX_CopierEmitter_$AccountId"
$planOnly = (-not $Apply)

# Snapshot any existing task definition for rollback (never overwrite without a recoverable copy).
$existingXml = ""
try { $existingXml = (& schtasks /query /tn "\$taskName" /xml ONE 2>$null) -join "`n" } catch { $existingXml = "" }

if ($planOnly) {
    Emit(@{ ok = $true; plan = $true; task = $taskName; account_id = $AccountId;
            action = "would_register_disabled"; had_existing = [bool]$existingXml; state = "disabled_stopped" })
    exit 0
}

# Build a DISABLED task: a single action, NO <Triggers>, Settings.Enabled=false, no restart-on-failure.
# XML-escape every operator-supplied value ('&' '<' '>' are legal in Windows paths and would break the XML).
function Get-XmlEscaped($s) { return ([string]$s).Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;") }
$exeEsc = Get-XmlEscaped $PythonExe
$argsEsc = Get-XmlEscaped ('"' + $EntryPoint + '" run --config "' + $ConfigPath + '"')
$principalRaw = if ($RunAsUser) { $RunAsUser } else { "$env:USERDOMAIN\$env:USERNAME" }
$principalEsc = Get-XmlEscaped $principalRaw
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>GuvFX Copy Trading emitter (account $AccountId) - DISABLED/DARK</Description></RegistrationInfo>
  <Triggers />
  <Principals><Principal id="Author"><UserId>$principalEsc</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <Enabled>false</Enabled>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Interval>PT0M</Interval><Count>0</Count></RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>$exeEsc</Command><Arguments>$argsEsc</Arguments></Exec></Actions>
</Task>
"@

$xmlPath = Join-Path $env:TEMP ("guvfx_copier_emitter_" + $AccountId + ".xml")
Set-Content -LiteralPath $xmlPath -Value $taskXml -Encoding Unicode
try {
    & schtasks /create /tn "\$taskName" /xml "$xmlPath" /f | Out-Null
    & schtasks /change /tn "\$taskName" /disable | Out-Null
    # Locale-independent verify: read back the registered task XML and confirm it is DISABLED (RULE 11 - never
    # parse a localised status string). Fail closed otherwise.
    $verifyXml = (& schtasks /query /tn "\$taskName" /xml ONE 2>$null) -join "`n"
    if ($verifyXml -notmatch "<Enabled>false</Enabled>") { throw "post_install_not_disabled" }
}
catch {
    # Rollback-first: restore the previous task definition if one existed, else remove the partial task.
    $reason = $_.Exception.Message
    try { & schtasks /delete /tn "\$taskName" /f 2>$null | Out-Null } catch { }
    if ($existingXml) {
        $restorePath = Join-Path $env:TEMP ("guvfx_copier_emitter_restore_" + $AccountId + ".xml")
        Set-Content -LiteralPath $restorePath -Value $existingXml -Encoding Unicode
        try { & schtasks /create /tn "\$taskName" /xml "$restorePath" /f 2>$null | Out-Null } catch { }
        Remove-Item -LiteralPath $restorePath -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $xmlPath -ErrorAction SilentlyContinue
    Fail ("install_failed_rolled_back:" + $reason)
}
Remove-Item -LiteralPath $xmlPath -ErrorAction SilentlyContinue
Emit(@{ ok = $true; apply = $true; task = $taskName; account_id = $AccountId; state = "disabled_stopped";
        had_existing = [bool]$existingXml })
exit 0
