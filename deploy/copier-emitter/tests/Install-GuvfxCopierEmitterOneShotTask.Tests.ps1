# Pester 5.x tests for the governed one-shot task installer (ADR-0104, packet 097). Non-production, synthetic only.
# Drives Install-GuvfxCopierEmitterOneShotTask directly with an INJECTED fake schtasks (no real scheduler, no real
# account, no secret, no host/backend/broker contact). Mutation-kills the dangerous cases. Windows PowerShell 5.1.
BeforeAll {
    $script:Installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'Install-GuvfxCopierEmitterOneShotTask.ps1'
    . $script:Installer   # dot-source: defines the function, does NOT execute (InvocationName '.')
    $script:BundleDir = Split-Path $PSScriptRoot -Parent

    function New-SyntheticPackage {
        param([int]$ConfigAccount = 46, [switch]$CorruptHash, [switch]$NoManifest, [switch]$BadPs1, [switch]$NoConfig)
        $root = Join-Path ([System.IO.Path]::GetTempPath()) ('guvfx_oneshot_pkg_' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $root | Out-Null
        # the two reviewed runtime artefacts (copied from the real bundle so the rendered XML is authentic)
        Copy-Item (Join-Path $script:BundleDir 'oneshot_attest_task_template.xml') (Join-Path $root 'oneshot_attest_task_template.xml')
        Copy-Item (Join-Path $script:BundleDir 'run_oneshot_attest.py') (Join-Path $root 'run_oneshot_attest.py')
        $h1 = (Get-FileHash -LiteralPath (Join-Path $root 'oneshot_attest_task_template.xml') -Algorithm SHA256).Hash.ToLower()
        $h2 = (Get-FileHash -LiteralPath (Join-Path $root 'run_oneshot_attest.py') -Algorithm SHA256).Hash.ToLower()
        if ($CorruptHash) { $h2 = ('0' * 64) }
        if (-not $NoManifest) {
            $m = [ordered]@{ bundle = 'synthetic'; files = [ordered]@{
                'oneshot_attest_task_template.xml' = $h1; 'run_oneshot_attest.py' = $h2 } }
            ($m | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath (Join-Path $root 'MANIFEST.json') -Encoding ascii
        }
        if (-not $NoConfig) {
            (@{ account_id = $ConfigAccount } | ConvertTo-Json) | Set-Content -LiteralPath (Join-Path $root 'config.json') -Encoding ascii
        }
        if ($BadPs1) { Set-Content -LiteralPath (Join-Path $root 'broken.ps1') -Value "function X { `$y = 'unterminated" -Encoding ascii }
        else { Set-Content -LiteralPath (Join-Path $root 'ok.ps1') -Value "param()`nWrite-Output 'ok'`n" -Encoding ascii }
        return $root
    }

    function New-FakeSchtasks {
        $state = [ordered]@{ Calls = (New-Object System.Collections.ArrayList); QueryXml = $null;
                             CreateExit = 0; ReadBackXml = $null; CreatedXml = $null }
        $sb = {
            param($argList)
            [void]$state.Calls.Add(($argList -join ' '))
            $verb = [string]$argList[0]
            if ($verb -eq '/query') {
                $x = $(if ($state.CreatedXml) { $(if ($null -ne $state.ReadBackXml) { $state.ReadBackXml } else { $state.CreatedXml }) } else { $state.QueryXml })
                if ($null -eq $x) { return ([ordered]@{ ExitCode = 1; Out = '' }) }
                return ([ordered]@{ ExitCode = 0; Out = [string]$x })
            }
            if ($verb -eq '/create') {
                $content = ''
                try { $content = Get-Content -LiteralPath ([string]$argList[4]) -Raw } catch { $content = '' }
                if ($state.CreateExit -eq 0) { $state.CreatedXml = $content }
                return ([ordered]@{ ExitCode = $state.CreateExit; Out = '' })
            }
            if ($verb -eq '/delete') { $state.CreatedXml = $null; $state.QueryXml = $null; return ([ordered]@{ ExitCode = 0; Out = '' }) }
            return ([ordered]@{ ExitCode = 0; Out = '' })
        }.GetNewClosure()
        return [ordered]@{ State = $state; Invoke = $sb }
    }

    # ---- principal-SID fix (GFX-COPIER-ONESHOT-INSTALLER-PRINCIPAL-NORMALIZATION-FIX-108): the installer now
    #      compares task principals by CANONICAL SID (schtasks stores a SID, not the submitted name). The mock returns
    #      the rendered bare 'guvfx_u_46', so every schtasks-exercising Install call below EXPLICITLY injects a
    #      deterministic fake resolver ($script:Resolve) via -ResolvePrincipalSid that maps the pinned-account forms
    #      -> one canonical test SID; the EXISTING regression tests then exercise the SID-to-SID path without a real
    #      account. (Explicit injection, not $PSDefaultParameterValues, because Pester shadows that variable.) The
    #      matrix tests below pass their OWN resolver + crafted read-back to prove accept/reject; the real-schtasks
    #      test proves live Windows SID normalization.
    $script:PinnedSid = 'S-1-5-21-TEST-GUVFX-46'
    $script:Resolve = {
        param($ref)
        $v = ('' + $ref).Trim()
        if ($v -match '(?i)^guvfx_u_46$' -or $v -match '(?i)^WIN-TEST\\guvfx_u_46$') { return 'S-1-5-21-TEST-GUVFX-46' }
        if ($v -eq 'S-1-5-21-TEST-GUVFX-46') { return 'S-1-5-21-TEST-GUVFX-46' }
        if ($v -match '(?i)unresolved') { return $null }        # explicit unresolved/dangling marker -> fail closed
        if ($v -match '^S-1-[0-9-]+$') { return $v }             # any other real SID -> itself (a DIFFERENT identity)
        return $null                                             # any other name -> unresolved -> fail closed
    }.GetNewClosure()

    # a known-compliant rendered task XML (captured from one successful synthetic install) for idempotency/drift
    $script:pkgRef = New-SyntheticPackage
    $fx0 = New-FakeSchtasks
    $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $script:pkgRef -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx0.Invoke
    $script:compliantXml = [string]$fx0.State.CreatedXml
    $script:src = Get-Content -LiteralPath $script:Installer -Raw
}

Describe 'Install-GuvfxCopierEmitterOneShotTask - governed one-shot task installer' {

    It '(1) installs the exact account-46 task name, pinned' {
        $pkg = New-SyntheticPackage; $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $pkg -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.task | Should -BeExactly 'GuvFX_CopierEmitterOneShot_46'
        $res.account_pinned | Should -BeTrue
        $res.action | Should -BeExactly 'installed'
    }

    It '(2) refuses account 48 explicitly, before any schtasks call' {
        $pkg = New-SyntheticPackage; $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 48 -PackageRoot $pkg -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.reason | Should -BeExactly 'account_48_refused'
        $fx.State.Calls.Count | Should -Be 0
    }

    It '(3) refuses Customer Zero (account 1)' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 1 -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.reason | Should -BeExactly 'reserved_identity_refused'
        $fx.State.Calls.Count | Should -Be 0
    }

    It '(4) refuses an arbitrary account (47)' {
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 47 -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'account_not_emitter_pinned'
    }

    It '(5) accepts no arbitrary task name / endpoint / operation parameter' {
        $p = (Get-Command Install-GuvfxCopierEmitterOneShotTask).Parameters.Keys
        foreach ($bad in 'TaskName', 'Task', 'Endpoint', 'VerifierUrl', 'Operation', 'Command', 'Arguments') { $p | Should -Not -Contain $bad }
    }

    It '(6) accepts no arbitrary executable/script path, and refuses a non-python interpreter' {
        $p = (Get-Command Install-GuvfxCopierEmitterOneShotTask).Parameters.Keys
        foreach ($bad in 'EntryPoint', 'ScriptPath', 'ConfigPath', 'KeyFile', 'ResultFile', 'ExePath') { $p | Should -Not -Contain $bad }
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -PythonExe 'C:\evil\mal.exe' -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'python_exe_untrusted'
    }

    It '(7) fails closed on a missing manifest' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -NoManifest) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'manifest_missing'
    }

    It '(8) fails closed on a manifest/hash mismatch' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -CorruptHash) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'hash_mismatch:run_oneshot_attest.py'
    }

    It '(9) fails closed on a PowerShell parse error in the bundle' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -BadPs1) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'parse_error:broken.ps1'
    }

    It '(10) the rendered/registered task has zero triggers' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.trigger_count | Should -Be 0
        ([xml]$fx.State.CreatedXml).Task.Triggers.ChildNodes.Count | Should -Be 0
    }

    It '(11) the rendered/registered task is disabled' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.disabled | Should -BeTrue
        ('' + ([xml]$fx.State.CreatedXml).Task.Settings.Enabled) | Should -BeExactly 'false'
    }

    It '(12) never starts/runs/enables the task' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        ($fx.State.Calls | Where-Object { $_ -match '/run' -or $_ -match '/enable' }) | Should -BeNullOrEmpty
    }

    It '(13) registration is followed by an authoritative read-back query' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $calls = @($fx.State.Calls)
        ($calls -join '||') | Should -Match '/create'
        # a /query (authoritative read-back) occurs AFTER the /create
        $createIdx = -1
        for ($i = 0; $i -lt $calls.Count; $i++) { if ($createIdx -lt 0 -and $calls[$i] -match '/create') { $createIdx = $i } }
        $queryAfter = $false
        for ($i = $createIdx + 1; $i -lt $calls.Count; $i++) { if ($calls[$i] -match '/query') { $queryAfter = $true } }
        $createIdx | Should -BeGreaterOrEqual 0
        $queryAfter | Should -BeTrue
    }

    It '(14) a read-back mismatch fails closed and removes the task' {
        $fx = New-FakeSchtasks
        $fx.State.ReadBackXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.reason | Should -BeExactly 'readback_mismatch'
        ($fx.State.Calls | Where-Object { $_ -match '/delete' }) | Should -Not -BeNullOrEmpty   # fail-closed cleanup
    }

    It '(15) an existing compliant task is idempotent (no-change, no create)' {
        # the compliant XML was captured from $script:pkgRef, so install against that SAME package (its derived
        # action paths match the existing task) to prove the no-change path
        $fx = New-FakeSchtasks; $fx.State.QueryXml = $script:compliantXml
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $script:pkgRef -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.action | Should -BeExactly 'no_change'
        $res.idempotent_nochange | Should -BeTrue
        ($fx.State.Calls | Where-Object { $_ -match '/create' }) | Should -BeNullOrEmpty
    }

    It '(16) drift requires an explicit -Replace switch' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.drift_detected | Should -BeTrue
        $res.reason | Should -BeExactly 'drift_requires_replace'
        ($fx.State.Calls | Where-Object { $_ -match '/create' }) | Should -BeNullOrEmpty
    }

    It '(17) replacement exports a rollback of the previous definition' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -Replace -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.rollback_exported | Should -BeTrue
        $res.action | Should -BeExactly 'replaced'
        ($fx.State.Calls -join '||') | Should -Match '/delete'
        ($fx.State.Calls -join '||') | Should -Match '/create'
    }

    It '(18) a failed replacement restores the previous task, left disabled' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $fx.State.ReadBackXml = ($script:compliantXml -replace 'guvfx_u_46', 'attacker')   # read-back no longer matches
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -Replace -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.readback_verified | Should -BeFalse
        # the finally restored the exported previous definition and left it disabled
        $calls = ($fx.State.Calls -join '||')
        $calls | Should -Match '/delete'
        ($fx.State.Calls | Where-Object { $_ -match '/change.*\\GuvFX_CopierEmitterOneShot_46.*/disable' }) | Should -Not -BeNullOrEmpty
    }

    It '(19) accepts no secret/key through any parameter' {
        $p = (Get-Command Install-GuvfxCopierEmitterOneShotTask).Parameters.Keys
        foreach ($bad in 'Key', 'Secret', 'IngestKey', 'HmacKey', 'Token', 'ApiKey', 'Password') { $p | Should -Not -Contain $bad }
    }

    It '(20) prints no secret-like value; the result carries no key and no long hex' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $json = ($res | ConvertTo-Json -Compress -Depth 6)
        $json | Should -Not -Match '[0-9a-fA-F]{40,}'
        $json | Should -Not -Match 'COPIER_INGEST_HMAC_KEY'
        $res.no_secret_in_xml | Should -BeTrue
    }

    It '(21) never references or changes the run-mode emitter task' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        # the run-mode task name is GuvFX_CopierEmitter_<id> (no "OneShot"); it must never appear in any call
        ($fx.State.Calls | Where-Object { $_ -match 'GuvFX_CopierEmitter_\d' }) | Should -BeNullOrEmpty
        # and the installer CODE (comments stripped) never constructs the run-mode task name
        $codeOnly = (($script:src -split "`n") | ForEach-Object { $_ -replace '#.*$', '' }) -join "`n"
        $codeOnly | Should -Not -Match 'GuvFX_CopierEmitter_\d'
    }

    It '(22) parses and runs under Windows PowerShell 5.1' {
        $PSVersionTable.PSVersion.Major | Should -BeGreaterOrEqual 5
        $pe = $null; $pt = $null
        [System.Management.Automation.Language.Parser]::ParseFile($script:Installer, [ref]$pt, [ref]$pe) | Out-Null
        @($pe).Count | Should -Be 0
    }

    It '(23) verifies task state from XML booleans, not locale-dependent status text' {
        $script:src | Should -Match '\.Task\.Settings\.Enabled'
        $script:src | Should -Not -Match "'Ready'"
        $script:src | Should -Not -Match "'Running'"
        $script:src | Should -Not -Match "'Disabled'"
    }

    It '(24) has no exit path inside the function that could bypass the finally cleanup' {
        $body = $script:src.Substring($script:src.IndexOf('function Install-GuvfxCopierEmitterOneShotTask'))
        $body = $body.Substring(0, $body.IndexOf('# ---- run-directly entry'))
        # scan CODE only (strip line comments: the header/inline comments legitimately mention the word "exit")
        $codeBody = (($body -split "`n") | ForEach-Object { $_ -replace '#.*$', '' }) -join "`n"
        $codeBody | Should -Not -Match '(^|[^A-Za-z])exit([^A-Za-z]|$)'
        $codeBody | Should -Match 'finally\s*\{'
    }

    It '(25) a successful install leaves the task disabled and never enabled/run' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.disabled | Should -BeTrue
        $res.readback_verified | Should -BeTrue
        ($fx.State.Calls | Where-Object { $_ -match '/run' -or $_ -match '/enable' }) | Should -BeNullOrEmpty
    }

    It '(bonus) PLAN is the dry-run default: no schtasks mutation without -Apply' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -ResolvePrincipalSid $script:Resolve -InvokeSchtasks $fx.Invoke
        $res.action | Should -BeExactly 'plan'
        ($fx.State.Calls | Where-Object { $_ -match '/create' -or $_ -match '/delete' -or $_ -match '/change' }) | Should -BeNullOrEmpty
    }
}

