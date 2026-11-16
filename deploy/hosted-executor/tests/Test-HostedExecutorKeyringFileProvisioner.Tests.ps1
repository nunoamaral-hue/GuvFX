# ASCII-only (security.md RULE 9). Pester behavioural test for the VALUE-BLIND hosted-executor keyring-file
# PROVISIONER (GFX-HOSTED-EXECUTOR-FILE-BASED-KEYRING-144 / ADR-0112). SYNTHETIC keys only - NEVER a real secret.
# Run on a Windows PowerShell 5.1 runner with Pester 5.x (the 5.1 built-in Pester 3.4 does NOT support BeforeAll):
#   Install-Module Pester -MinimumVersion 5.0 -Force -SkipPublisherCheck
#   Invoke-Pester -Path .\Test-HostedExecutorKeyringFileProvisioner.Tests.ps1 -Output Detailed
# All setup lives in BeforeAll (Pester 5+ runs the file body in a DISCOVERY phase where top-level functions/vars
# are NOT available to the It run phase). The machine-variable read and the service-principal SID resolution are
# replaced by injected seams (synthetic ring JSON + active id; a resolver that maps the service principal to the
# RUNNER'S OWN SID so the ACL can be set without a real service account). The destination file is sandboxed under
# a temp path via -FixedPathForTest; the fixed filename remains non-overridable.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Describe 'hosted-executor keyring-file provisioner (synthetic)' {
    BeforeAll {
        $env:GUVFX_PROVISIONER_SELFTEST = '1'
        $script:Wrapper = Join-Path (Split-Path -Parent $PSScriptRoot) 'Provision-GuvfxHostedExecutorKeyringFile.ps1'
        # synthetic: distinctive secret values that MUST NOT appear in any result output (value-blind check)
        $script:RingV1 = '{"hx-20261115":"SYNTH_SECRET_VALUE_DO_NOT_LEAK_0001","hx-prev":"SYNTH_SECRET_VALUE_DO_NOT_LEAK_0002"}'
        $script:ActiveV1 = 'hx-20261115'
        $script:RingV2 = '{"hx-20261201":"SYNTH_SECRET_VALUE_DO_NOT_LEAK_0003"}'
        $script:ActiveV2 = 'hx-20261201'
        $script:LeakMarker = 'SYNTH_SECRET_VALUE_DO_NOT_LEAK'
        $script:RunnerSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

        function New-DestPath { return (Join-Path ([System.IO.Path]::GetTempPath()) ((([guid]::NewGuid().ToString('N'))) + '\hosted_executor_keyring.json')) }

        function Invoke-Prov {
            param(
                [string]$Dest, [string]$Ring = '__V1__', [string]$ActiveId = '__V1__',
                [switch]$Remove, [switch]$Verify, [scriptblock]$AfterTemp = $null, [switch]$NoSelfTest
            )
            if ($Ring -eq '__V1__') { $Ring = $script:RingV1 }
            if ($ActiveId -eq '__V1__') { $ActiveId = $script:ActiveV1 }
            $rsid = $script:RunnerSid
            $sidBlock = { param($ref) if ($ref -match '^S-1-') { return $ref } return $rsid }.GetNewClosure()
            $r = $Ring; $ringBlock = { return $r }.GetNewClosure()
            $a = $ActiveId; $idBlock = { return $a }.GetNewClosure()
            $p = @{
                FixedPathForTest = $Dest; ResolvePrincipalSid = $sidBlock;
                ReadMachineRing = $ringBlock; ReadMachineActiveId = $idBlock
            }
            if ($Remove) { $p['Remove'] = $true }
            if ($Verify) { $p['Verify'] = $true }
            if ($AfterTemp) { $p['AfterTempForTest'] = $AfterTemp }
            if ($NoSelfTest) { $env:GUVFX_PROVISIONER_SELFTEST = '' }
            try {
                $out = & $script:Wrapper @p 2>&1
            } finally {
                if ($NoSelfTest) { $env:GUVFX_PROVISIONER_SELFTEST = '1' }
            }
            $line = ($out | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
            return [pscustomobject]@{ Raw = ($out -join [Environment]::NewLine); Json = ($line | ConvertFrom-Json) }
        }
    }

    AfterEach {
        # best-effort cleanup of any sandbox dirs created this test
        Get-ChildItem -LiteralPath ([System.IO.Path]::GetTempPath()) -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'hosted_executor_keyring.json') } |
            ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } catch { } }
    }

    It 'provisions the file with a protected ACL and correct value-blind metadata' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest
        $r.Json.ok | Should -BeTrue
        $r.Json.reason | Should -Be 'provisioned'
        $r.Json.file_present | Should -BeTrue
        $r.Json.acl_verified | Should -BeTrue
        $r.Json.key_count | Should -Be 2
        $r.Json.active_present | Should -BeTrue
        Test-Path -LiteralPath $dest | Should -BeTrue
    }

    It 'never emits any secret value (value-blind)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest
        $r.Raw | Should -Not -Match $script:LeakMarker
        # the FILE legitimately contains the synthetic secret; the OUTPUT must not
        (Get-Content -LiteralPath $dest -Raw) | Should -Match $script:LeakMarker
    }

    It 'writes a well-formed {keyring,key_id} document the daemon can parse' {
        $dest = New-DestPath
        Invoke-Prov -Dest $dest | Out-Null
        $doc = (Get-Content -LiteralPath $dest -Raw) | ConvertFrom-Json
        @($doc.keyring.PSObject.Properties).Count | Should -Be 2
        $doc.key_id | Should -Be 'hx-20261115'
    }

    It 'verify mode confirms a correctly provisioned file' {
        $dest = New-DestPath
        Invoke-Prov -Dest $dest | Out-Null
        $r = Invoke-Prov -Dest $dest -Verify
        $r.Json.ok | Should -BeTrue
        $r.Json.reason | Should -Be 'verified'
        $r.Json.acl_verified | Should -BeTrue
        $r.Json.active_present | Should -BeTrue
    }

    It 'refuses an absent machine ring (fail closed)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -Ring ''
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'machine_ring_absent'
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It 'refuses a malformed machine ring (fail closed)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -Ring '{not valid json'
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'machine_ring_invalid_json'
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It 'refuses an empty-string secret in the ring (fail closed)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -Ring '{"hx-20261115":""}'
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'machine_ring_empty_secret'
    }

    It 'refuses an absent active id (fail closed)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -ActiveId ''
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'active_id_absent'
    }

    It 'refuses an active id that is not a key in the ring (fail closed)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -ActiveId 'hx-not-in-ring'
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'active_id_not_in_ring'
    }

    It 'rotates: a second provision replaces the file with the new keyring' {
        $dest = New-DestPath
        Invoke-Prov -Dest $dest | Out-Null
        $r = Invoke-Prov -Dest $dest -Ring $script:RingV2 -ActiveId $script:ActiveV2
        $r.Json.ok | Should -BeTrue
        $r.Json.key_count | Should -Be 1
        $doc = (Get-Content -LiteralPath $dest -Raw) | ConvertFrom-Json
        $doc.key_id | Should -Be 'hx-20261201'
        @($doc.keyring.PSObject.Properties).Count | Should -Be 1
    }

    It 'rolls back: -Remove deletes the file (daemon then falls back to env)' {
        $dest = New-DestPath
        Invoke-Prov -Dest $dest | Out-Null
        $r = Invoke-Prov -Dest $dest -Remove
        $r.Json.ok | Should -BeTrue
        $r.Json.reason | Should -Be 'removed'
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It 'refuses every test seam when the self-test signal is not set (un-bypassable in production)' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -NoSelfTest
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'test_seam_in_production'
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It 'fails closed and cleans up when the write path throws after the restricted temp write' {
        $dest = New-DestPath
        $r = Invoke-Prov -Dest $dest -AfterTemp { throw 'injected' }
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'write_failed'
        Test-Path -LiteralPath $dest | Should -BeFalse
        # no temp siblings left behind
        $dir = Split-Path -Parent $dest
        if (Test-Path -LiteralPath $dir) {
            @(Get-ChildItem -LiteralPath $dir -Filter '.hosted_executor_keyring.json.tmp.*' -Force -ErrorAction SilentlyContinue).Count | Should -Be 0
        }
    }

    It 'refuses the fixed-leaf violation if the sandbox path has the wrong filename' {
        $bad = Join-Path ([System.IO.Path]::GetTempPath()) ((([guid]::NewGuid().ToString('N'))) + '\wrong_name.json')
        $r = Invoke-Prov -Dest $bad
        $r.Json.ok | Should -BeFalse
        $r.Json.reason | Should -Be 'fixed_path_leaf_violation'
    }
}
