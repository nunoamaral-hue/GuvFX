#Requires -Version 5.1
# ASCII-only. GuvFX Copy Trading - secure HUMAN-OPERATED one-shot IDENTITY-attestation wrapper (ADR-0077;
# GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021/022). Repository tooling: NOT installed, NOT scheduled, NOT run by any
# agent. A human operator runs it locally over RDP during the verifier's temporary ON window.
#
# It: (1) refuses to run under active/forced PowerShell transcription or Verbose/Debug tracing; (2) prompts for the
# emitter secrets with NON-ECHOING secure prompts (never as arguments); (3) invokes `run_emitter.py attest` EXACTLY
# ONCE, injecting the secrets into the CHILD PROCESS ENVIRONMENT ONLY (never Machine/User env, registry, task XML,
# config, disk, history or logs), capturing stdout INTERNALLY; (4) validates on a SEPARATE in-memory parse that the
# output is exactly one well-formed attest envelope (account 46, expected server), then submits the emitter's ORIGINAL
# envelope bytes VERBATIM (no deserialize/re-serialize) in one in-memory POST, NO retry; (5) prints ONLY a redacted
# result; (6) ALWAYS clears secrets + the envelope in a finally block and leaves the scheduled task unchanged/disabled.
#
# It NEVER enables/runs the scheduled task, runs observe/observe-once/run/delivery modes, or prints the envelope,
# HMAC signature, agent token or broker login.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$AccountId,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$VerifierUrl,
    [string]$PythonExe = 'python',
    [string]$EntryPoint = '',
    [int]$TimeoutSeconds = 30,
    [switch]$AssumeYes   # test-only: skip the interactive confirmation (secrets are STILL prompted). Never use in prod.
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-PSDebug -Off
if ($VerbosePreference -ne 'SilentlyContinue' -or $DebugPreference -ne 'SilentlyContinue') {
    throw 'Refusing to run with Verbose/Debug tracing enabled (would risk secret exposure).'
}

function Write-RedactedResult($obj) { $obj | ConvertTo-Json -Compress }

