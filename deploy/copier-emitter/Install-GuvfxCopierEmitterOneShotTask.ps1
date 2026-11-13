#Requires -Version 5.1
# ASCII-only. Governed installer for the DISABLED, triggerless, account-46-pinned one-shot emitter identity-
# attestation task GuvFX_CopierEmitterOneShot_46 (ADR-0104). Repository artifact; DARK.
#
# It INSTALLS NOTHING by itself: PLAN (dry-run) is the default; -Apply registers the task DISABLED and triggerless
# and authoritatively reads it back (fail-closed). It NEVER enables or runs the task, NEVER provisions or reads the
# real HMAC key, NEVER opens a verifier window, NEVER contacts the backend or broker, and NEVER alters the existing
# run-mode emitter task (GuvFX_CopierEmitter_<id>). Every identity/path/action is DERIVED from the pinned account 46
# and the reviewed staged package + fixed manifest; nothing (task name, exe, script, endpoint, account, operation,
# free-form command) is accepted from the caller. Customer Zero, account 48 and every reserved identity are refused.
#
# Dot-sourceable: when dot-sourced it defines Install-GuvfxCopierEmitterOneShotTask (returning a structured result,
# never calling exit) and does not execute; the Pester tests drive it with an injected schtasks invoker (no real
# scheduler, no real account, no secret). When run directly it executes PLAN/Apply and emits one redacted JSON line.
[CmdletBinding()]
param(
    [int]$AccountId = 46,
    [string]$PackageRoot = '',
    [string]$PythonExe = 'python',
    [switch]$Apply,
    [switch]$Replace
)
$ErrorActionPreference = 'Stop'

