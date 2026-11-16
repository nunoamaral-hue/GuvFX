#Requires -Version 5.1
# ASCII-only (security.md RULE 9: must parse under Windows PowerShell 5.1 with no BOM). GuvFX Copy Trading -
# host emitter ATTESTATION-GATE toggle (ADR-0104/ADR-0101/ADR-0077; GFX-COPIER-ATTESTATION-GATE-143).
# Repository tooling: NOT installed, NOT scheduled, NOT run by any agent. A human operator runs it locally over RDP
# during one Sponsor-gated attestation session, as part of the documented three-gate enable/teardown sequence.
#
# WHY THIS EXISTS
#   The one-shot identity attestation needs THREE independent gates ON (see COPIER_ONE_SHOT_ATTESTATION_RUNBOOK.md):
#     1. backend COPIER_ENABLED            (master gate)
#     2. backend COPIER_ATTESTATION_VERIFY_ENABLED  (the attest-verify endpoint)
#     3. host emitter config attestation_allowed=true (checked by EmitterApp.attest() in app.py BEFORE any key use,
#        identity probe or POST; see trade_copier/emitter/app.py). Gate 3 is HOST-side and INVISIBLE to the backend,
#        so a backend-only enable can never satisfy it - the attestation returns reason "attestation_gate_off".
#   This wrapper is the reviewed, reversible way to toggle ONLY gate 3, leaving observation_allowed and
#   delivery_allowed OFF (execution/observation stay DARK) and every identity field untouched.
#
# SECURITY / SAFETY CONTRACT (fail closed)
#   * Handles NO secret: config.json carries only the NAMES of env vars that hold secrets, never a key. This script
#     never reads, derives, prints or logs any secret; its output is redacted JSON only.
#   * Single-field edit: a unique literal-token replace of the attestation_allowed line, so identity fields
#     (account_id/broker_login/broker_server/host_id/bridge_base_url/ingest_url/terminal_fingerprint/bridge_id) are
#     preserved byte-for-byte.
#   * Rollback-first: the config is backed up BEFORE mutation; on ANY post-write verify mismatch OR write exception
#     the backup is restored and the tool fails closed.
#   * Identity completeness mirrors the authoritative EmitterConfig.require_complete_identity() (account_id>1,
#     broker_login, broker_server, host_id, bridge_base_url, ingest_url) PLUS terminal_fingerprint + bridge_id, which
#     the attestation envelope carries and must match the backend's expected identity for the verify to succeed.
#   * Refuses: a missing/invalid config, an incomplete identity, a reserved account id (<=1), observation_allowed or
#     delivery_allowed already true (must stay OFF), or an attestation_allowed token that is not present EXACTLY once
#     across both states (false+true). A missing key reads as absent -> treated OFF/empty (fail closed).
#   * Never enables/triggers a task, opens a verifier window, provisions a key, or contacts the backend/broker/#48.
[CmdletBinding()]
param(
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Status,
    [string]$ConfigPath = 'C:\GuvFX\copier-emitter\46\config.json'
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$FALSE_TOK = '"attestation_allowed": false,'
$TRUE_TOK  = '"attestation_allowed": true,'

function Emit($o) { $o | ConvertTo-Json -Compress; }

# StrictMode-safe property read: a missing property reads as $null (matching the emitter's "missing/malformed => OFF"
# rule) instead of throwing.
function Get-Prop($obj, [string]$name) {
    $p = $obj.PSObject.Properties[$name]
    if ($p) { return $p.Value } else { return $null }
}
function Get-Str($obj, [string]$name) { return ([string](Get-Prop $obj $name)).Trim() }

function Test-IdentityComplete($obj) {
    $acct = 0
    try { $acct = [int](Get-Prop $obj 'account_id') } catch { $acct = 0 }
    if ($acct -le 1) { return $false }
    foreach ($f in 'broker_login', 'broker_server', 'host_id', 'bridge_base_url', 'ingest_url',
        'terminal_fingerprint', 'bridge_id') {
        if ([string]::IsNullOrEmpty((Get-Str $obj $f))) { return $false }
    }
    return $true
}

if (-not (Test-Path -LiteralPath $ConfigPath)) { Emit(@{ ok = $false; reason = 'config_missing' }); exit 1 }

$raw = [System.IO.File]::ReadAllText($ConfigPath)
try { $obj = $raw | ConvertFrom-Json } catch { Emit(@{ ok = $false; reason = 'config_unparsable' }); exit 1 }

$falseN = ([regex]::Matches($raw, [regex]::Escape($FALSE_TOK))).Count
$trueN = ([regex]::Matches($raw, [regex]::Escape($TRUE_TOK))).Count

$acctId = 0
try { $acctId = [int](Get-Prop $obj 'account_id') } catch { $acctId = 0 }

if ($Status) {
    Emit(@{
        ok = $true
        attestation_allowed = [bool](Get-Prop $obj 'attestation_allowed')
        observation_allowed = [bool](Get-Prop $obj 'observation_allowed')
        delivery_allowed = [bool](Get-Prop $obj 'delivery_allowed')
        identity_complete = (Test-IdentityComplete $obj)
        account_id = $acctId
        false_token_count = $falseN
        true_token_count = $trueN
    })
    exit 0
}

if (-not ($Enable -xor $Disable)) { Emit(@{ ok = $false; reason = 'specify_exactly_one_of_Enable_Disable_Status' }); exit 2 }

# fail closed: never attest a reserved account or a half-identity; execution-adjacent gates MUST stay off.
if ($acctId -le 1) { Emit(@{ ok = $false; reason = 'account_id_reserved' }); exit 1 }
if (-not (Test-IdentityComplete $obj)) { Emit(@{ ok = $false; reason = 'identity_incomplete_refusing' }); exit 1 }
if ([bool](Get-Prop $obj 'observation_allowed') -or [bool](Get-Prop $obj 'delivery_allowed')) {
    Emit(@{ ok = $false; reason = 'observation_or_delivery_allowed_refusing' }); exit 1
}

# the attestation_allowed token must be present EXACTLY once across BOTH states (reject a conflicting duplicate).
if (($falseN + $trueN) -ne 1) { Emit(@{ ok = $false; reason = 'attestation_token_not_unique'; false = $falseN; true = $trueN }); exit 1 }

$from = if ($Enable) { $FALSE_TOK } else { $TRUE_TOK }
$to   = if ($Enable) { $TRUE_TOK }  else { $FALSE_TOK }
$fromN = if ($Enable) { $falseN } else { $trueN }
if ($fromN -eq 0) { Emit(@{ ok = $true; reason = 'already_in_target_state'; noop = $true }); exit 0 }

# rollback-first: back up BEFORE mutating; restore on ANY verify mismatch OR write exception.
$bak = $ConfigPath + '.bak-attestgate-' + ([DateTime]::UtcNow).ToString('yyyyMMddTHHmmssZ')
Copy-Item -LiteralPath $ConfigPath -Destination $bak -Force
try {
    $new = $raw.Replace($from, $to)
    [System.IO.File]::WriteAllText($ConfigPath, $new, (New-Object System.Text.UTF8Encoding($false)))
    $v = ([System.IO.File]::ReadAllText($ConfigPath)) | ConvertFrom-Json
    $want = [bool]$Enable
    if ((([bool](Get-Prop $v 'attestation_allowed')) -ne $want) -or [bool](Get-Prop $v 'observation_allowed') `
            -or [bool](Get-Prop $v 'delivery_allowed') -or (-not (Test-IdentityComplete $v))) {
        throw 'verify_mismatch'
    }
}
catch {
    Copy-Item -LiteralPath $bak -Destination $ConfigPath -Force   # restore on ANY failure (fail closed)
    Emit(@{ ok = $false; reason = 'verify_failed_rolled_back' }); exit 1
}
Emit(@{
    ok = $true
    attestation_allowed = [bool](Get-Prop $v 'attestation_allowed')
    observation_allowed = [bool](Get-Prop $v 'observation_allowed')
    delivery_allowed = [bool](Get-Prop $v 'delivery_allowed')
    backup = $bak
})
exit 0
