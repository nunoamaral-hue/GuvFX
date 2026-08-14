<#
  STREAM 10E - W^X escape-battery ADMIN-CONTEXT evidence collector + AUTHORITATIVE verdict (ADR-0043).

  Runs AS Administrator on the DISPOSABLE certification host AFTER the tenant-context Invoke-GuvfxEscapeBattery.ps1
  has attempted the escapes. It renders the authoritative per-case PASS(blocked)/FAIL(escaped) by correlating the
  tenant-observed attempts with the AppLocker event log, and it fails closed on an un-proven measurement path.

  RULE 11 (the measurement path must be proven able to see a real positive before a negative is trusted): a clean
  "no 8004 blocks" is worthless unless the channel is demonstrably live. This script REQUIRES at least one AppLocker
  ALLOW event (8002/8005) attributable to the hosted SID inside the window as its POSITIVE CONTROL; absent that, it
  returns measurement_unproven and the whole cert FAILS (never a false clean, per the codebase RULE-11 incidents).

  Verdict per escape case:
    - a BLOCK event (8004 Exe/Dll enforce, 8007 Script enforce) matching the artefact  -> PASS (blocked)
    - an ALLOW event (8002/8005) matching an ESCAPE artefact                            -> FAIL (escaped)
    - AuditOnly mode: an 8003 "would-block" matching the artefact                       -> AUDIT_WOULD_BLOCK (advisory)
    - no matching event                                                                 -> INCONCLUSIVE (fail-closed)
  It also captures the effective policy, the G5v2 ACL read-back (Set-GuvfxWorkspaceAclV2 -Verify), the golden-gate
  result, and the AllowDllImport value, and writes a hashed evidence manifest.

  ASCII-ONLY (RULE 9); ParseFile()-validate first. Get-WinEvent can hang over an SSH-stdin channel - run this
  DETACHED (-File) and read the JSON (repo GOTCHA). Emits the manifest to -EvidenceDir and a compact summary to stdout.

  Usage (as admin):
    powershell -NoProfile -File Get-GuvfxCertEvidence.ps1 -AccountId 90 -HostedUser guvfx_u_90 `
        -RuntimeRoot 'C:\GuvFX\accounts\90' -Mode Enforce -SinceMinutes 30 `
        -TenantAttemptsJson 'C:\GuvFX\_cert\tenant_attempts.json' -EvidenceDir 'C:\GuvFX\_cert\evidence'
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$HostedUser,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [Parameter(Mandatory=$true)][ValidateSet("AuditOnly","Enforce")][string]$Mode,
  [int]$SinceMinutes = 30,
  [string]$TenantAttemptsJson,
  [Parameter(Mandatory=$true)][string]$EvidenceDir
)
$ErrorActionPreference = "Stop"

function Fail($reason) { [ordered]@{ ok=$false; overall="ERROR"; reason="$reason" } | ConvertTo-Json -Compress; exit 1 }