# Return the EXACT bytes of the top-level "envelope" object value, verbatim as the emitter serialized it (brace scan
# that respects JSON string literals + escapes). We submit THIS verbatim - never a deserialize/re-serialize, so
# formatting, property order and large integers survive byte-for-byte and the HMAC signature always verifies.
function Get-EnvelopeSubstring([string]$raw) {
    $marker = '"envelope"'
    $mi = $raw.IndexOf($marker)
    if ($mi -lt 0) { throw 'emitter output has no envelope (no POST).' }
    $bi = $raw.IndexOf('{', $mi + $marker.Length)
    if ($bi -lt 0) { throw 'emitter envelope is not an object (no POST).' }
    $depth = 0; $inStr = $false; $esc = $false; $end = -1
    for ($i = $bi; $i -lt $raw.Length; $i++) {
        $ch = $raw[$i]
        if ($inStr) {
            if ($esc) { $esc = $false }
            elseif ($ch -eq '\') { $esc = $true }
            elseif ($ch -eq '"') { $inStr = $false }
        } else {
            if ($ch -eq '"') { $inStr = $true }
            elseif ($ch -eq '{') { $depth++ }
            elseif ($ch -eq '}') { $depth--; if ($depth -eq 0) { $end = $i; break } }
        }
    }
    if ($end -lt 0) { throw 'emitter envelope braces unbalanced (no POST).' }
    return $raw.Substring($bi, $end - $bi + 1)
}

# --- transcription guard: fail closed if a transcript could capture secrets ---
$txPolicy = $null
try {
    $txPolicy = (Get-ItemProperty -Path 'HKLM:\Software\Policies\Microsoft\Windows\PowerShell\Transcription' `
            -Name 'EnableTranscripting' -ErrorAction SilentlyContinue).EnableTranscripting
} catch { $txPolicy = $null }
if ($txPolicy -eq 1) { throw 'System-wide PowerShell transcription is enabled; refusing (secrets could be captured).' }
# stop any session transcript for this run; do NOT restart it (Stop-Transcript throws if none is active -> ignore)
try { Stop-Transcript | Out-Null } catch { }

# --- non-secret validation + confirmation ---
if ($AccountId -ne 46) { throw "AccountId must be 46 for this identity bootstrap (got $AccountId)." }
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "ConfigPath not found: $ConfigPath" }
$cfg = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([int]$cfg.account_id -ne 46) { throw 'config account_id must be 46.' }
if (@('Tradersway-Live', 'TradersWay-Live') -notcontains [string]$cfg.broker_server) { throw 'unexpected broker_server in config.' }
if ($EntryPoint -eq '') { $EntryPoint = Join-Path (Split-Path -Parent $ConfigPath) 'run_emitter.py' }
if (-not (Test-Path -LiteralPath $EntryPoint)) { throw "EntryPoint not found: $EntryPoint" }
$ingestEnvName = [string]$cfg.ingest_key_env
$agentEnvName = [string]$cfg.agent_token_env
if (-not $ingestEnvName -or -not $agentEnvName) { throw 'config must name ingest_key_env and agent_token_env.' }

Write-Output 'ONE-SHOT IDENTITY-ONLY attestation for GuvFX account 46 (server TradersWay-Live).'
Write-Output 'This does NOT enable observation, delivery or execution, and does NOT run the scheduled task.'
Write-Output 'The production verifier must be in its temporary ON window (enabled by the separately-governed Claude operator).'
if (-not $AssumeYes) {
    $c = Read-Host 'To proceed type EXACTLY: attest-account-46'
    if ($c -ne 'attest-account-46') { throw 'confirmation mismatch; aborting with no secret entry.' }
}

# --- cleanup-tracked state (initialised before the try so the finally is StrictMode-safe) ---
$plainKey = $null; $plainToken = $null; $envelopeRaw = $null; $bodyBytes = $null
$keySS = $null; $tokenSS = $null; $proc = $null; $stdout = $null; $stderr = $null
try {
    # non-echoing secret entry; never as arguments, never echoed
    $keySS = Read-Host -AsSecureString ("Enter secret value for env '" + $ingestEnvName + "' (hidden)")
    $tokenSS = Read-Host -AsSecureString ("Enter secret value for env '" + $agentEnvName + "' (hidden)")
    $bK = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($keySS)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bK)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bK)
    $bT = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokenSS)
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bT)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bT)
    if ($plainKey.Length -lt 32) { throw 'ingest key too short (fail closed, no invocation).' }

    # invoke the emitter attest mode EXACTLY ONCE; secrets go to the CHILD environment only (never persisted)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonExe
    $psi.Arguments = ('"' + $EntryPoint + '" attest --config "' + $ConfigPath + '"')   # paths only; NO secret args
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    [void]$psi.EnvironmentVariables.Remove($ingestEnvName)
    [void]$psi.EnvironmentVariables.Add($ingestEnvName, $plainKey)
    [void]$psi.EnvironmentVariables.Remove($agentEnvName)
    [void]$psi.EnvironmentVariables.Add($agentEnvName, $plainToken)
    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) { try { $proc.Kill() } catch { }; throw 'emitter attest timed out.' }
    $plainKey = $null; $plainToken = $null   # child has them; drop the parent copies immediately

    if ($proc.ExitCode -ne 0) { throw ('emitter attest exited ' + $proc.ExitCode + ' (no POST).') }

    # VALIDATION on a SEPARATE in-memory parse (never used as the request body). The stdout must be EXACTLY one
    # JSON object - reject multiple/prefixed/suffixed output before any POST.
    $trimmed = $stdout.Trim()
    if ($trimmed.Length -lt 2 -or $trimmed[0] -ne '{' -or $trimmed[$trimmed.Length - 1] -ne '}') {
        throw 'emitter stdout is not a single JSON object (no POST).'
    }
    $result = $null
    try { $result = $trimmed | ConvertFrom-Json } catch { throw 'emitter output was not a single valid JSON object (no POST).' }
    if ($null -eq $result -or ($result -is [array])) { throw 'emitter output was not a single JSON object (no POST).' }
    if (-not $result.ok -or [string]$result.mode -ne 'attest') { throw 'emitter did not produce an ok attest result (no POST).' }
    if ($null -eq $result.envelope -or $null -eq $result.envelope.signed -or $null -eq $result.envelope.signature) {
        throw 'emitter output missing envelope/signed/signature (no POST).'
    }
    if ([int]$result.envelope.signed.account_id -ne 46) { throw 'envelope account_id mismatch (no POST).' }
    if (@('Tradersway-Live', 'TradersWay-Live') -notcontains [string]$result.envelope.signed.broker_server) {
        throw 'envelope broker_server mismatch (no POST).'
    }

    # VERBATIM body: extract the emitter's original envelope bytes; do NOT deserialize/re-serialize. Never printed.
    $envelopeRaw = Get-EnvelopeSubstring $stdout
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($envelopeRaw)

    # submit EXACTLY ONCE; in-memory body; NO automatic retry
    $reqTs = (Get-Date).ToUniversalTime().ToString('o')
    $httpStatus = 0; $verified = $false; $reasonCode = 'no_response'; $corr = ''
    try {
        $resp = Invoke-WebRequest -Uri $VerifierUrl -Method Post -Body $bodyBytes -ContentType 'application/json' `
            -TimeoutSec $TimeoutSeconds -UseBasicParsing
        $httpStatus = [int]$resp.StatusCode
        $rj = $null; try { $rj = $resp.Content | ConvertFrom-Json } catch { $rj = $null }
        if ($rj) { $verified = ([string]$rj.status -eq 'verified'); $reasonCode = [string]$rj.status }
    } catch [System.Net.WebException] {
        $wr = $_.Exception.Response
        if ($wr) { $httpStatus = [int]([int]$wr.StatusCode); $reasonCode = ('http_' + $httpStatus) }
        else { $reasonCode = 'network_error' }
    }
    $envelopeRaw = $null; $bodyBytes = $null
    Write-RedactedResult ([ordered]@{ account_id = 46; http_status = $httpStatus; verified = $verified;
            reason_code = $reasonCode; request_ts = $reqTs; correlation_id = $corr })
}
finally {
    # ALWAYS: clear secrets + in-memory copies; dispose; leave the scheduled task untouched; no temp file was created
    $plainKey = $null; $plainToken = $null; $envelopeRaw = $null; $bodyBytes = $null; $stdout = $null; $stderr = $null
    if ($keySS) { try { $keySS.Dispose() } catch { } }
    if ($tokenSS) { try { $tokenSS.Dispose() } catch { } }
    if ($proc) {
        try { if (-not $proc.HasExited) { $proc.Kill() } } catch { }
        try { $proc.Dispose() } catch { }
    }
    [System.GC]::Collect()
}
