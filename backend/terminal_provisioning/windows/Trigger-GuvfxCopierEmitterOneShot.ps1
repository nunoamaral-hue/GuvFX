<#
  ADR-0104 - trigger THIS account's INDEPENDENT one-shot emitter identity-attestation ONCE and return its
  sanitised verdict. Repository artifact; DARK (nothing installs/enables/runs this until a separately-governed,
  Sponsor-gated step does). ASCII-only (RULE 9).

  Runs as the LocalSystem hosted-executor daemon. It performs NO attestation itself and reads NO secret; it
  ORCHESTRATES only: prove the tenant identity, prove the pre-installed one-shot task exists, is DISABLED, and
  its action points ONLY at the approved non-interactive attest entrypoint (no drift), delete any stale result,
  ENABLE the task for EXACTLY ONE invocation, trigger it (it runs AS guvfx_u_<id>: reads the HOST-LOCAL ingest
  secret, builds+self-verifies the attestation, and POSTs it ONCE to the backend verifier), wait bounded, then
  ALWAYS re-disable the task in a finally (fail-closed: if the re-disable cannot be verified, the whole op is
  reported NOT ok). It emits ONE compact SANITISED JSON line (ok + http_status/verified/reason) - never the
  attestation envelope, signature, broker login or the ingest secret, none of which it ever sees.

  Security boundary (architecture.md Windows-primitive layer): every identity/path/task name is DERIVED here from
  the server-supplied -AccountId; nothing (command/script/username/path/task/url/secret) is accepted from the
  caller. Customer Zero (account 1) is refused, and the emitter bootstrap is defence-in-depth PINNED to the sole
  account it exists for (46). It can reach NO order, NO follower account, and NO execution/replication/ingestion
  path - it only enables, triggers and re-disables one fixed attestation task. Fail-closed everywhere.

  Usage (invoked by the primitive runner with a fixed argv):
    powershell -NoProfile -File Trigger-GuvfxCopierEmitterOneShot.ps1 -Username guvfx_u_46 -RuntimeRoot 'C:\GuvFX\accounts\46' -TerminalRoot 'C:\GuvFX\accounts\46\terminal' -AccountId 46