function Install-GuvfxCopierEmitterOneShotTask {
    [CmdletBinding()]
    param(
        [int]$AccountId = 46,
        [string]$PackageRoot = '',
        [string]$PythonExe = 'python',
        [switch]$Apply,
        [switch]$Replace,
        [scriptblock]$InvokeSchtasks = $null   # injectable for tests; $null => the real schtasks
    )
    $ErrorActionPreference = 'Stop'

    # ---- fixed, pinned constants (never caller-controlled) -------------------------------------------------
    $PINNED = 46
    $RESERVED = @(0, 1, 18)                 # Customer Zero (1) + sacred demo-control (18) + 0
    $EXPLICIT_REFUSE = @(48)                # the copier follower account, explicitly refused
    $TASK = 'GuvFX_CopierEmitterOneShot_' + $PINNED
    $PRINCIPAL = 'guvfx_u_' + $PINNED
    $VERIFIER_URL = 'https://api.guvfx.com/api/copy-trading/attest-verify/'
    $RESULT_FILE = 'C:\GuvFX\accounts\' + $PINNED + '\_oneshot\attest_result.json'
    $KEY_FILE = 'C:\GuvFX\copier-emitter\oneshot\ingest.key'
    $REQUIRED = @('run_oneshot_attest.py', 'oneshot_attest_task_template.xml')
    $DQ = [char]34                          # double-quote, kept out of literals so the file's quote parity is simple

    $r = [ordered]@{
        ok = $false; action = $null; account_id = $AccountId; task = $TASK; applied = [bool]$Apply;
        account_pinned = $false; disabled = $false; trigger_count = $null; action_matches_fixed = $false;
        no_secret_in_xml = $false; readback_verified = $false; idempotent_nochange = $false;
        drift_detected = $false; rollback_exported = $false; reason = ''
    }
    function Fail([string]$why) { $r.ok = $false; $r.reason = [string]$why; return $r }

    # ---- account pin (explicit, ordered, distinct reasons) ------------------------------------------------
    if ($EXPLICIT_REFUSE -contains $AccountId) { return (Fail 'account_48_refused') }
    if ($RESERVED -contains $AccountId) { return (Fail 'reserved_identity_refused') }
    if ($AccountId -ne $PINNED) { return (Fail 'account_not_emitter_pinned') }
    $r.account_pinned = $true

    # ---- the only caller-influenced executable is the Python interpreter; refuse anything but python[.exe] (a bare
    #      name resolved on PATH, or an absolute path whose leaf is python/py/pythonw). No arbitrary executable. ----
    if ($PythonExe -notmatch '^(python|py|pythonw)(\.exe)?$' -and $PythonExe -notmatch '^[A-Za-z]:\\[^<>|?*]*\\(python|py|pythonw)(\.exe)?$') { return (Fail 'python_exe_untrusted') }

    # ---- package root + manifest -------------------------------------------------------------------------
    if (-not $PackageRoot -or -not (Test-Path -LiteralPath $PackageRoot -PathType Container)) { return (Fail 'package_root_missing') }
    $manifestPath = Join-Path $PackageRoot 'MANIFEST.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { return (Fail 'manifest_missing') }
    $manifest = $null
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json } catch { return (Fail 'manifest_malformed') }
    if (-not $manifest.files) { return (Fail 'manifest_no_files') }

    # ---- validate the required runtime files against the reviewed manifest hashes ------------------------
    foreach ($name in $REQUIRED) {
        $expected = $null
        try { $expected = [string]$manifest.files.$name } catch { $expected = $null }
        if (-not $expected) { return (Fail ('manifest_missing_entry:' + $name)) }
        $fp = Join-Path $PackageRoot $name
        if (-not (Test-Path -LiteralPath $fp)) { return (Fail ('required_file_missing:' + $name)) }
        $actual = (Get-FileHash -LiteralPath $fp -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne ([string]$expected).ToLower()) { return (Fail ('hash_mismatch:' + $name)) }
    }

    # ---- ParseFile every .ps1 in the bundle (RULE 9) before touching the scheduler -----------------------
    foreach ($ps1 in @(Get-ChildItem -LiteralPath $PackageRoot -Filter '*.ps1' -File)) {
        $pt = $null; $pe = $null
        [System.Management.Automation.Language.Parser]::ParseFile($ps1.FullName, [ref]$pt, [ref]$pe) | Out-Null
        if ($pe -and $pe.Count -gt 0) { return (Fail ('parse_error:' + $ps1.Name)) }
    }

    # ---- derive the fixed action paths; validate the account-46 config -----------------------------------
    $entrypoint = Join-Path $PackageRoot 'run_oneshot_attest.py'
    $configPath = Join-Path $PackageRoot 'config.json'
    if (-not (Test-Path -LiteralPath $configPath)) { return (Fail 'config_missing') }
    $cfg = $null
    try { $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json } catch { return (Fail 'config_malformed') }
    if ([int]$cfg.account_id -ne $PINNED) { return (Fail 'config_account_not_46') }

    # ---- render the reviewed template: ONLY the fixed placeholder set, each XML-escaped ------------------
    $templatePath = Join-Path $PackageRoot 'oneshot_attest_task_template.xml'
    $tpl = Get-Content -LiteralPath $templatePath -Raw
    function Esc([string]$s) { return ([string]$s).Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;') }
    $sub = [ordered]@{
        '{{ACCOUNT_ID}}'  = (Esc ([string]$PINNED))
        '{{PRINCIPAL}}'   = (Esc $PRINCIPAL)
        '{{PYTHON_EXE}}'  = (Esc $PythonExe)
        '{{ENTRYPOINT}}'  = (Esc $entrypoint)
        '{{CONFIG_PATH}}' = (Esc $configPath)
        '{{VERIFIER_URL}}' = (Esc $VERIFIER_URL)
        '{{KEY_FILE}}'    = (Esc $KEY_FILE)
        '{{RESULT_FILE}}' = (Esc $RESULT_FILE)
    }
    $rendered = $tpl
    foreach ($k in $sub.Keys) { $rendered = $rendered.Replace($k, [string]$sub[$k]) }
    if ($rendered -match '\{\{[A-Z_]+\}\}') { return (Fail 'template_unfilled_placeholder') }

    # the exact fixed Arguments the rendered action must carry (double-quotes via $DQ so file quote-parity is simple)
    $expectedArgs = $DQ + $entrypoint + $DQ + ' --config ' + $DQ + $configPath + $DQ + ' --verifier-url ' + $DQ + $VERIFIER_URL + $DQ + ' --key-file ' + $DQ + $KEY_FILE + $DQ + ' --result-file ' + $DQ + $RESULT_FILE + $DQ

    # ---- structurally validate the rendered XML ----------------------------------------------------------
    $xml = $null
    try { $xml = [xml]$rendered } catch { return (Fail 'rendered_xml_malformed') }
    if (('' + $xml.Task.Settings.Enabled) -ne 'false') { return (Fail 'rendered_not_disabled') }
    $trigNode = $xml.Task.Triggers
    $trigCount = 0
    if ($trigNode -and $trigNode.ChildNodes) { $trigCount = [int]$trigNode.ChildNodes.Count }
    if ($trigCount -ne 0) { return (Fail 'rendered_has_triggers') }
    $cmd = '' + $xml.Task.Actions.Exec.Command
    $actionArgs = '' + $xml.Task.Actions.Exec.Arguments
    if ($cmd -ne $PythonExe) { return (Fail 'rendered_command_mismatch') }
    if ($actionArgs -ne $expectedArgs) { return (Fail 'rendered_args_mismatch') }
    # parity with the Trigger-GuvfxCopierEmitterOneShot.ps1 action-trust check (defence in depth)
    if ($actionArgs -notmatch 'run_oneshot_attest\.py' -or $actionArgs -notmatch 'attest-verify' -or $actionArgs -match 'event-ingest' -or $actionArgs -notmatch 'attest_result\.json') { return (Fail 'rendered_action_untrusted') }
    if (('' + $xml.Task.Principals.Principal.UserId) -ne $PRINCIPAL) { return (Fail 'rendered_principal_mismatch') }
    if ($rendered -match '[0-9a-fA-F]{40,}' -or $rendered -match 'COPIER_INGEST_HMAC_KEY' -or $rendered -match 'BEGIN [A-Z ]*PRIVATE KEY') { return (Fail 'rendered_contains_secret') }
    $r.no_secret_in_xml = $true

    # ---- idempotency: compare any existing task's security-relevant fields -------------------------------
    if ($null -eq $InvokeSchtasks) {
        $InvokeSchtasks = { param($argList) $o = (& schtasks @argList 2>$null); return [ordered]@{ ExitCode = $LASTEXITCODE; Out = (@($o) -join [Environment]::NewLine) } }
    }
    function Read-TaskXml([string]$name) {
        $res = & $InvokeSchtasks @('/query', '/tn', ('\' + $name), '/xml', 'ONE')
        if ($null -eq $res -or $res.ExitCode -ne 0 -or -not $res.Out) { return $null }
        return [string]$res.Out
    }
    function Test-TaskCompliant([string]$taskXml) {
        $t = $null
        try { $t = [xml]$taskXml } catch { return $false }
        if (-not $t.Task) { return $false }
        $tTrig = 0
        if ($t.Task.Triggers -and $t.Task.Triggers.ChildNodes) { $tTrig = [int]$t.Task.Triggers.ChildNodes.Count }
        return ((('' + $t.Task.Settings.Enabled) -eq 'false') -and ($tTrig -eq 0) -and
                (('' + $t.Task.Actions.Exec.Command) -eq $PythonExe) -and
                (('' + $t.Task.Actions.Exec.Arguments) -eq $expectedArgs) -and
                (('' + $t.Task.Principals.Principal.UserId) -eq $PRINCIPAL) -and
                (-not ($taskXml -match '[0-9a-fA-F]{40,}')) -and (-not ($taskXml -match 'COPIER_INGEST_HMAC_KEY')))
    }

    $existingXml = Read-TaskXml $TASK
    if ($existingXml) {
        if (Test-TaskCompliant $existingXml) {
            $r.ok = $true; $r.action = 'no_change'; $r.idempotent_nochange = $true; $r.disabled = $true
            $r.trigger_count = 0; $r.action_matches_fixed = $true; $r.readback_verified = $true
            $r.reason = 'already_compliant'
            return $r
        }
        $r.drift_detected = $true
        if (-not $Replace) { $r.ok = $false; $r.action = 'drift'; $r.reason = 'drift_requires_replace'; return $r }
    }

    # ---- PLAN (dry-run default): never touch the scheduler -----------------------------------------------
    if (-not $Apply) { $r.ok = $true; $r.action = 'plan'; $r.reason = 'dry_run_no_change'; return $r }

    # ---- APPLY: register DISABLED, authoritative read-back, fail-closed. No `exit` here, so the finally
    #      ALWAYS runs (rollback/cleanup can never be skipped by an early exit). -------------------------------
    $rollbackExport = $null
    $registered = $false
    try {
        if ($r.drift_detected) {
            $rollbackExport = $existingXml                 # export the previous definition for rollback
            if ($rollbackExport) { $r.rollback_exported = $true }
            & $InvokeSchtasks @('/delete', '/tn', ('\' + $TASK), '/f') | Out-Null
        }
        $xmlPath = Join-Path $env:TEMP ('guvfx_oneshot_' + $PINNED + '_' + [guid]::NewGuid().ToString('N') + '.xml')
        Set-Content -LiteralPath $xmlPath -Value $rendered -Encoding Unicode
        $cres = & $InvokeSchtasks @('/create', '/tn', ('\' + $TASK), '/xml', $xmlPath, '/f')
        Remove-Item -LiteralPath $xmlPath -ErrorAction SilentlyContinue
        if ($null -eq $cres -or $cres.ExitCode -ne 0) { $r.ok = $false; $r.reason = 'registration_failed' }
        else {
            $registered = $true
            & $InvokeSchtasks @('/change', '/tn', ('\' + $TASK), '/disable') | Out-Null   # belt; never /run, never /enable
            $rb = Read-TaskXml $TASK
            if (-not $rb) { $r.ok = $false; $r.reason = 'readback_unreadable' }
            else {
                $rx = $null
                try { $rx = [xml]$rb } catch { $rx = $null }
                if (-not $rx -or -not $rx.Task) { $r.ok = $false; $r.reason = 'readback_malformed' }
                else {
                    $rbTrig = 0
                    if ($rx.Task.Triggers -and $rx.Task.Triggers.ChildNodes) { $rbTrig = [int]$rx.Task.Triggers.ChildNodes.Count }
                    $rbEnabled = (('' + $rx.Task.Settings.Enabled) -eq 'false')
                    $rbCmd = (('' + $rx.Task.Actions.Exec.Command) -eq $PythonExe)
                    $rbArgs = (('' + $rx.Task.Actions.Exec.Arguments) -eq $expectedArgs)
                    $rbUid = (('' + $rx.Task.Principals.Principal.UserId) -eq $PRINCIPAL)
                    $rbSecret = (($rb -match '[0-9a-fA-F]{40,}') -or ($rb -match 'COPIER_INGEST_HMAC_KEY'))
                    $r.disabled = $rbEnabled
                    $r.trigger_count = $rbTrig
                    $r.action_matches_fixed = ($rbCmd -and $rbArgs)
                    $r.no_secret_in_xml = (-not $rbSecret)
                    if ($rbEnabled -and $rbTrig -eq 0 -and $rbCmd -and $rbArgs -and $rbUid -and (-not $rbSecret)) {
                        $r.readback_verified = $true; $r.ok = $true
                        $r.action = $(if ($r.drift_detected) { 'replaced' } else { 'installed' })
                        $r.reason = 'installed_disabled_verified'
                    }
                    else { $r.ok = $false; $r.reason = 'readback_mismatch' }
                }
            }
        }
    }
    finally {
        # fail-closed: if we registered but could NOT verify a compliant disabled task, remove it and (when a
        # previous definition existed) restore the exported rollback, left DISABLED. Never leave an unverified task.
        if ($registered -and (-not $r.readback_verified)) {
            try { & $InvokeSchtasks @('/delete', '/tn', ('\' + $TASK), '/f') | Out-Null } catch { }
            if ($rollbackExport) {
                try {
                    $rbPath = Join-Path $env:TEMP ('guvfx_oneshot_rb_' + $PINNED + '_' + [guid]::NewGuid().ToString('N') + '.xml')
                    Set-Content -LiteralPath $rbPath -Value $rollbackExport -Encoding Unicode
                    & $InvokeSchtasks @('/create', '/tn', ('\' + $TASK), '/xml', $rbPath, '/f') | Out-Null
                    & $InvokeSchtasks @('/change', '/tn', ('\' + $TASK), '/disable') | Out-Null
                    Remove-Item -LiteralPath $rbPath -ErrorAction SilentlyContinue
                } catch { }
            }
        }
    }
    return $r
}

# ---- run-directly entry (skipped when dot-sourced for tests: InvocationName is '.') -----------------------
if ($MyInvocation.InvocationName -ne '.') {
    $res = Install-GuvfxCopierEmitterOneShotTask @PSBoundParameters
    ($res | ConvertTo-Json -Compress -Depth 6)
    if (-not $res.ok) { exit 1 } else { exit 0 }
}
