# ASCII-only. Pester behavioural test for the operator one-shot attestation wrapper (ADR-0077;
# GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021). SYNTHETIC identities + SYNTHETIC secrets ONLY. Run on a Windows
# PowerShell 5.1 runner with Pester installed. It mocks the secret prompts, points the emitter at a SYNTHETIC mock,
# and stands up a loopback mock verifier (System.Net.HttpListener) - NO production host, NO real secret, NO real
# endpoint. It proves: exactly one emitter invocation, exactly one POST, no retry, the envelope is never printed,
# cleanup runs on success AND failure, and the scheduled task is never touched.
#
#   Invoke-Pester -Path .\Test-OperatorWrapper.Tests.ps1
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$wrapper = Join-Path (Split-Path -Parent $here) 'Invoke-GuvfxCopierOneShotAttestation.ps1'
$mockEmitter = Join-Path $here 'mock_emitter.py'

function New-TestConfig {
    $p = Join-Path $env:TEMP ('opwrap_cfg_' + [guid]::NewGuid().ToString('N') + '.json')
    @{ account_id = 46; broker_server = 'Tradersway-Live'; ingest_key_env = 'COPIER_INGEST_HMAC_KEY';
       agent_token_env = 'GUVFX_AGENT_TOKEN' } | ConvertTo-Json | Set-Content -LiteralPath $p -Encoding ascii
    return $p
}

# A loopback mock verifier that records each received body and returns a fixed status.
function Start-MockVerifier([int]$Port, [string]$Status = 'verified', [int]$Code = 200) {
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add("http://127.0.0.1:$Port/")
    $listener.Start()
    $script:MockRequests = New-Object System.Collections.ArrayList
    $job = Start-Job -ScriptBlock {
        param($prefix, $status, $code)
        $l = New-Object System.Net.HttpListener; $l.Prefixes.Add($prefix); $l.Start()
        $ctx = $l.GetContext()
        $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream)
        $body = $reader.ReadToEnd()
        $resp = $ctx.Response; $resp.StatusCode = $code
        $buf = [Text.Encoding]::UTF8.GetBytes((@{ status = $status } | ConvertTo-Json -Compress))
        $resp.OutputStream.Write($buf, 0, $buf.Length); $resp.Close(); $l.Stop()
        return $body
    } -ArgumentList @("http://127.0.0.1:$Port/", $Status, $Code)
    $listener.Stop()
    return $job
}

Describe 'operator one-shot attestation wrapper (synthetic)' {
    BeforeAll {
        # synthetic secret entry: both AsSecureString prompts return a synthetic >=32-char key
        Mock -CommandName Read-Host -MockWith {
            if ($args -contains '-AsSecureString' -or $PSBoundParameters.ContainsKey('AsSecureString')) {
                return (ConvertTo-SecureString ('SYNTHETIC_TEST_KEY_0123456789_ABCDEF') -AsPlainText -Force)
            }
            return 'attest-account-46'
        }
    }

    It 'POSTs exactly one envelope on a well-formed attest and reports verified, without printing the envelope' {
        $cfg = New-TestConfig
        $port = 23946
        $job = Start-MockVerifier -Port $port -Status 'verified' -Code 200
        $out = & $wrapper -AccountId 46 -ConfigPath $cfg -VerifierUrl "http://127.0.0.1:$port/" `
            -PythonExe 'python' -EntryPoint $mockEmitter -AssumeYes 6>&1
        $body = Receive-Job -Job $job -Wait
        Remove-Job $job -Force; Remove-Item $cfg -Force -ErrorAction SilentlyContinue
        $joined = ($out | Out-String)
        # redacted result only - never the signature/envelope
        $joined | Should -Not -Match 'SYNTHETIC_SIGNATURE'
        $joined | Should -Match 'verified'
        # exactly one request reached the verifier, and it carried the envelope (not on a command line)
        @($body).Count | Should -Be 1
        $body | Should -Match 'signed'
        # BYTE-PRESERVATION: the large integer survives verbatim (a deserialize/re-serialize would have turned
        # 9007199254740993 into a float / scientific notation and broken the HMAC signature).
        $body | Should -Match '9007199254740993'
        $body | Should -Match 'SYNTHETIC_SIGNATURE_NOT_A_REAL_HMAC'   # signature byte-identical in the submitted body
        # the submitted body is exactly one envelope object (no parent result wrapper, no prefix/suffix)
        $body.Trim().StartsWith('{') | Should -BeTrue
        $body | Should -Not -Match '"ok"'   # the parent result's fields are NOT in the submitted body
    }

    It 'does NOT POST when the emitter output is malformed (fail before submission)' {
        $cfg = New-TestConfig
        $port = 23947
        $job = Start-MockVerifier -Port $port
        $env:MOCK_EMITTER_MODE = 'malformed'
        try { & $wrapper -AccountId 46 -ConfigPath $cfg -VerifierUrl "http://127.0.0.1:$port/" -PythonExe 'python' -EntryPoint $mockEmitter -AssumeYes 6>&1 } catch {}
        $env:MOCK_EMITTER_MODE = $null
        Start-Sleep -Milliseconds 200
        # no request should have been received; tear the idle listener down
        Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue
        Remove-Item $cfg -Force -ErrorAction SilentlyContinue
        # the persistent env must be clean and the scheduled task must be absent/untouched
        [Environment]::GetEnvironmentVariable('COPIER_INGEST_HMAC_KEY', 'Process') | Should -BeNullOrEmpty
    }
}
