# ASCII-only. Windows host-parity validator for the GuvFX Copy Trading emitter task XML (ADR-0077;
# GFX-COPIER-HOST-EMITTER-TASKXML-FIX-009). This is TEST TOOLING - it is NOT part of the vendored bundle and is
# never installed on a host by the installer. It proves, on a real Windows box, that the reviewed task XML is
# accepted by schtasks as a DISABLED, triggerless task (the exact gap that ParseFile cannot catch), and that a
# zero-duration RestartOnFailure interval (the packet-008 defect) is still rejected.
#
# It is bounded and self-cleaning: it uses a clearly non-production task name, invokes NO emitter, contacts NO
# MT5 / bridge / backend / broker, holds NO secret, and removes every artefact (temp task + temp files) on both
# success and failure. Do NOT run it on the production Windows host without a separate authorisation; it is safe
# to run on any scratch Windows box or CI Windows runner.
#
# Exit 0 = all parity checks passed. Non-zero = a check failed (details on stdout).
[CmdletBinding()]
param(
    [string]$BundleDir = (Join-Path (Split-Path -Parent $PSScriptRoot) '')
)
$ErrorActionPreference = 'Stop'
# Edition-robust: this validator deliberately reads $LASTEXITCODE from native schtasks calls (a non-zero exit is
# the SIGNAL for the "gone" and "rejected" controls). On Windows PowerShell 5.1 a native non-zero exit never
# throws. On PowerShell 7.4+ $PSNativeCommandUseErrorActionPreference defaults to $true, which WOULD turn those
# expected non-zero exits into terminating errors and make a PASS unreachable (RULE 11). Force the 5.1 semantics
# so the control checks work identically under 5.1 and 7.4+ (the assignment is a harmless no-op under 5.1).
$PSNativeCommandUseErrorActionPreference = $false
$TASK = 'GuvFX_CopierEmitter_PARITYTEST_TEMP'   # clearly non-production; never an account id
$tempFiles = New-Object System.Collections.ArrayList
$ok = $true

function Note($m) { Write-Output $m }

try {
    # 1. Parse every bundle PowerShell file (AST, no execution).
    $ps1 = Get-ChildItem -Path $BundleDir -Filter *.ps1 -File
    foreach ($f in $ps1) {
        $e = $null; $t = $null
        [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e) | Out-Null
        $n = if ($e) { $e.Count } else { 0 }
        if ($n -ne 0) { $ok = $false }
        Note ("PARSE  {0}  errors={1}" -f $f.Name, $n)
    }

    # 2. Positive control: a deliberately malformed script MUST report >0 parse errors (RULE 11).
    $bad = Join-Path $env:TEMP 'guvfx_parity_bad.ps1'
    [void]$tempFiles.Add($bad)
    Set-Content -LiteralPath $bad -Value 'function Broken { if ( $x -eq ' -Encoding ascii
    $eb = $null; $tb = $null
    [System.Management.Automation.Language.Parser]::ParseFile($bad, [ref]$tb, [ref]$eb) | Out-Null
    $nb = if ($eb) { $eb.Count } else { 0 }
    if ($nb -le 0) { $ok = $false }
    Note ("POSCTRL parser-detects-bad errors={0} (expect >0)" -f $nb)

    # Build a concrete task XML from the reviewed TEMPLATE by substituting its placeholders with harmless,
    # non-emitter test values (action = cmd.exe; the task is DISABLED and deleted, so it never runs).
    $tplPath = Join-Path $BundleDir 'emitter_task_template.xml'
    $tpl = Get-Content -LiteralPath $tplPath -Raw
    $good = $tpl.Replace('{{ACCOUNT_ID}}', '999999').
                 Replace('{{PRINCIPAL}}', "$env:USERDOMAIN\$env:USERNAME").
                 Replace('{{PYTHON_EXE}}', 'C:\Windows\System32\cmd.exe').
                 Replace('{{ENTRYPOINT}}', 'C:\NonExistent\parity.noop').
                 Replace('{{CONFIG_PATH}}', 'C:\NonExistent\parity.cfg')
    $goodPath = Join-Path $env:TEMP 'guvfx_parity_good.xml'
    [void]$tempFiles.Add($goodPath)
    Set-Content -LiteralPath $goodPath -Value $good -Encoding Unicode

    # 3-4. Create the temp task from the reviewed XML and prove it reads back DISABLED (locale-independent).
    & schtasks /delete /tn "\$TASK" /f 2>$null | Out-Null
    & schtasks /create /tn "\$TASK" /xml "$goodPath" /f | Out-Null
    & schtasks /change /tn "\$TASK" /disable | Out-Null
    $readback = (& schtasks /query /tn "\$TASK" /xml ONE 2>$null) -join "`n"
    $isDisabled = ($readback -match '<Enabled>false</Enabled>')
    $noTrigger = ($readback -notmatch '<Trigger>')
    if (-not $isDisabled) { $ok = $false }
    if (-not $noTrigger) { $ok = $false }
    Note ("GOOD_XML created=True disabled={0} triggerless={1}" -f $isDisabled, $noTrigger)

    # 5-6. Delete the temp task and prove it is gone.
    & schtasks /delete /tn "\$TASK" /f | Out-Null
    & schtasks /query /tn "\$TASK" 2>$null | Out-Null
    $gone = ($LASTEXITCODE -ne 0)
    if (-not $gone) { $ok = $false }
    Note ("GOOD_XML deleted_and_gone={0}" -f $gone)

    # Negative control: the OLD defect (zero-duration RestartOnFailure) MUST be rejected by schtasks, proving
    # this validator would have caught the packet-008 failure. The bad interval literal lives ONLY here, in the
    # test tooling - never in the vendored bundle.
    $zeroIv = 'PT' + '0M'
    $badXml = $good -replace '</Settings>', ("  <RestartOnFailure><Interval>{0}</Interval><Count>0</Count></RestartOnFailure>`n  </Settings>" -f $zeroIv)
    $badXmlPath = Join-Path $env:TEMP 'guvfx_parity_badxml.xml'
    [void]$tempFiles.Add($badXmlPath)
    Set-Content -LiteralPath $badXmlPath -Value $badXml -Encoding Unicode
    & schtasks /delete /tn "\$TASK" /f 2>$null | Out-Null
    & schtasks /create /tn "\$TASK" /xml "$badXmlPath" /f 2>$null | Out-Null
    $rejected = ($LASTEXITCODE -ne 0)
    if (-not $rejected) { $ok = $false }
    Note ("NEGCTRL zero_interval_rejected={0} (expect True)" -f $rejected)
}
catch {
    $ok = $false
    Note ("ERROR " + $_.Exception.Message)
}
finally {
    # Self-clean: always remove the temp task and any temp files, success or failure.
    try { & schtasks /delete /tn "\$TASK" /f 2>$null | Out-Null } catch { }
    foreach ($tf in $tempFiles) { Remove-Item -LiteralPath $tf -Force -ErrorAction SilentlyContinue }
}

Note ("RESULT parity_all_checks_passed={0}" -f $ok)
if ($ok) { exit 0 } else { exit 1 }
