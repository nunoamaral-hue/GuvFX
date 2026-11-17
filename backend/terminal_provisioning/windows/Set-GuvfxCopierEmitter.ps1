<#
  ADR-0117 (GFX-COPIER-PRODUCTION-ACTUATORS-PIPELINE-153) - governed-arm actuators for the STANDING immediate
  emitter task GuvFX_CopierEmitter_<AccountId>. Repository artifact; DARK (nothing installs/enables/runs this
  until a separately-governed, Sponsor-gated step does). ASCII-only (RULE 9: must parse under Windows PowerShell
  5.1 with no BOM).

  Runs as the LocalSystem hosted-executor daemon. It performs NO trading and reads NO secret VALUE; it
  ORCHESTRATES one of four fixed modes against the pre-installed STANDING emitter task + its host-local ingest
  key:
    -Mode Enable    : verify the task exists, its action points ONLY at run_emitter.py 'run' + the account config
                      (never the one-shot attest), and its principal is guvfx_u_<id>; then ENABLE it. Idempotent.
    -Mode Disable   : /End a running instance then DISABLE the task; verify disabled. Idempotent (absent => ok).
    -Mode RemoveKey : best-effort shred + delete the host-local standing ingest key
                      (C:\GuvFX\copier-emitter\<id>\ingest.key); refuses a reparse target. Idempotent.
    -Mode Read      : VALUE-BLIND read-back only (task enabled/disabled + ingest.key presence/ACL metadata).
                      NEVER reads or returns the key or any key-derived value.

  Security boundary (architecture.md Windows-primitive layer): every identity/path/task name is DERIVED here from
  the server-supplied -AccountId; nothing (command/script/path/task/url/secret) is accepted from the caller.
  Customer Zero (account 1) is refused, and the emitter is defence-in-depth PINNED to the sole account it exists
  for (46). It can reach NO order, NO follower account, and NO execution/replication/ingestion path - it only
  toggles / reads one fixed task and shreds one fixed key file. Fail-closed everywhere; task state is read
  locale-independently from the task XML (RULE 11), never from a localised status string.

  Usage (invoked by the primitive runner with a fixed argv):
    powershell -NoProfile -File Set-GuvfxCopierEmitter.ps1 -Username guvfx_u_46 -AccountId 46 -Mode Enable
#>
param(
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][ValidateSet("Enable","Disable","RemoveKey","Read")][string]$Mode
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# The standing immediate emitter exists ONLY for this account. Customer Zero is refused separately.
$EMITTER_ACCOUNT = 46
$EMITTER_BASE = "C:\GuvFX\copier-emitter"

# The single compact-JSON verdict. Every UNPROVEN fact stays its fail-closed default; the ingest key NEVER appears.
$v = [ordered]@{
  ok=$false; account_id=$AccountId; mode=$Mode;
  emitter_task_enabled=$false; emitter_task_disabled=$false;
  ingest_key_present=$false; ingest_key_acl_ok=$false; reason=""
}
function Emit([string]$why) { if ($why) { $v.reason = $why }; ($v | ConvertTo-Json -Compress -Depth 5); if (-not $v.ok) { exit 1 } else { exit 0 } }

function Resolve-SidValue([string]$ref) {
  if ([string]::IsNullOrWhiteSpace($ref)) { return $null }
  try {
    if ($ref -match '^S-1-') { return (New-Object System.Security.Principal.SecurityIdentifier($ref)).Value }
    $acct = $ref; if ($ref -notmatch '\\') { $acct = "$env:COMPUTERNAME\$ref" }
    return (New-Object System.Security.Principal.NTAccount($acct)).Translate([System.Security.Principal.SecurityIdentifier]).Value
  } catch { return $null }
}