#>
param(
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [Parameter(Mandatory=$true)][string]$TerminalRoot,
  [Parameter(Mandatory=$true)][int]$AccountId,
  # Bounded wait for the task's fresh result file. The attest builds locally and POSTs once (short), so a healthy
  # run completes well under this; kept below the client read-timeout so the caller reads a real fail-closed
  # result rather than a transport abort, and far below the daemon's primitive timeout.
  [int]$TimeoutSeconds = 90
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
# The emitter one-shot attestation bootstrap exists ONLY for this account. Customer Zero is refused separately.
$EMITTER_ONESHOT_ACCOUNT = 46

# The single compact-JSON verdict. Every UNPROVEN fact stays false; the ingest secret / envelope NEVER appear.
$v = [ordered]@{
  ok=$false; account_id=$AccountId; mode="oneshot-attest";
  task_was_disabled=$false; attest_triggered=$false; result_observed=$false; task_redisabled=$false;
  http_status=$null; verified=$false; reason=""
}
function Emit([string]$why) { if ($why) { $v.reason = $why }; ($v | ConvertTo-Json -Compress -Depth 5); if (-not $v.ok) { exit 1 } else { exit 0 } }

try {
  # 1-5. Server-derived identity contract. Refuse anything that is not the exact derivation from AccountId, refuse
  #      Customer Zero, and (defence in depth) re-pin the sole emitter account. These run BEFORE any enable, so a
  #      refusal leaves the task exactly as it was (disabled); Emit exits with nothing to clean up.
  if ($AccountId -le 0 -or $AccountId -eq 1) { Emit "reserved_or_invalid_account" }
  if ($AccountId -ne $EMITTER_ONESHOT_ACCOUNT) { Emit "account_not_emitter_pinned" }
  $expectedUser = "guvfx_u_$AccountId"
  if ($Username -ne $expectedUser) { Emit "username_mismatch" }
  $expectedRuntime = Join-Path $ACCOUNTS_BASE "$AccountId"
  $expectedTerminalRoot = Join-Path $expectedRuntime "terminal"
  if ([System.IO.Path]::GetFullPath($RuntimeRoot) -ne [System.IO.Path]::GetFullPath($expectedRuntime)) { Emit "runtime_mismatch" }
  if ([System.IO.Path]::GetFullPath($TerminalRoot) -ne [System.IO.Path]::GetFullPath($expectedTerminalRoot)) { Emit "terminal_root_mismatch" }

  # 6. Prove the hosted user exists.
  if (-not (Get-LocalUser -Name $expectedUser -ErrorAction SilentlyContinue)) { Emit "no_user" }

  $taskName = "GuvFX_CopierEmitterOneShot_$AccountId"
  $resultPath = Join-Path (Join-Path $expectedRuntime "_oneshot") "attest_result.json"

  # 7. Prove the pre-installed one-shot task exists and is CURRENTLY DISABLED (locale-independent read-back, RULE
  #    11 - never parse a localised status string). The one-shot must always START from disabled; an already-
  #    enabled task is an integrity anomaly, so fail closed WITHOUT enabling/triggering anything.
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if (-not $task) { Emit "no_oneshot_task" }
  $currentXml = ""
  try { $currentXml = (& schtasks /query /tn "\$taskName" /xml ONE 2>$null) -join "`n" } catch { $currentXml = "" }
  if ($currentXml -notmatch "<Enabled>false</Enabled>") { Emit "oneshot_task_not_disabled" }
  $v.task_was_disabled = $true

  # 8. Verify the task action points ONLY at the approved non-interactive attest entrypoint and the ATTEST
  #    verifier route - never a drifted command, and never the event-ingestion path. It must reference the fixed
  #    entrypoint 'run_oneshot_attest.py', write to the exact result file we poll, carry the 'attest-verify'
  #    route, and NEVER carry 'event-ingest'. Any drift fails closed before anything is enabled.
  $actionsOk = $false
  $resultLeaf = [System.IO.Path]::GetFileName($resultPath)
  foreach ($a in @($task.Actions)) {
    $arg = "$($a.Arguments)"
    if ($arg -match "run_oneshot_attest\.py" -and $arg -match [regex]::Escape($resultLeaf) `
        -and $arg -match "attest-verify" -and $arg -notmatch "event-ingest") { $actionsOk = $true }
  }
  if (-not $actionsOk) { Emit "oneshot_task_action_untrusted" }

  # 9. Stale-protect: delete any prior result BEFORE enabling/triggering, so we can only ever return a FRESH one.
  if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue }

  # 10-13. Enable (one invocation), trigger, bounded-wait, read - inside a try whose finally ALWAYS re-disables.
  #        No `exit` is used in this region: Emit() exits, and exit would SKIP the finally, so we only set verdict
  #        fields here and Emit ONCE after the finally has re-disabled the task (fail-closed re-disable).
  $enabled = $false
  try {
    $null = & schtasks /change /tn "\$taskName" /enable 2>$null
    if ($LASTEXITCODE -ne 0) { $v.reason = "enable_failed" }
    else {
      $enabled = $true
      $triggerAt = Get-Date
      $null = & schtasks /run /tn "\$taskName" 2>$null
      if ($LASTEXITCODE -ne 0) { $v.reason = "trigger_failed" }
      else {
        $v.attest_triggered = $true
        $deadline = (Get-Date).AddSeconds([Math]::Max(5, $TimeoutSeconds))
        $found = $false
        while ((Get-Date) -lt $deadline) {
          if (Test-Path -LiteralPath $resultPath) {
            try { $w = (Get-Item -LiteralPath $resultPath).LastWriteTime } catch { $w = $null }
            if ($w -and $w -ge $triggerAt) { $found = $true; break }
          }
          Start-Sleep -Milliseconds 500
        }
        if (-not $found) { $v.reason = "attest_timeout" }
        else {
          $v.result_observed = $true
          $raw = $null
          try { $raw = Get-Content -LiteralPath $resultPath -Raw -ErrorAction Stop } catch { $raw = $null }
          $obj = $null
          if ($raw) { try { $obj = $raw | ConvertFrom-Json -ErrorAction Stop } catch { $obj = $null } }
          if ($null -eq $obj) { $v.reason = "result_malformed" }
          elseif ([int]$obj.account_id -ne $AccountId) { $v.reason = "result_account_mismatch" }
          else {
            # Carry ONLY the sanitised fields (never an envelope/secret, even if a future result added one).
            if ($obj.http_status -is [int]) { $v.http_status = [int]$obj.http_status }
            $v.verified = [bool]$obj.verified
            $rr = "$($obj.reason)"
            if ($rr) { $v.reason = $rr }
            # ok iff the task POSTed and the backend verifier accepted it. A SECRET_UNAVAILABLE / 503 / any other
            # outcome stays ok:false (fail closed on an unknown or unverified outcome).
            $v.ok = ([bool]$obj.ok -and [bool]$obj.verified)
          }
        }
      }
    }
  }
  finally {
    if ($enabled) {
      # ALWAYS re-disable the one-shot task - even on timeout/error. A running instance is not killed (it finishes
      # its single attest+POST and writes the result); disabling only forbids any further start. Then verify the
      # disable locale-independently (RULE 11). If it cannot be verified, the op is NOT ok (never leave enabled).
      try { $null = & schtasks /change /tn "\$taskName" /disable 2>$null } catch { }
      $verifyXml = ""
      try { $verifyXml = (& schtasks /query /tn "\$taskName" /xml ONE 2>$null) -join "`n" } catch { $verifyXml = "" }
      if ($verifyXml -match "<Enabled>false</Enabled>") { $v.task_redisabled = $true }
      else { $v.task_redisabled = $false; $v.ok = $false; $v.reason = "redisable_failed" }
    }
  }
  Emit ""
}
catch { $v.ok = $false; Emit "oneshot_primitive_error" }