Describe 'Install-GuvfxCopierEmitterOneShotTask - canonical SID principal read-back (packet 108)' {
    # APPLY registers under the bare rendered name 'guvfx_u_46'; schtasks stores a SID, so the read-back returns a SID.
    # These tests drive APPLY with a crafted read-back UserId + an explicit resolver and assert canonical SID-to-SID
    # accept/reject + fail-closed. The default $PSDefaultParameterValues resolver maps the pinned-account forms -> one
    # test SID (S-1-5-21-TEST-GUVFX-46); other SIDs -> themselves; names with an 'unresolved' marker or any other
    # name -> $null (fail closed).
    BeforeAll {
        # defined in BeforeAll (run phase), not the Describe body (discovery phase), so it is in scope for the It blocks
        function Invoke-ApplyReadbackUserId {
            param([string]$UserId, [scriptblock]$Resolver)
            $fx = New-FakeSchtasks
            $fx.State.ReadBackXml = ($script:compliantXml -replace '<UserId>[^<]*</UserId>', ('<UserId>' + $UserId + '</UserId>'))
            $p = @{ PackageRoot = $script:pkgRef; Apply = $true; InvokeSchtasks = $fx.Invoke }
            $p['ResolvePrincipalSid'] = if ($Resolver) { $Resolver } else { $script:Resolve }
            return @{ res = (Install-GuvfxCopierEmitterOneShotTask @p); fx = $fx }
        }
    }

    # ---- POSITIVE: all three accepted forms of the pinned account resolve to the same SID -> read-back verified ----
    It '(P1) read-back bare guvfx_u_46 -> canonical SID matches -> installed+verified' {
        $o = Invoke-ApplyReadbackUserId 'guvfx_u_46'
        $o.res.ok | Should -BeTrue
        $o.res.readback_verified | Should -BeTrue
        $o.res.action | Should -BeExactly 'installed'
    }
    It '(P2) read-back machine-qualified WIN-TEST\guvfx_u_46 -> same SID -> verified' {
        $o = Invoke-ApplyReadbackUserId 'WIN-TEST\guvfx_u_46'
        $o.res.ok | Should -BeTrue
        $o.res.readback_verified | Should -BeTrue
    }
    It '(P3) read-back as the canonical local-account SID -> verified (schtasks normalization)' {
        $o = Invoke-ApplyReadbackUserId 'S-1-5-21-TEST-GUVFX-46'
        $o.res.ok | Should -BeTrue
        $o.res.readback_verified | Should -BeTrue
    }
    It '(P4) a SID-verified install remains DISABLED and triggerless' {
        $o = Invoke-ApplyReadbackUserId 'S-1-5-21-TEST-GUVFX-46'
        $o.res.disabled | Should -BeTrue
        $o.res.trigger_count | Should -Be 0
    }

    # ---- NEGATIVE: any identity whose canonical SID differs, or cannot be resolved, FAILS and deletes the task ----
    It '(N1) a different local user SID -> mismatch -> fail-closed delete' {
        $o = Invoke-ApplyReadbackUserId 'S-1-5-21-2216203845-1747098376-1637942580-9999'
        $o.res.ok | Should -BeFalse
        $o.res.reason | Should -BeExactly 'readback_mismatch'
        ($o.fx.State.Calls | Where-Object { $_ -match '/delete' }) | Should -Not -BeNullOrEmpty
    }
    It '(N2) a machine-qualified DIFFERENT user -> unresolved -> fail-closed delete' {
        $o = Invoke-ApplyReadbackUserId 'WIN-OTHER\someone_else'
        $o.res.ok | Should -BeFalse
        $o.res.reason | Should -BeExactly 'readback_mismatch'
    }
    It '(N3) an unresolved/dangling SID -> fail-closed delete' {
        $o = Invoke-ApplyReadbackUserId 'S-1-5-21-UNRESOLVED-0-0-0'
        $o.res.ok | Should -BeFalse
        $o.res.reason | Should -BeExactly 'readback_mismatch'
    }
    It '(N4) an UNRESOLVED EXPECTED principal fails before acceptance (resolver returns null for guvfx_u_46)' {
        $nullResolver = { param($ref) return $null }
        $o = Invoke-ApplyReadbackUserId 'guvfx_u_46' $nullResolver
        $o.res.ok | Should -BeFalse
        $o.res.readback_verified | Should -BeFalse
    }
    It '(N5) a fake-suffix substring evil_guvfx_u_46 is NOT accepted (no substring match)' {
        $o = Invoke-ApplyReadbackUserId 'evil_guvfx_u_46'
        $o.res.ok | Should -BeFalse
        $o.res.readback_verified | Should -BeFalse
    }
    It '(N6) account substitution guvfx_u_48 in the read-back is refused' {
        $o = Invoke-ApplyReadbackUserId 'guvfx_u_48'
        $o.res.ok | Should -BeFalse
        $o.res.readback_verified | Should -BeFalse
    }
    It '(N7) a DIFFERENT name that authoritatively resolves to the SAME SID is accepted (SID-to-SID, not string)' {
        # an alias name the resolver maps to the pinned SID -> accepted (proves comparison is by SID, not by string)
        $aliasResolver = {
            param($ref)
            $v = ('' + $ref).Trim()
            if ($v -match '(?i)^guvfx_u_46$') { return 'S-1-5-21-TEST-GUVFX-46' }
            if ($v -match '(?i)^WIN-TEST\\legacy_alias_46$') { return 'S-1-5-21-TEST-GUVFX-46' }
            return $null
        }
        $o = Invoke-ApplyReadbackUserId 'WIN-TEST\legacy_alias_46' $aliasResolver
        $o.res.ok | Should -BeTrue
        $o.res.readback_verified | Should -BeTrue
    }

    It '(src) the read-back resolves BOTH sides to a SID and compares SID-to-SID (no raw string equality on UserId)' {
        # mutation-killing: the raw-string comparison must be gone; the canonical resolver + SID compare must be present
        $script:src | Should -Match 'Resolve-GuvfxPrincipalSid'
        $script:src | Should -Match 'function Test-PrincipalCanonical'
        $script:src | Should -Match 'Translate\(\[System\.Security\.Principal\.SecurityIdentifier\]\)'
        # the read-back no longer compares the returned UserId to the bare $PRINCIPAL by string equality
        $script:src | Should -Not -Match "Principal\.UserId\) -eq \`$PRINCIPAL"
        # account pin preserved
        $script:src | Should -Match "\`$PINNED = 46"
    }
}
