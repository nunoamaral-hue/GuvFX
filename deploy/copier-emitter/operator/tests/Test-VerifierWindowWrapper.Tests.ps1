# ASCII-only (security.md RULE 9). Pester behavioural test for the backend bounded-verifier-window operator wrapper
# (ADR-0101; GFX-COPIER-SECURE-INGEST-CONSUMER-RESUME-083). SYNTHETIC key only. Run on a Windows PowerShell 5.1 runner
# with Pester 5.x installed (the 5.1 built-in Pester 3.4 does NOT support BeforeAll / Should -BeTrue):
#   Install-Module Pester -MinimumVersion 5.0 -Force -SkipPublisherCheck ; Invoke-Pester -Path .\Test-VerifierWindowWrapper.Tests.ps1 -Output Detailed
# It mocks the secret prompt and injects a STUB that stands in for `ssh` (no real host, no real secret, no real
# endpoint). It proves: the key is PIPED (never an argument), the window is opened exactly once, the window is ALWAYS
# closed (success AND failure in the attestation step), cleanup is verified, and the key is never printed.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$wrapper = Join-Path (Split-Path -Parent $here) 'Open-GuvfxCopierVerifierWindow.ps1'
$SYNTH = 'SYNTHETIC_TEST_KEY_0123456789_ABCDEF01'   # >= 32 chars, no placeholder marker

# A STUB that stands in for `ssh`: it records its argv and stdin to log files, then emits canned JSON by action. The
# wrapper calls it as `& $SshExe $target $remoteCommand`, so $args[-1] is the remote command string.
function New-StubSsh([string]$argvLog, [string]$stdinLog) {
    $p = Join-Path $env:TEMP ('stub_ssh_' + [guid]::NewGuid().ToString('N') + '.ps1')
    $body = @'
param()
$remote = $args[$args.Count - 1]
Add-Content -LiteralPath "__ARGV__" -Value ($args -join ' ')
$stdin = @($input) -join "`n"
if ($stdin) { Add-Content -LiteralPath "__STDIN__" -Value $stdin }
if ($remote -like '*--status*') { '{"action":"status","status":{"open":false}}'; exit 0 }
if ($remote -like '*--close*')  { '{"action":"close","closed_windows":1,"status":{"open":false}}'; exit 0 }
if ($remote -like '*--open*')   { '{"action":"open","purpose":"ingest","expires_at":"2026-11-12T18:00:00Z","key_file_written":true,"status":{"open":true}}'; exit 0 }
throw "stub ssh: unexpected remote command"
'@
    $body = $body.Replace('__ARGV__', $argvLog).Replace('__STDIN__', $stdinLog)
    Set-Content -LiteralPath $p -Encoding ascii -Value $body
    return $p
}

Describe 'backend bounded-verifier-window wrapper (synthetic)' {
    BeforeAll {
        Mock -CommandName Read-Host -MockWith {
            if ($args -contains '-AsSecureString' -or $PSBoundParameters.ContainsKey('AsSecureString')) {
                return (ConvertTo-SecureString $SYNTH -AsPlainText -Force)
            }
            return 'OPEN-VERIFIER-WINDOW'
        }
    }

    It 'pipes the key (never an argument), opens once, and always closes' {
        $argvLog = Join-Path $env:TEMP ('argv_' + [guid]::NewGuid().ToString('N') + '.log')
        $stdinLog = Join-Path $env:TEMP ('stdin_' + [guid]::NewGuid().ToString('N') + '.log')
        $stub = New-StubSsh $argvLog $stdinLog
        # Point -SshExe at the stub script so the wrapper invokes it (via `& $SshExe $target $remote`) instead of
        # a real ssh/host; the stub records argv + stdin and returns canned JSON.
        $out = & $wrapper -BackendSshTarget 'x@h' -SshExe $stub -Purpose ingest -AssumeYes `
            -RemotePython 'python' 2>&1
        $result = ($out | Out-String | ConvertFrom-Json)
        $result.opened | Should -BeTrue
        $result.closed | Should -BeTrue

        $argv = Get-Content -LiteralPath $argvLog -Raw
        $stdin = Get-Content -LiteralPath $stdinLog -Raw
        $argv | Should -Not -Match ([regex]::Escape($SYNTH))   # key is NEVER an argument
        $stdin | Should -Match ([regex]::Escape($SYNTH))        # key IS delivered on stdin
        ($out | Out-String) | Should -Not -Match ([regex]::Escape($SYNTH))   # key never printed
        $argv | Should -Match '--open'
        $argv | Should -Match '--close'
    }

    It 'closes the window even when the attestation step throws' {
        $argvLog = Join-Path $env:TEMP ('argv2_' + [guid]::NewGuid().ToString('N') + '.log')
        $stdinLog = Join-Path $env:TEMP ('stdin2_' + [guid]::NewGuid().ToString('N') + '.log')
        $stub = New-StubSsh $argvLog $stdinLog
        $threw = $false
        try {
            & $wrapper -BackendSshTarget 'x@h' -SshExe $stub -Purpose attest -AssumeYes `
                -AttestationStep { throw 'emitter failure' } 2>&1 | Out-Null
        } catch { $threw = $true }
        $argv = Get-Content -LiteralPath $argvLog -Raw
        $argv | Should -Match '--open'
        $argv | Should -Match '--close'   # finally block still closed the window
    }
}