function Read-TaskXml([string]$taskName) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $out = (& schtasks.exe /query /tn ("\" + $taskName) /xml ONE 2>$null) -join "`n" } finally { $ErrorActionPreference = $prev }
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($out)) { return $null }
  return [string]$out
}

try {
  # Identity contract - runs BEFORE any mutation, so a refusal leaves the host exactly as it was.
  if ($AccountId -le 0 -or $AccountId -eq 1) { Emit "reserved_or_invalid_account" }
  if ($AccountId -ne $EMITTER_ACCOUNT) { Emit "account_not_emitter_pinned" }
  $expectedUser = "guvfx_u_$AccountId"
  if ($Username -ne $expectedUser) { Emit "username_mismatch" }

  $taskName = "GuvFX_CopierEmitter_$AccountId"
  $emitterDir = Join-Path $EMITTER_BASE "$AccountId"
  $keyPath = Join-Path $emitterDir "ingest.key"
  $configLeaf = "config.json"

  # ----------------------------------------- READ (value-blind, read-only) -----------------------------
  if ($Mode -eq "Read") {
    $xml = Read-TaskXml $taskName
    if ($xml) {
      $v.emitter_task_enabled = ($xml -match "<Enabled>true</Enabled>")
      $v.emitter_task_disabled = ($xml -match "<Enabled>false</Enabled>")
    }
    if (Test-Path -LiteralPath $keyPath) {
      $item = Get-Item -LiteralPath $keyPath -Force
      $isReparse = (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint)
      $v.ingest_key_present = (-not $isReparse -and -not ($item -is [System.IO.DirectoryInfo]))
      if ($v.ingest_key_present) {
        try {
          $acl = Get-Acl -LiteralPath $keyPath
          $tenantSid = Resolve-SidValue $expectedUser
          $allowed = @("S-1-5-18", "S-1-5-32-544", $tenantSid)
          $aclOk = $acl.AreAccessRulesProtected -and ($null -ne $tenantSid)
          foreach ($rule in $acl.Access) {
            if ($rule.IsInherited) { $aclOk = $false; break }
            $sid = $null; try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $aclOk = $false; break }
            if ($allowed -notcontains $sid) { $aclOk = $false; break }
          }
          $v.ingest_key_acl_ok = [bool]$aclOk
        } catch { $v.ingest_key_acl_ok = $false }
      }
    }
    $v.ok = $true     # a read that ran is ok; the booleans carry the posture
    Emit ""
  }

  # ----------------------------------------- REMOVEKEY (shred; teardown) -------------------------------
  if ($Mode -eq "RemoveKey") {
    if (Test-Path -LiteralPath $keyPath) {
      $item = Get-Item -LiteralPath $keyPath -Force
      if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) {
        Emit "remove_target_reparse"
      }
      try {
        $size = [int]$item.Length
        $fs = [System.IO.File]::Open($keyPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write)
        try { $zero = New-Object byte[] ([Math]::Max($size, 1)); $fs.Write($zero, 0, $zero.Length); $fs.Flush() } finally { $fs.Close() }
      } catch { }
      Remove-Item -LiteralPath $keyPath -Force -ErrorAction SilentlyContinue
    }
    $v.ingest_key_present = (Test-Path -LiteralPath $keyPath)
    $v.ok = (-not $v.ingest_key_present)
    Emit ($(if ($v.ok) { "" } else { "remove_failed" }))
  }

  # For Enable/Disable the task must exist. Read its XML once (locale-independent).
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

  # ----------------------------------------- DISABLE (idempotent; risk-reducing) -----------------------
  if ($Mode -eq "Disable") {
    if (-not $task) { $v.emitter_task_disabled = $true; $v.ok = $true; Emit "" }   # absent => already not emitting
    try { $null = & schtasks /end /tn ("\" + $taskName) 2>$null } catch { }
    $null = & schtasks /change /tn ("\" + $taskName) /disable 2>$null
    $xml = Read-TaskXml $taskName
    if ($xml -and ($xml -match "<Enabled>false</Enabled>")) { $v.emitter_task_disabled = $true; $v.ok = $true; Emit "" }
    Emit "disable_not_verified"
  }

  # ----------------------------------------- ENABLE (risk-increasing; contract-validated) --------------
  if ($Mode -eq "Enable") {
    if (-not $task) { Emit "no_emitter_task" }
    # Action contract: the standing emitter runs run_emitter.py 'run' against the account config - NEVER the
    # one-shot attest entrypoint and NEVER an arbitrary command. Any drift fails closed before enabling.
    $actionOk = $false
    foreach ($a in @($task.Actions)) {
      $arg = "$($a.Arguments)"
      if ($arg -match "run_emitter\.py" -and $arg -match "(^|\s)run(\s|$)" -and $arg -match "--config" `
          -and $arg -match [regex]::Escape($configLeaf) -and $arg -notmatch "run_oneshot_attest" -and $arg -notmatch "attest") { $actionOk = $true }
    }
    if (-not $actionOk) { Emit "emitter_task_action_untrusted" }
    # Principal contract: the task must run AS guvfx_u_<id> (SID match, locale-independent).
    $expectedSid = Resolve-SidValue $expectedUser
    $actualSid = Resolve-SidValue ("$($task.Principal.UserId)")
    if (-not $expectedSid -or -not $actualSid -or ($expectedSid -ne $actualSid)) { Emit "emitter_task_principal_drift" }

    $null = & schtasks /change /tn ("\" + $taskName) /enable 2>$null
    $xml = Read-TaskXml $taskName
    if ($xml -and ($xml -match "<Enabled>true</Enabled>")) { $v.emitter_task_enabled = $true; $v.ok = $true; Emit "" }
    Emit "enable_not_verified"
  }

  Emit "unknown_mode"
}
catch { $v.ok = $false; Emit "emitter_primitive_error" }
