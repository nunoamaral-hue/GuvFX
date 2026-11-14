# ASCII-only (security.md RULE 9). Pester behavioural test for the host-local one-shot ingest-key PROVISIONER
# (ADR-0104/ADR-0101; GFX-COPIER-ONESHOT-KEY-PROVISIONER-112). SYNTHETIC key only - NEVER a real secret. Run on a
# Windows PowerShell 5.1 runner with Pester 5.x (the 5.1 built-in Pester 3.4 does NOT support BeforeAll / Should -BeTrue):
#   Install-Module Pester -MinimumVersion 5.0 -Force -SkipPublisherCheck ; Invoke-Pester -Path .\Test-OneShotKeyProvisioner.Tests.ps1 -Output Detailed
# All setup lives in BeforeAll (Pester 5+ runs the file body in a DISCOVERY phase where top-level functions/vars are
# NOT available to the It run phase). The real scheduler, SID resolution and secret entry are replaced by injected
# seams (a synthetic compliant task XML, a resolver that maps guvfx_u_46 to the RUNNER'S OWN SID so the ACL can be set
# without a real guvfx_u_46 account, and a synthetic >=32-char key). The FIXED relative key path is sandboxed under a
# temp root via -FixedRootForTest; the account/task/filename remain non-overridable.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Describe 'host-local one-shot key provisioner (synthetic)' {
    BeforeAll {
        $script:Wrapper = Join-Path (Split-Path -Parent $PSScriptRoot) 'Provision-GuvfxCopierOneShotKey.ps1'
        $script:Synth = 'SYNTHETIC_PROV_KEY_0123456789_ABCDEF0123'          # 40 chars, no 40+ hex run, no placeholder
        $script:Synth32 = 'SYNTH_KEY_32_0123456789_ABCDEFGH'               # exactly 32 chars
        $script:RunnerSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

        function New-Root { return (Join-Path $env:TEMP ('prov_' + [guid]::NewGuid().ToString('N'))) }
        function Key-Path([string]$root) { return (Join-Path $root 'copier-emitter\oneshot\ingest.key') }
        function Key-Dir([string]$root) { return (Join-Path $root 'copier-emitter\oneshot') }

        function New-TaskXml {
            param([string]$Enabled = 'false', [string]$Triggers = '<Triggers />', [string]$UserId = 'guvfx_u_46', [string]$Arguments = '')
            if ($Arguments -eq '') {
                $Arguments = '"C:\GuvFX\copier-emitter\46\run_oneshot_attest.py" --config "C:\GuvFX\copier-emitter\46\config.json" --verifier-url "https://verifier.invalid/api/copy-trading/attest-verify/" --key-file "C:\GuvFX\copier-emitter\oneshot\ingest.key" --result-file "C:\GuvFX\accounts\46\_oneshot\attest_result.json"'
            }
            return ('<?xml version="1.0" encoding="UTF-16"?>' +
                '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' +
                '<Settings><Enabled>' + $Enabled + '</Enabled><AllowStartIfOnBatteries>true</AllowStartIfOnBatteries></Settings>' +
                $Triggers +
                '<Actions><Exec><Command>C:\Program Files\Python311\python.exe</Command><Arguments>' + $Arguments + '</Arguments></Exec></Actions>' +
                '<Principals><Principal><UserId>' + $UserId + '</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>' +
                '</Task>')
        }

        function Invoke-Prov {
            param(
                [string]$Root, [string]$TaskXml = '__COMPLIANT__', [string]$Secret = '__SYNTH__',
                [int]$Account = 46, [switch]$Remove, [switch]$Verify, [switch]$Verbose2, [scriptblock]$AfterTemp = $null
            )
            if ($TaskXml -eq '__COMPLIANT__') { $TaskXml = (New-TaskXml) }
            if ($Secret -eq '__SYNTH__') { $Secret = $script:Synth }
            $rsid = $script:RunnerSid
            $sidBlock = {
                param($ref)
                if ($ref -match '^S-1-') { return $ref }
                if ($ref -eq 'guvfx_u_46') { return $rsid }
                return 'S-1-5-21-9999-9999-9999-9999'
            }.GetNewClosure()
            $txl = $TaskXml
            if ([string]::IsNullOrEmpty($txl)) { $taskBlock = { return $null }.GetNewClosure() }
            else { $taskBlock = { return $txl }.GetNewClosure() }
            $sec = $Secret
            $secBlock = { return $sec }.GetNewClosure()

            $p = @{
                AccountId = $Account; AssumeYes = $true; FixedRootForTest = $Root;
                ReadTaskXml = $taskBlock; ResolvePrincipalSid = $sidBlock; ReadSecret = $secBlock
            }
            if ($Remove) { $p['Remove'] = $true }
            if ($Verify) { $p['Verify'] = $true }
            if ($AfterTemp) { $p['AfterTempForTest'] = $AfterTemp }
            if ($Verbose2) {
                $out = & $script:Wrapper @p -Verbose 2>&1
            } else {
                $out = & $script:Wrapper @p 2>&1
            }
            $code = $LASTEXITCODE
            $text = ($out | Out-String)
            $json = $null
            $lines = @(($text -split "`r?`n") | Where-Object { $_.Trim() -ne '' })
            if ($lines.Count -gt 0) { try { $json = $lines[$lines.Count - 1] | ConvertFrom-Json } catch { $json = $null } }
            return @{ json = $json; raw = $text; code = $code; root = $Root }
        }

        function Get-AclSidSet([string]$path) {
            $acl = Get-Acl -LiteralPath $path
            $set = @()
            foreach ($r in $acl.Access) { $set += $r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
            return ($set | Sort-Object -Unique)
        }
    }

    AfterAll {
        Get-ChildItem -LiteralPath $env:TEMP -Filter 'prov_*' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } catch { } }
        Remove-Variable -Name Synth, Synth32, RunnerSid, Wrapper -Scope Script -ErrorAction SilentlyContinue
    }

    Context 'provision - positive, boundaries, redaction, ACL, consumer compatibility' {
        It '1. provisions on the fixed account-46 path from hidden input' {
            $root = New-Root
            $r = Invoke-Prov -Root $root
            $r.json.ok | Should -BeTrue
            $r.json.reason | Should -Be 'provisioned'
            $r.json.account_id | Should -Be 46
            $r.json.key_file_present | Should -BeTrue
            $r.json.acl_verified | Should -BeTrue
            $r.json.length_policy_satisfied | Should -BeTrue
            $r.code | Should -Be 0
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeTrue
        }
        It '2. accepts a secret of exactly the minimum length (32)' {
            $root = New-Root
            $r = Invoke-Prov -Root $root -Secret $script:Synth32
            $r.json.ok | Should -BeTrue
            $r.json.reason | Should -Be 'provisioned'
        }
        It '3. rejects a short (<32) secret, writes nothing' {
            $root = New-Root
            $r = Invoke-Prov -Root $root -Secret 'tooshort'
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -Be 'secret_too_short'
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeFalse
        }
        It '4. rejects an empty secret, writes nothing' {
            $root = New-Root
            $r = Invoke-Prov -Root $root -Secret ''
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -Be 'secret_too_short'
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeFalse
        }
        It '20. breaks ACL inheritance on the written file' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            (Get-Acl -LiteralPath (Key-Path $root)).AreAccessRulesProtected | Should -BeTrue
        }
        It '21. grants ONLY {tenant(runner), SYSTEM, Administrators}' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            $sids = Get-AclSidSet (Key-Path $root)
            $expected = @('S-1-5-18', 'S-1-5-32-544', $script:RunnerSid) | Sort-Object -Unique
            ($sids -join ',') | Should -Be ($expected -join ',')
        }
        It '22. removes the temp file after success' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            @(Get-ChildItem -LiteralPath (Key-Dir $root) -Filter '.ingest.key.tmp.*' -Force -ErrorAction SilentlyContinue).Count | Should -Be 0
        }
        It '24. never prints the key in stdout/result' {
            $root = New-Root
            $r = Invoke-Prov -Root $root
            $r.raw | Should -Not -Match ([regex]::Escape($script:Synth))
            "$($r.json)" | Should -Not -Match ([regex]::Escape($script:Synth))
        }
        It '29. writes the key verbatim (UTF-8, no BOM) for consumer compatibility' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            $bytes = [System.IO.File]::ReadAllBytes((Key-Path $root))
            ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) | Should -BeFalse  # no BOM
            [System.Text.Encoding]::UTF8.GetString($bytes) | Should -Be $script:Synth
        }
        It '28. provision then -Remove leave the mocked task contract intact (no task mutation)' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            (Invoke-Prov -Root $root -Verify).json.ok | Should -BeTrue
            (Invoke-Prov -Root $root -Remove).json.ok | Should -BeTrue
            # the same compliant task still passes the contract -> provision/remove never touched the task
            (Invoke-Prov -Root $root).json.ok | Should -BeTrue
        }
    }

    Context 'account pinning (mutation-killing)' {
        It '5. refuses account 48' {
            $r = Invoke-Prov -Root (New-Root) -Account 48
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -Be 'account_refused_explicit'
        }
        It '6. refuses Customer Zero (1)' {
            (Invoke-Prov -Root (New-Root) -Account 1).json.reason | Should -Be 'account_refused_reserved'
        }
        It '6b. refuses the sacred reserved identity (18)' {
            (Invoke-Prov -Root (New-Root) -Account 18).json.reason | Should -Be 'account_refused_reserved'
        }
        It '7. refuses an arbitrary account (99)' {
            (Invoke-Prov -Root (New-Root) -Account 99).json.reason | Should -Be 'account_not_pinned'
        }
    }

    Context 'no caller-supplied alternate account/task/path/principal' {
        It '8/9/10. exposes NO override parameter for path/task/principal' {
            $params = (Get-Command -Name $script:Wrapper).Parameters.Keys
            foreach ($forbidden in @('KeyPath', 'KeyFile', 'Path', 'Destination', 'Task', 'TaskName', 'Principal', 'PrincipalName', 'UserId', 'Sid')) {
                $params | Should -Not -Contain $forbidden
            }
        }
        It '8b. the FIXED relative structure is enforced regardless of the sandbox root' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            # the key can ONLY ever land at <root>\copier-emitter\oneshot\ingest.key
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeTrue
            (Split-Path -Leaf (Key-Path $root)) | Should -Be 'ingest.key'
        }
    }

    Context 'installed-task contract (fail closed)' {
        It '15. refuses when the task is absent' {
            (Invoke-Prov -Root (New-Root) -TaskXml '').json.reason | Should -Be 'task_absent'
        }
        It '16. refuses when the task is ENABLED' {
            (Invoke-Prov -Root (New-Root) -TaskXml (New-TaskXml -Enabled 'true')).json.reason | Should -Be 'task_not_disabled'
        }
        It '17. refuses when the task carries a trigger' {
            $trig = '<Triggers><TimeTrigger><StartBoundary>2026-01-01T00:00:00</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>'
            (Invoke-Prov -Root (New-Root) -TaskXml (New-TaskXml -Triggers $trig)).json.reason | Should -Be 'task_has_trigger'
        }
        It '18. refuses an action that drifts (missing attest-verify / event-ingest present)' {
            $badArgs = '"C:\GuvFX\copier-emitter\46\run_oneshot_attest.py" --verifier-url "https://verifier.invalid/api/copy-trading/event-ingest/" --key-file "C:\GuvFX\copier-emitter\oneshot\ingest.key" --result-file "x\attest_result.json"'
            (Invoke-Prov -Root (New-Root) -TaskXml (New-TaskXml -Arguments $badArgs)).json.reason | Should -Be 'task_action_drift'
        }
        It '19. refuses a drifted principal' {
            (Invoke-Prov -Root (New-Root) -TaskXml (New-TaskXml -UserId 'guvfx_u_99')).json.reason | Should -Be 'task_principal_drift'
        }
    }

    Context 'reparse-point refusal (mutation-killing)' {
        It '12. refuses a reparse-point DESTINATION' {
            $root = New-Root
            New-Item -ItemType Directory -Path (Key-Dir $root) -Force | Out-Null
            $tgt = Join-Path $env:TEMP ('rtgt_' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tgt -Force | Out-Null
            New-Item -ItemType Junction -Path (Key-Path $root) -Target $tgt -ErrorAction Stop | Out-Null
            (Invoke-Prov -Root $root).json.reason | Should -Be 'reparse_point_detected'
        }
        It '13. refuses a reparse-point PARENT' {
            $root = New-Root
            New-Item -ItemType Directory -Path (Join-Path $root 'copier-emitter') -Force | Out-Null
            $tgt = Join-Path $env:TEMP ('ptgt_' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tgt -Force | Out-Null
            New-Item -ItemType Junction -Path (Key-Dir $root) -Target $tgt -ErrorAction Stop | Out-Null
            (Invoke-Prov -Root $root).json.reason | Should -Be 'reparse_point_detected'
        }
    }

    Context 'existing-file policy' {
        It '11. refuses to overwrite an existing key without -Remove' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            (Invoke-Prov -Root $root).json.reason | Should -Be 'key_file_exists'
        }
    }

    Context 'capture-mode guard' {
        It '14. refuses to provision under verbose tracing' {
            (Invoke-Prov -Root (New-Root) -Verbose2).json.reason | Should -Be 'tracing_active'
        }
    }

    Context 'remove + failure cleanup' {
        It '25. -Remove deletes the fixed key file' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            $r = Invoke-Prov -Root $root -Remove
            $r.json.ok | Should -BeTrue
            $r.json.reason | Should -Be 'removed'
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeFalse
        }
        It '26. -Remove is idempotent on an absent file' {
            $root = New-Root
            $r = Invoke-Prov -Root $root -Remove
            $r.json.ok | Should -BeTrue
            $r.json.reason | Should -Be 'removed'
        }
        It '27. -Remove refuses a reparse target' {
            $root = New-Root
            New-Item -ItemType Directory -Path (Key-Dir $root) -Force | Out-Null
            $tgt = Join-Path $env:TEMP ('rmtgt_' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tgt -Force | Out-Null
            New-Item -ItemType Junction -Path (Key-Path $root) -Target $tgt -ErrorAction Stop | Out-Null
            $r = Invoke-Prov -Root $root -Remove
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -BeIn @('reparse_point_detected', 'remove_target_reparse')
        }
        It '23. removes the temp file after a mid-write failure, writes no final key' {
            $root = New-Root
            $r = Invoke-Prov -Root $root -AfterTemp { throw 'injected' }
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -Be 'write_failed'
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeFalse
            @(Get-ChildItem -LiteralPath (Key-Dir $root) -Filter '.ingest.key.tmp.*' -Force -ErrorAction SilentlyContinue).Count | Should -Be 0
        }
    }

    Context 'independent verifier + mismatch fail-closed' {
        It 'verify passes on a correctly provisioned file' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            $r = Invoke-Prov -Root $root -Verify
            $r.json.ok | Should -BeTrue
            $r.json.reason | Should -Be 'verified'
        }
        It '30. mismatch (task drift) fails verify closed WITHOUT weakening the file ACL' {
            $root = New-Root
            [void](Invoke-Prov -Root $root)
            $before = Get-AclSidSet (Key-Path $root)
            $r = Invoke-Prov -Root $root -Verify -TaskXml (New-TaskXml -UserId 'guvfx_u_99')
            $r.json.ok | Should -BeFalse
            $r.json.reason | Should -Be 'task_principal_drift'
            # the file and its restrictive ACL are untouched by a failed verify
            (Test-Path -LiteralPath (Key-Path $root)) | Should -BeTrue
            ((Get-AclSidSet (Key-Path $root)) -join ',') | Should -Be ($before -join ',')
        }
    }
}
