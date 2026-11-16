# ASCII-only (security.md RULE 9). Pester behavioural test for the host emitter ATTESTATION-GATE toggle
# (ADR-0104/ADR-0077; GFX-COPIER-ATTESTATION-GATE-143). Handles NO secret (config.json carries only env-var NAMES).
# Run on a Windows PowerShell 5.1 runner with Pester 5.x (the 5.1 built-in Pester 3.4 does NOT support BeforeAll):
#   Install-Module Pester -MinimumVersion 5.0 -Force -SkipPublisherCheck ; Invoke-Pester -Path .\Test-AttestationGate.Tests.ps1 -Output Detailed
# All setup lives in BeforeAll (Pester 5+ runs the file body in a DISCOVERY phase). Each test gets a FRESH temp config
# assembled line-by-line so token formatting (2-space indent, one space after colon, trailing commas) matches
# config.template.json exactly. SYNTHETIC identity values only; no secret anywhere.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Describe 'host emitter attestation-gate toggle (synthetic config, no secret)' {
    BeforeAll {
        $script:Wrapper = Join-Path (Split-Path -Parent $PSScriptRoot) 'Set-GuvfxCopierAttestationGate.ps1'

        # Assemble a complete, identity-filled config. $Attest may be 'false'|'true'|'OMIT'. -OmitObs drops the
        # observation_allowed line. delivery_allowed is always last (no trailing comma).
        function New-ConfigText {
            param(
                [string]$Attest = 'false', [string]$Obs = 'false', [string]$Del = 'false',
                [int]$Account = 46, [string]$Login = '99999', [string]$Server = 'Synthetic-Demo',
                [string]$HostId = 'WIN-SYNTH', [string]$TermFp = 'synthetic_terminal_fp_000000000000',
                [string]$Bridge = '127.0.0.1:8899', [string]$BridgeBase = 'http://127.0.0.1:8899',
                [string]$IngestUrl = 'https://api.guvfx.com/api/copy-trading/event-ingest/',
                [switch]$OmitObs
            )
            $L = New-Object System.Collections.Generic.List[string]
            $L.Add('{')
            $L.Add('  "account_id": ' + $Account + ',')
            $L.Add('  "broker_login": "' + $Login + '",')
            $L.Add('  "broker_server": "' + $Server + '",')
            $L.Add('  "host_id": "' + $HostId + '",')
            $L.Add('  "bridge_base_url": "' + $BridgeBase + '",')
            $L.Add('  "ingest_url": "' + $IngestUrl + '",')
            $L.Add('  "key_id": "copier-ingest",')
            $L.Add('  "terminal_fingerprint": "' + $TermFp + '",')
            $L.Add('  "bridge_id": "' + $Bridge + '",')
            $L.Add('  "ingest_key_env": "COPIER_INGEST_HMAC_KEY",')
            $L.Add('  "agent_token_env": "GUVFX_AGENT_TOKEN",')
            $L.Add('  "max_pending": 1000,')
            $L.Add('  "max_attempts": 5,')
            if ($Attest -ne 'OMIT') { $L.Add('  "attestation_allowed": ' + $Attest + ',') }
            if (-not $OmitObs) { $L.Add('  "observation_allowed": ' + $Obs + ',') }
            $L.Add('  "delivery_allowed": ' + $Del)
            $L.Add('}')
            return ($L -join "`n")
        }

        function New-ConfigFile {
            param([string]$Text)
            $p = Join-Path $env:TEMP ('attestgate_' + [guid]::NewGuid().ToString('N') + '.json')
            [System.IO.File]::WriteAllText($p, $Text, (New-Object System.Text.UTF8Encoding($false)))
            return $p
        }

        function Run-Toggle {
            param([string]$ConfigPath, [switch]$Enable, [switch]$Disable, [switch]$Status)
            $a = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $script:Wrapper, '-ConfigPath', $ConfigPath)
            if ($Enable) { $a += '-Enable' }
            if ($Disable) { $a += '-Disable' }
            if ($Status) { $a += '-Status' }
            $out = & powershell @a 2>&1
            $text = ($out | Out-String)
            $json = $null
            foreach ($line in ($text -split "`n")) {
                $t = $line.Trim()
                if ($t.StartsWith('{')) { try { $json = $t | ConvertFrom-Json } catch { } }
            }
            return $json
        }

        # assert a refusal did NOT mutate the file (byte-for-byte).
        function Assert-Unchanged {
            param([string]$Path, [byte[]]$Before)
            $after = [System.IO.File]::ReadAllBytes($Path)
            (,$after) | Should -Not -BeNullOrEmpty
            [System.Convert]::ToBase64String($after) | Should -Be ([System.Convert]::ToBase64String($Before))
        }
    }

    It '-Status reports all gates false and identity complete on a fresh config' {
        $p = New-ConfigFile (New-ConfigText)
        $r = Run-Toggle -ConfigPath $p -Status
        $r.ok | Should -BeTrue
        $r.attestation_allowed | Should -BeFalse
        $r.observation_allowed | Should -BeFalse
        $r.delivery_allowed | Should -BeFalse
        $r.identity_complete | Should -BeTrue
        $r.false_token_count | Should -Be 1
        $r.true_token_count | Should -Be 0
        $r.account_id | Should -Be 46
    }

    It '-Enable flips attestation_allowed false->true, backs up equal to the original, leaves obs/delivery false' {
        $text = New-ConfigText
        $p = New-ConfigFile $text
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeTrue
        $r.attestation_allowed | Should -BeTrue
        $r.observation_allowed | Should -BeFalse
        $r.delivery_allowed | Should -BeFalse
        $r.backup | Should -Not -BeNullOrEmpty
        (Test-Path -LiteralPath $r.backup) | Should -BeTrue
        # the backup is byte-identical to the pre-mutation file (rollback-first integrity).
        [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($r.backup)) | Should -Be ([System.Convert]::ToBase64String($before))
        ((Get-Content -LiteralPath $p -Raw | ConvertFrom-Json).attestation_allowed) | Should -BeTrue
    }

    It '-Disable flips true->false' {
        $p = New-ConfigFile (New-ConfigText -Attest 'true')
        $r = Run-Toggle -ConfigPath $p -Disable
        $r.ok | Should -BeTrue
        $r.attestation_allowed | Should -BeFalse
    }

    It 'round-trips (enable then disable) back to attestation_allowed=false' {
        $p = New-ConfigFile (New-ConfigText)
        Run-Toggle -ConfigPath $p -Enable | Out-Null
        ((Get-Content -LiteralPath $p -Raw | ConvertFrom-Json).attestation_allowed) | Should -BeTrue
        Run-Toggle -ConfigPath $p -Disable | Out-Null
        ((Get-Content -LiteralPath $p -Raw | ConvertFrom-Json).attestation_allowed) | Should -BeFalse
    }

    It 'preserves every identity field byte-for-byte (regression)' {
        $p = New-ConfigFile (New-ConfigText -Login '77777' -Server 'Regression-Server' -HostId 'WIN-REG' -TermFp 'fp_regression_0000000000000000' -Bridge '10.1.2.3:8810')
        $before = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
        Run-Toggle -ConfigPath $p -Enable | Out-Null
        $after = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
        foreach ($f in 'account_id','broker_login','broker_server','host_id','bridge_base_url','ingest_url','terminal_fingerprint','bridge_id','ingest_key_env','agent_token_env','max_pending','max_attempts') {
            "$($after.$f)" | Should -Be "$($before.$f)"
        }
    }

    It '-Enable is idempotent (already true -> noop, no backup)' {
        $p = New-ConfigFile (New-ConfigText -Attest 'true')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeTrue
        $r.noop | Should -BeTrue
        $r.reason | Should -Be 'already_in_target_state'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses an incomplete identity (empty terminal_fingerprint) WITHOUT mutating' {
        $p = New-ConfigFile (New-ConfigText -TermFp '')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'identity_incomplete_refusing'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses an incomplete identity (empty host_id) WITHOUT mutating (mirrors require_complete_identity)' {
        $p = New-ConfigFile (New-ConfigText -HostId '')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'identity_incomplete_refusing'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses when observation_allowed is already true WITHOUT mutating' {
        $p = New-ConfigFile (New-ConfigText -Obs 'true')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'observation_or_delivery_allowed_refusing'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses when delivery_allowed is already true WITHOUT mutating' {
        $p = New-ConfigFile (New-ConfigText -Del 'true')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'observation_or_delivery_allowed_refusing'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses a reserved account id (<=1) WITHOUT mutating' {
        $p = New-ConfigFile (New-ConfigText -Account 1)
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'account_id_reserved'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'refuses when the attestation_allowed token is absent (count 0) WITHOUT mutating' {
        $p = New-ConfigFile (New-ConfigText -Attest 'OMIT')
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Run-Toggle -ConfigPath $p -Enable
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'attestation_token_not_unique'
        Assert-Unchanged -Path $p -Before $before
    }

    It 'does NOT throw on an absent optional key (StrictMode-safe); omitting observation_allowed enables cleanly' {
        $p = New-ConfigFile (New-ConfigText -OmitObs)
        $r = Run-Toggle -ConfigPath $p -Status
        $r.ok | Should -BeTrue
        $r.observation_allowed | Should -BeFalse
        $r2 = Run-Toggle -ConfigPath $p -Enable
        $r2.ok | Should -BeTrue
        $r2.attestation_allowed | Should -BeTrue
    }

    It 'reports config_missing for an absent path' {
        $p = Join-Path $env:TEMP ('attestgate_absent_' + [guid]::NewGuid().ToString('N') + '.json')
        $r = Run-Toggle -ConfigPath $p -Status
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'config_missing'
    }

    It 'reports config_unparsable for malformed JSON' {
        $p = New-ConfigFile '{ not json'
        $r = Run-Toggle -ConfigPath $p -Status
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'config_unparsable'
    }

    It 'errors when neither -Enable nor -Disable is given (and not -Status)' {
        $p = New-ConfigFile (New-ConfigText)
        $r = Run-Toggle -ConfigPath $p
        $r.ok | Should -BeFalse
        $r.reason | Should -Be 'specify_exactly_one_of_Enable_Disable_Status'
    }

    It 'never emits a secret or key-like value in any output' {
        $p = New-ConfigFile (New-ConfigText)
        $all = @()
        $all += (Run-Toggle -ConfigPath $p -Status | ConvertTo-Json -Compress)
        $all += (Run-Toggle -ConfigPath $p -Enable | ConvertTo-Json -Compress)
        $all += (Run-Toggle -ConfigPath $p -Disable | ConvertTo-Json -Compress)
        foreach ($blob in $all) {
            $blob.ToLower() | Should -Not -Match 'secret|password|hmac|private'
        }
    }
}
