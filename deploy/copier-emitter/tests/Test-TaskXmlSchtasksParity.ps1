# ASCII-only. Windows schtasks host-parity validator (ADR-0077; GFX-COPIER-PARITY-VALIDATOR-FIX-013). This is TEST
# TOOLING - it is NOT part of the vendored bundle, is never installed on a host by the installer, and must NEVER be
# run on the production Windows host. It proves, on a scratch/CI Windows box, that the reviewed task XML (the
# standalone template AND the installer-generated shape) is accepted by schtasks as a DISABLED, triggerless,
# least-privilege task with NO RestartOnFailure, and that a zero-duration RestartOnFailure interval is still rejected.
#
# Cross-edition native-exit handling (GFX-COPIER-HOST-EMITTER-DISABLED-REINSTALL-012, Defect 1): every schtasks call
# goes through Invoke-Schtasks, which sets $ErrorActionPreference='Continue' IN ITS OWN SCOPE so a non-zero schtasks
# exit is NEVER promoted to a terminating error - neither on Windows PowerShell 5.1 (where
# $PSNativeCommandUseErrorActionPreference does not exist) nor on PowerShell 7.4+ (where it defaults $true). Exit
# codes are read from $LASTEXITCODE. Cleanup of a missing temp task is a no-op, never a failure.
#
# Explicit resolvable principal (Defect 2): -Principal is REQUIRED and MUST map to a Windows SID BEFORE any task is
# created. There is NO fallback to the current user's domain/name (over SSH that resolved to WORKGROUP\administrator,
# an unmappable name that made schtasks reject otherwise-valid XML). Supply e.g. 'WIN-RD8VDS93DK7\guvfx_u_46' or any
# resolvable local account.
#
# Bounded and self-cleaning: a clearly non-production task name, invokes NO emitter, contacts NO MT5 / bridge /
# backend / broker, holds NO secret, and removes every artefact (temp task + temp files) on success and failure.
#
# Exit 0 = all parity checks passed. 2 = principal omitted/unresolvable (fail closed, no task created). 1 = a check failed.
[CmdletBinding()]
param(
    [string]$BundleDir = '',
    [string]$Principal = ''
)
$ErrorActionPreference = 'Stop'
$TASK = 'GuvFX_CopierEmitter_PARITYTEST_TEMP'   # clearly non-production; never an account id
$tempFiles = New-Object System.Collections.ArrayList
$ok = $true

function Note($m) { Write-Output $m }

# Resolve BundleDir in the body (NOT in the param default, where $PSScriptRoot can be empty under `powershell -File`).
if (-not $BundleDir) {
    if ($PSScriptRoot) { $BundleDir = Split-Path -Parent $PSScriptRoot } else { $BundleDir = (Get-Location).Path }
}

# Every native schtasks call goes through here. EAP='Continue' is set in THIS scope only, so a non-zero schtasks exit
# is never a terminating error on 5.1 or 7.4+. stderr is merged into the returned text; callers inspect .Code.
function Invoke-Schtasks([string[]]$SchtasksArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & schtasks @SchtasksArgs 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ Code = $code; Text = ($out | Out-String) }
}

# Remove the temp task if present; a missing task is a no-op (never throws, never fails the run) - Defect 1 fix.
function Remove-TempTask {
    Invoke-Schtasks @('/delete', '/tn', $TASK, '/f') | Out-Null
}

# Map a principal NAME to a SID string, or $null when it cannot be resolved - Defect 2 fix.
function Resolve-PrincipalSid([string]$Name) {
    if (-not $Name) { return $null }
    try {
        $nt = New-Object System.Security.Principal.NTAccount($Name)
        return $nt.Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        return $null
    }
}

# Installer-generated task XML shape (mirrors Install-GuvfxCopierEmitter.ps1 -Apply output, incl. ExecutionTimeLimit).
function Build-InstallerXml([string]$Princ) {
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>GuvFX Copy Trading emitter (account 999999) - DISABLED/DARK</Description></RegistrationInfo>
  <Triggers />
  <Principals><Principal id="Author"><UserId>$Princ</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <Enabled>false</Enabled>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>C:\Windows\System32\cmd.exe</Command><Arguments>"C:\NonExistent\parity.noop" run --config "C:\NonExistent\parity.cfg"</Arguments></Exec></Actions>
</Task>
"@
}

function New-TempFile([string]$Content, [string]$Tag, [string]$Ext) {
    $p = Join-Path $env:TEMP ("guvfx_parity_" + $Tag + "_" + [guid]::NewGuid().ToString('N') + "." + $Ext)
    [void]$tempFiles.Add($p)
    Set-Content -LiteralPath $p -Value $Content -Encoding Unicode
    return $p
}

# Create the temp task from an XML file, disable it, and return the read-back XML + create exit code.
function Create-And-Readback([string]$XmlPath) {
    Remove-TempTask
    $c = Invoke-Schtasks @('/create', '/tn', $TASK, '/xml', $XmlPath, '/f')
    Invoke-Schtasks @('/change', '/tn', $TASK, '/disable') | Out-Null
    $q = Invoke-Schtasks @('/query', '/tn', $TASK, '/xml', 'ONE')
    return [pscustomobject]@{ CreateCode = $c.Code; CreateText = $c.Text; Xml = $q.Text }
}

