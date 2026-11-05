# ASCII-only. GuvFX Copy Trading host emitter ONE-SHOT identity attestation (ADR-0077). Repository artifact; DARK.
# Runs the emitter's one-shot attestation mode (python <EntryPoint> attest --config <cfg>). It does NOT start the
# task, does NOT begin the observation loop, and reads ONLY account identity (never positions). The emitter's own
# JSON (the signed attestation envelope + the key id, never the secret) is passed through as the final
# line. Attestation is itself gated OFF in the config unless attestation_allowed is set.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$AccountId,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$EntryPoint,
    [string]$PythonExe = "python"
)
$ErrorActionPreference = "Stop"

function Emit($obj) { $obj | ConvertTo-Json -Compress }
if ($AccountId -le 1) { Emit(@{ ok = $false; reason = "account_id_reserved" }); exit 1 }
if (-not (Test-Path -LiteralPath $ConfigPath)) { Emit(@{ ok = $false; reason = "config_missing" }); exit 1 }
if (-not (Test-Path -LiteralPath $EntryPoint)) { Emit(@{ ok = $false; reason = "entrypoint_missing" }); exit 1 }

# Delegate to the emitter's attestation mode; capture its final JSON line verbatim (it contains no secret).
$out = & $PythonExe "$EntryPoint" attest --config "$ConfigPath" 2>$null
$last = ($out | Select-Object -Last 1)
if (-not $last) { Emit(@{ ok = $false; reason = "no_attestation_output" }); exit 1 }
# Pass through the emitter's JSON unchanged.
$last
exit 0
