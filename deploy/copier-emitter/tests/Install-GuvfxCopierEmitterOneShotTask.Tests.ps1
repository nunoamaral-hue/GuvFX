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

    # a known-compliant rendered task XML (captured from one successful synthetic install) for idempotency/drift
    $script:pkgRef = New-SyntheticPackage
    $fx0 = New-FakeSchtasks
    $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $script:pkgRef -Apply -InvokeSchtasks $fx0.Invoke
    $script:compliantXml = [string]$fx0.State.CreatedXml
    $script:src = Get-Content -LiteralPath $script:Installer -Raw
}

Describe 'Install-GuvfxCopierEmitterOneShotTask - governed one-shot task installer' {

    It '(1) installs the exact account-46 task name, pinned' {
        $pkg = New-SyntheticPackage; $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $pkg -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.task | Should -BeExactly 'GuvFX_CopierEmitterOneShot_46'
        $res.account_pinned | Should -BeTrue
        $res.action | Should -BeExactly 'installed'
    }

    It '(2) refuses account 48 explicitly, before any schtasks call' {
        $pkg = New-SyntheticPackage; $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 48 -PackageRoot $pkg -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.reason | Should -BeExactly 'account_48_refused'
        $fx.State.Calls.Count | Should -Be 0
    }

    It '(3) refuses Customer Zero (account 1)' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 1 -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.reason | Should -BeExactly 'reserved_identity_refused'
        $fx.State.Calls.Count | Should -Be 0
    }

    It '(4) refuses an arbitrary account (47)' {
        $res = Install-GuvfxCopierEmitterOneShotTask -AccountId 47 -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'account_not_emitter_pinned'
    }

    It '(5) accepts no arbitrary task name / endpoint / operation parameter' {
        $p = (Get-Command Install-GuvfxCopierEmitterOneShotTask).Parameters.Keys
        foreach ($bad in 'TaskName', 'Task', 'Endpoint', 'VerifierUrl', 'Operation', 'Command', 'Arguments') { $p | Should -Not -Contain $bad }
    }

    It '(6) accepts no arbitrary executable/script path, and refuses a non-python interpreter' {
        $p = (Get-Command Install-GuvfxCopierEmitterOneShotTask).Parameters.Keys
        foreach ($bad in 'EntryPoint', 'ScriptPath', 'ConfigPath', 'KeyFile', 'ResultFile', 'ExePath') { $p | Should -Not -Contain $bad }
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -PythonExe 'C:\evil\mal.exe' -Apply -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'python_exe_untrusted'
    }

    It '(7) fails closed on a missing manifest' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -NoManifest) -Apply -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'manifest_missing'
    }

    It '(8) fails closed on a manifest/hash mismatch' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -CorruptHash) -Apply -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'hash_mismatch:run_oneshot_attest.py'
    }

    It '(9) fails closed on a PowerShell parse error in the bundle' {
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage -BadPs1) -Apply -InvokeSchtasks (New-FakeSchtasks).Invoke
        $res.reason | Should -BeExactly 'parse_error:broken.ps1'
    }

    It '(10) the rendered/registered task has zero triggers' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.trigger_count | Should -Be 0
        ([xml]$fx.State.CreatedXml).Task.Triggers.ChildNodes.Count | Should -Be 0
    }

    It '(11) the rendered/registered task is disabled' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.disabled | Should -BeTrue
        ('' + ([xml]$fx.State.CreatedXml).Task.Settings.Enabled) | Should -BeExactly 'false'
    }

    It '(12) never starts/runs/enables the task' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        ($fx.State.Calls | Where-Object { $_ -match '/run' -or $_ -match '/enable' }) | Should -BeNullOrEmpty
    }

    It '(13) registration is followed by an authoritative read-back query' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
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
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.reason | Should -BeExactly 'readback_mismatch'
        ($fx.State.Calls | Where-Object { $_ -match '/delete' }) | Should -Not -BeNullOrEmpty   # fail-closed cleanup
    }

    It '(15) an existing compliant task is idempotent (no-change, no create)' {
        # the compliant XML was captured from $script:pkgRef, so install against that SAME package (its derived
        # action paths match the existing task) to prove the no-change path
        $fx = New-FakeSchtasks; $fx.State.QueryXml = $script:compliantXml
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot $script:pkgRef -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.action | Should -BeExactly 'no_change'
        $res.idempotent_nochange | Should -BeTrue
        ($fx.State.Calls | Where-Object { $_ -match '/create' }) | Should -BeNullOrEmpty
    }

    It '(16) drift requires an explicit -Replace switch' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeFalse
        $res.drift_detected | Should -BeTrue
        $res.reason | Should -BeExactly 'drift_requires_replace'
        ($fx.State.Calls | Where-Object { $_ -match '/create' }) | Should -BeNullOrEmpty
    }

    It '(17) replacement exports a rollback of the previous definition' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -Replace -InvokeSchtasks $fx.Invoke
        $res.rollback_exported | Should -BeTrue
        $res.action | Should -BeExactly 'replaced'
        ($fx.State.Calls -join '||') | Should -Match '/delete'
        ($fx.State.Calls -join '||') | Should -Match '/create'
    }

    It '(18) a failed replacement restores the previous task, left disabled' {
        $fx = New-FakeSchtasks
        $fx.State.QueryXml = ($script:compliantXml -replace '<Enabled>false</Enabled>', '<Enabled>true</Enabled>')
        $fx.State.ReadBackXml = ($script:compliantXml -replace 'guvfx_u_46', 'attacker')   # read-back no longer matches
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -Replace -InvokeSchtasks $fx.Invoke
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
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $json = ($res | ConvertTo-Json -Compress -Depth 6)
        $json | Should -Not -Match '[0-9a-fA-F]{40,}'
        $json | Should -Not -Match 'COPIER_INGEST_HMAC_KEY'
        $res.no_secret_in_xml | Should -BeTrue
    }

    It '(21) never references or changes the run-mode emitter task' {
        $fx = New-FakeSchtasks
        $null = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
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
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -Apply -InvokeSchtasks $fx.Invoke
        $res.ok | Should -BeTrue
        $res.disabled | Should -BeTrue
        $res.readback_verified | Should -BeTrue
        ($fx.State.Calls | Where-Object { $_ -match '/run' -or $_ -match '/enable' }) | Should -BeNullOrEmpty
    }

    It '(bonus) PLAN is the dry-run default: no schtasks mutation without -Apply' {
        $fx = New-FakeSchtasks
        $res = Install-GuvfxCopierEmitterOneShotTask -PackageRoot (New-SyntheticPackage) -InvokeSchtasks $fx.Invoke
        $res.action | Should -BeExactly 'plan'
        ($fx.State.Calls | Where-Object { $_ -match '/create' -or $_ -match '/delete' -or $_ -match '/change' }) | Should -BeNullOrEmpty
    }
}