function Assert-ShapeOk($label, $res) {
    $disabled = ($res.Xml -match '<Enabled>false</Enabled>')
    $triggerless = ($res.Xml -notmatch '<Trigger>')
    $noRestart = ($res.Xml -notmatch 'RestartOnFailure')
    $noHighest = ($res.Xml -notmatch 'HighestAvailable')
    $created = ($res.CreateCode -eq 0)
    if (-not ($created -and $disabled -and $triggerless -and $noRestart -and $noHighest)) { $script:ok = $false }
    Note ("{0} created={1} disabled={2} triggerless={3} no_restartonfailure={4} no_highestavailable={5}" -f `
            $label, $created, $disabled, $triggerless, $noRestart, $noHighest)
    if (-not $created) { Note ("{0}_create_error={1}" -f $label, ($res.CreateText.Trim() -replace '\s+', ' ')) }
}

try {
    # 0. Fail closed unless an explicit, resolvable principal was supplied (Defect 2). No current-user fallback.
    $sid = Resolve-PrincipalSid $Principal
    if (-not $sid) {
        Note ("PRINCIPAL resolved=False value='{0}' - fail closed, no task created" -f $Principal)
        exit 2
    }
    Note ("PRINCIPAL resolved=True")

    # 1. Parse every reviewed bundle PowerShell file + this validator (AST, no execution).
    $ps1 = @(Get-ChildItem -Path $BundleDir -Filter *.ps1 -File)
    if ($PSCommandPath) { $ps1 += Get-Item -LiteralPath $PSCommandPath }
    foreach ($f in ($ps1 | Sort-Object FullName -Unique)) {
        $e = $null; $t = $null
        [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e) | Out-Null
        $n = if ($e) { $e.Count } else { 0 }
        if ($n -ne 0) { $ok = $false }
        Note ("PARSE  {0}  errors={1}" -f $f.Name, $n)
    }

    # 2. Positive control: a deliberately malformed script MUST report >0 parse errors (RULE 11).
    $badPs1 = New-TempFile 'function Broken { if ( $x -eq ' 'badctl' 'ps1'
    $eb = $null; $tb = $null
    [System.Management.Automation.Language.Parser]::ParseFile($badPs1, [ref]$tb, [ref]$eb) | Out-Null
    $nb = if ($eb) { $eb.Count } else { 0 }
    if ($nb -le 0) { $ok = $false }
    Note ("POSCTRL parser-detects-bad errors={0} (expect >0)" -f $nb)

    # 3. Corrected standalone TEMPLATE, placeholders substituted with harmless non-emitter values.
    $tpl = Get-Content -LiteralPath (Join-Path $BundleDir 'emitter_task_template.xml') -Raw
    $good = $tpl.Replace('{{ACCOUNT_ID}}', '999999').Replace('{{PRINCIPAL}}', $Principal).Replace('{{PYTHON_EXE}}', 'C:\Windows\System32\cmd.exe').Replace('{{ENTRYPOINT}}', 'C:\NonExistent\parity.noop').Replace('{{CONFIG_PATH}}', 'C:\NonExistent\parity.cfg')
    $tplPath = New-TempFile $good 'template' 'xml'
    Assert-ShapeOk 'TEMPLATE_XML' (Create-And-Readback $tplPath)

    # 4. Installer-generated XML shape (what Phase-5 -Apply registers).
    $insXml = Build-InstallerXml $Principal
    $insPath = New-TempFile $insXml 'installer' 'xml'
    Assert-ShapeOk 'INSTALLER_XML' (Create-And-Readback $insPath)

    # 5. Delete the temp task and prove it is gone.
    Remove-TempTask
    $goneq = Invoke-Schtasks @('/query', '/tn', $TASK)
    $gone = ($goneq.Code -ne 0)
    if (-not $gone) { $ok = $false }
    Note ("DELETED_AND_GONE gone={0}" -f $gone)

    # 6. Negative control: a zero-duration RestartOnFailure interval MUST be rejected by schtasks. The bad literal
    # lives ONLY here, in the test tooling - never in the vendored bundle.
    $zeroIv = 'PT' + '0M'
    $bad = $insXml -replace '</Settings>', ("  <RestartOnFailure><Interval>{0}</Interval><Count>0</Count></RestartOnFailure>`n  </Settings>" -f $zeroIv)
    $badPath = New-TempFile $bad 'negctl' 'xml'
    Remove-TempTask
    $neg = Invoke-Schtasks @('/create', '/tn', $TASK, '/xml', $badPath, '/f')
    $rejected = ($neg.Code -ne 0)
    if (-not $rejected) { $ok = $false }
    Note ("NEGCTRL zero_interval_rejected={0} (expect True)" -f $rejected)
}
catch {
    $ok = $false
    Note ("ERROR " + $_.Exception.Message)
}
finally {
    # Self-clean: always remove the temp task and any temp files, success or failure.
    try { Remove-TempTask } catch { }
    foreach ($tf in $tempFiles) { Remove-Item -LiteralPath $tf -Force -ErrorAction SilentlyContinue }
}

Note ("RESULT parity_all_checks_passed={0}" -f $ok)
if ($ok) { exit 0 } else { exit 1 }