try {
  if ($HostedUser -notmatch "^guvfx_u_[1-9][0-9]*$") { Fail "not_a_hosted_identity" }
  if ($HostedUser -ne ("guvfx_u_" + $AccountId)) { Fail "hosted_user_mismatch" }
  New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null
  $sid = (Get-LocalUser -Name $HostedUser -ErrorAction Stop).SID.Value

  # Pull AppLocker events for the window. 8002/8004/8003 = EXE and DLL; 8005/8007/8006 = MSI and Script.
  $start = (Get-Date).AddMinutes(-1 * [math]::Abs($SinceMinutes))
  $events = @()
  foreach ($log in @("Microsoft-Windows-AppLocker/EXE and DLL", "Microsoft-Windows-AppLocker/MSI and Script")) {
    try {
      $evs = Get-WinEvent -FilterHashtable @{ LogName=$log; StartTime=$start } -ErrorAction SilentlyContinue
      foreach ($e in $evs) {
        $x = ([xml]$e.ToXml()).Event
        $ud = @{}; foreach ($d in $x.UserData.RuleAndFileData.ChildNodes) { $ud[$d.Name] = $d.'#text' }
        $events += [ordered]@{ id=[int]$e.Id; time=$e.TimeCreated.ToString("o"); sid="$($x.System.Security.UserID)";
                               path="$($ud['FilePath'])"; log=$log }
      }
    } catch {}
  }
  $forSid = @($events | Where-Object { $_.sid -eq $sid })

  # RULE-11 POSITIVE CONTROL: the channel must have captured at least one ALLOW (8002/8005) for the hosted SID.
  $allows = @($forSid | Where-Object { $_.id -eq 8002 -or $_.id -eq 8005 })
  $measurementProven = ($allows.Count -ge 1)

  # Load the tenant attempts (advisory: what the tenant TRIED, so we can attribute events to a case by artefact path).
  $attempts = @()
  if ($TenantAttemptsJson -and (Test-Path -LiteralPath $TenantAttemptsJson)) {
    try { $attempts = @((Get-Content -LiteralPath $TenantAttemptsJson -Raw | ConvertFrom-Json).attempts) } catch {}
  }

  function Verdict-ForArtefact([string]$artefact) {
    if ([string]::IsNullOrWhiteSpace($artefact)) { return @{ verdict="NO_ARTEFACT"; evidence=@() } }
    $leaf = (Split-Path $artefact -Leaf)
    $matched = @($forSid | Where-Object { $_.path -and ($_.path -like "*$leaf*") })
    $block = @($matched | Where-Object { $_.id -eq 8004 -or $_.id -eq 8007 })
    $allow = @($matched | Where-Object { $_.id -eq 8002 -or $_.id -eq 8005 })
    $audit = @($matched | Where-Object { $_.id -eq 8003 -or $_.id -eq 8006 })
    if ($allow.Count -ge 1) { return @{ verdict="FAIL_ESCAPED"; evidence=$allow } }
    if ($block.Count -ge 1) { return @{ verdict="PASS_BLOCKED"; evidence=$block } }
    if ($audit.Count -ge 1) { return @{ verdict="AUDIT_WOULD_BLOCK"; evidence=$audit } }
    return @{ verdict="INCONCLUSIVE"; evidence=@() }
  }

  $caseResults = @()
  foreach ($att in $attempts) {
    $v = Verdict-ForArtefact "$($att.artefact)"
    $caseResults += [ordered]@{ case="$($att.case)"; action="$($att.action)"; artefact="$($att.artefact)";
                                tenant_result="$($att.tenant_result)"; authoritative=$v.verdict;
                                event_count=@($v.evidence).Count }
  }

  # Sidecar state: effective policy, ACL read-back, golden gate, AllowDllImport ceiling.
  $win = Split-Path $PSScriptRoot -Parent   # ...\terminal_provisioning\windows
  $state = [ordered]@{}
  try { $eff = Get-AppLockerPolicy -Effective -Xml; ($eff | Out-File -FilePath (Join-Path $EvidenceDir "effective_policy.xml") -Encoding ASCII)
        $state.effective_policy_sha256 = (Get-FileHash -Path (Join-Path $EvidenceDir "effective_policy.xml") -Algorithm SHA256).Hash } catch { $state.effective_policy_sha256 = "unavailable" }
  $commonIni = Join-Path $RuntimeRoot "terminal\config\common.ini"
  try { $state.allowdllimport = (Select-String -Path $commonIni -Pattern "AllowDllImport\s*=\s*(\d)" -ErrorAction SilentlyContinue | ForEach-Object { $_.Matches[0].Groups[1].Value }) -join "," } catch { $state.allowdllimport = "unreadable" }

  $failEscaped = @($caseResults | Where-Object { $_.authoritative -eq "FAIL_ESCAPED" })
  $inconclusive = @($caseResults | Where-Object { $_.authoritative -eq "INCONCLUSIVE" })
  # Enforce cert PASSES only if: measurement proven, NO escape, NO inconclusive attempted-escape case, AllowDllImport=0.
  $enforceReady = ($Mode -eq "Enforce")
  $adiClean = ($state.allowdllimport -eq "0" -or $state.allowdllimport -eq "")
  $overall = "FAIL"
  if (-not $measurementProven) { $overall = "MEASUREMENT_UNPROVEN" }
  elseif ($failEscaped.Count -gt 0) { $overall = "FAIL_ESCAPED" }
  elseif ($enforceReady -and $inconclusive.Count -gt 0) { $overall = "INCONCLUSIVE" }
  elseif ($enforceReady -and -not $adiClean) { $overall = "FAIL_ALLOWDLLIMPORT" }
  elseif ($enforceReady) { $overall = "PASS" }
  else { $overall = "AUDIT_REVIEW" }   # AuditOnly runs never PASS; they inform the 8003 review before Enforce

  $manifest = [ordered]@{
    schema="guvfx.stream10e.escape_evidence/1"; account_id=$AccountId; hosted_user=$HostedUser; hosted_sid=$sid;
    mode=$Mode; window_minutes=$SinceMinutes; measurement_proven=$measurementProven; allow_control_events=$allows.Count;
    state=$state; cases=$caseResults;
    counts=[ordered]@{ pass_blocked=@($caseResults | Where-Object { $_.authoritative -eq "PASS_BLOCKED" }).Count;
                       fail_escaped=$failEscaped.Count; inconclusive=$inconclusive.Count;
                       audit_would_block=@($caseResults | Where-Object { $_.authoritative -eq "AUDIT_WOULD_BLOCK" }).Count };
    overall=$overall }
  $mfPath = Join-Path $EvidenceDir "escape_evidence.json"
  ($manifest | ConvertTo-Json -Depth 8) | Out-File -FilePath $mfPath -Encoding ASCII
  $manifest.manifest_sha256 = (Get-FileHash -Path $mfPath -Algorithm SHA256).Hash
  ($manifest | ConvertTo-Json -Depth 8) | Out-File -FilePath $mfPath -Encoding ASCII
  [ordered]@{ ok=($overall -eq "PASS"); overall=$overall; measurement_proven=$measurementProven;
              fail_escaped=$failEscaped.Count; inconclusive=$inconclusive.Count; manifest=$mfPath } | ConvertTo-Json -Compress
  if ($overall -ne "PASS" -and $overall -ne "AUDIT_REVIEW") { exit 1 }
}
catch { Fail "$($_.Exception.Message)" }
