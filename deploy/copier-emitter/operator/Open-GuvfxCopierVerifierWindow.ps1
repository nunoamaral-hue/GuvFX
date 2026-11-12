#Requires -Version 5.1
# ASCII-only (security.md RULE 9: must parse under Windows PowerShell 5.1 with no BOM). GuvFX Copy Trading -
# secure HUMAN-OPERATED backend bounded-verifier-window wrapper (ADR-0101; GFX-COPIER-SECURE-INGEST-CONSUMER-RESUME-083).
# Repository tooling: NOT installed, NOT scheduled, NOT run by any agent. A human operator runs it during a single
# Sponsor-gated verification session.
#
# It: (1) refuses to run under active/forced PowerShell transcription or Verbose/Debug tracing; (2) prompts for the
# backend ingest key with a NON-ECHOING secure prompt (never as an argument); (3) opens ONE bounded verifier window on
# the backend by PIPING the key to the containerized `copier_verifier_window --open` command over STDIN (the key is
# never an argument, env var, file, or log line on either host); (4) optionally runs a caller-supplied one-shot
# attestation/ingestion step while the window is open; (5) ALWAYS, in a finally block, closes the window
# (`copier_verifier_window --close`, which shreds the tmpfs key), verifies cleanup (`--status` reports open:false),
# clears the plaintext key, and leaves the verifier gate for Role A to restore OFF; (6) prints ONLY a redacted result.
#
# It NEVER enables the scheduled emitter task, NEVER writes the key to argv/env/disk/registry/history/logs, and NEVER
# prints the key, HMAC signature, agent token or broker login.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackendSshTarget,   # e.g. ubuntu@<host>
    [string]$BackendContainer = 'guvfx-backend',
    [ValidateSet('ingest', 'attest', 'both')][string]$Purpose = 'ingest',
    [ValidateRange(30, 900)][int]$TtlSeconds = 300,
    [ValidateRange(1, 50)][int]$MaxUses = 1,
    [string]$KeyPath = '',                                      # '' -> command default (/dev/shm/...)
    [string]$SshExe = 'ssh',
    [string]$RemotePython = 'python',
    [scriptblock]$AttestationStep = $null,                     # optional one-shot to run while the window is open
    [switch]$AssumeYes                                         # test-only: skip the interactive confirmation
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-PSDebug -Off
if ($VerbosePreference -ne 'SilentlyContinue' -or $DebugPreference -ne 'SilentlyContinue') {
    throw 'Refusing to run with Verbose/Debug tracing enabled (would risk secret exposure).'
}
try {
    if ((Get-Variable -Name 'Transcript' -Scope Global -ErrorAction SilentlyContinue)) {
        throw 'Refusing to run while a PowerShell transcript is active.'
    }
} catch { if ($_.Exception.Message -like 'Refusing*') { throw } }

function Write-RedactedResult($obj) { $obj | ConvertTo-Json -Compress }

# Build the remote argv for the containerized management command. NEVER includes the key.
function Get-RemoteCommand([string]$action) {
    $base = "docker exec -i $BackendContainer $RemotePython manage.py copier_verifier_window"
    switch ($action) {
        'open' {
            $cmd = "$base --open --purpose $Purpose --ttl $TtlSeconds --max-uses $MaxUses"
            if ($KeyPath) { $cmd = "$cmd --key-path $KeyPath" }
            return $cmd
        }
        'close'  { return "$base --close" }
        'status' { return "$base --status" }
        default  { throw "unknown action $action" }
    }
}

# Run a remote NON-SECRET command (status/close). Returns stdout. Overridable/mocked in tests.
function Invoke-RemoteCommand([string]$action) {
    $remote = Get-RemoteCommand $action
    return & $SshExe $BackendSshTarget $remote
}

# Open the window by piping the plaintext key to the remote command's STDIN. The key is written to the pipeline for
# exactly this call and never appears in argv. Overridable/mocked in tests.
function Invoke-RemoteOpenWithKey([string]$plainKey) {
    $remote = Get-RemoteCommand 'open'
    return ($plainKey | & $SshExe $BackendSshTarget $remote)
}

function Assert-WindowClosed() {
    $raw = Invoke-RemoteCommand 'status'
    $obj = $raw | ConvertFrom-Json
    if ($obj.status.open) { throw 'verifier window is still OPEN after close (cleanup failed).' }
}

# ---- preflight: no window must be open before we start ----
$preRaw = Invoke-RemoteCommand 'status'
$pre = $preRaw | ConvertFrom-Json
if ($pre.status.open) { throw 'a verifier window is already OPEN; refusing to open a second (single-flight).' }

if (-not $AssumeYes) {
    $phrase = Read-Host 'Type OPEN-VERIFIER-WINDOW to proceed'
    if ($phrase -ne 'OPEN-VERIFIER-WINDOW') { throw 'confirmation phrase mismatch; aborting.' }
}

$secure = Read-Host 'Enter the backend ingest key (hidden)' -AsSecureString
$plain = $null
$bstr = [IntPtr]::Zero
$result = [ordered]@{ action = 'verifier-window'; opened = $false; attested = $false; closed = $false }
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    $openRaw = Invoke-RemoteOpenWithKey $plain
    $open = $openRaw | ConvertFrom-Json
    if ($open.action -ne 'open') { throw 'open did not confirm (no window).' }
    $result.opened = $true
    $result.purpose = $open.purpose
    $result.expires_at = $open.expires_at

    if ($AttestationStep) {
        # The caller's one-shot attestation/ingestion runs here, while the window is OPEN. It must emit ONLY a
        # redacted result and MUST NOT receive or print the key (which this wrapper never passes to it).
        $att = & $AttestationStep
        $result.attested = $true
        $result.attestation = $att
    }
}
finally {
    # Guaranteed cleanup on EVERY path (success / failure / timeout / interruption): shred + close, then clear the key.
    try {
        [void](Invoke-RemoteCommand 'close')
        Assert-WindowClosed
        $result.closed = $true
    } catch {
        $result.closed = $false
        $result.close_error = $_.Exception.Message
    }
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($plain) { $plain = $null }
    $secure = $null
    [GC]::Collect()
}

Write-RedactedResult $result
if (-not $result.closed) { exit 2 }
