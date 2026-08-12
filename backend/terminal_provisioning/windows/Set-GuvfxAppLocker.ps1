<#
  Set-GuvfxAppLocker.ps1 -- deploy / verify / rollback / evidence for the GuvFX Hosted Workspace
  AppLocker candidate policy (AUDITONLY, non-blocking).

  What it does (see applocker\guvfx-hosted-auditonly.xml for the policy rationale):
    -Mode Deploy   : back up the current effective policy; ensure AppIDSvc (Application Identity) is
                     Automatic + Running; ensure the AppLocker event channels are enabled; resolve the
                     hosted user SID and substitute it into the template; apply the policy (AuditOnly).
    -Mode Verify   : print the effective policy collection modes + rule counts and AppIDSvc state.
    -Mode Rollback : restore AppIDSvc to its captured baseline start type AND restore the ORIGINAL AppLocker
                     policy captured at first Deploy (Set-AppLockerPolicy REPLACES, no -Merge -- so this returns
                     the host to its EXACT pre-Deploy state; if no baseline exists, clears to NotConfigured).
    -Mode Evidence : dump the effective policy XML + AppLocker event-channel record counts (the
                     measurement path). AuditOnly logs event 8003 ("would have been blocked").

  Safety: AuditOnly blocks NOTHING. Administrators keep Allow-* in every collection (recovery). The
  shell/escape denies are scoped to the hosted user SID only. Fully reversible via -Mode Rollback.

  ASCII-only (RULE 9). Parse-validate before first host execution:
    [System.Management.Automation.Language.Parser]::ParseFile('Set-GuvfxAppLocker.ps1',[ref]$null,[ref]$null)

  Usage:
    powershell -NoProfile -File Set-GuvfxAppLocker.ps1 -Mode Deploy   -HostedUser guvfx_u_1
    powershell -NoProfile -File Set-GuvfxAppLocker.ps1 -Mode Verify
    powershell -NoProfile -File Set-GuvfxAppLocker.ps1 -Mode Evidence
    powershell -NoProfile -File Set-GuvfxAppLocker.ps1 -Mode Rollback
#>
param(
  [ValidateSet('Deploy','Verify','Rollback','Evidence')]
  [string]$Mode = 'Verify',
  [switch]$Enforce,   # Deploy only: apply the SAME policy with EnforcementMode Enabled (Enforce) instead of AuditOnly.
  [string]$HostedUser = 'guvfx_u_1',
  [string]$TemplatePath = "$PSScriptRoot\applocker\guvfx-hosted-auditonly.xml",
  [string]$EnforceTemplatePath = "$PSScriptRoot\applocker\guvfx-hosted-enforce.xml",
  [string]$StateDir = 'C:\GuvFX\_applocker'
)
$ErrorActionPreference = 'Stop'
$AppLockerChannels = @('Microsoft-Windows-AppLocker/EXE and DLL','Microsoft-Windows-AppLocker/MSI and Script')
$BaselinePolicy = Join-Path $StateDir 'baseline-effective-policy.xml'
$BaselineSvc    = Join-Path $StateDir 'baseline-appidsvc.txt'
$AppliedPolicy  = Join-Path $StateDir 'applied-auditonly-policy.xml'

function Ensure-StateDir { if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null } }

function Effective-Summary {
  $p = Get-AppLockerPolicy -Effective
  $rows = @()
  foreach ($c in $p.RuleCollections) { $rows += ($c.RuleCollectionType + '=' + $c.Count + '(' + $c.EnforcementMode + ')') }
  if ($rows.Count -eq 0) { return '<none>' }
  return ($rows -join ', ')
}

function Svc-State {
  $s = Get-Service AppIDSvc
  $m = (Get-CimInstance Win32_Service -Filter "Name='AppIDSvc'").StartMode
  return @{ status = "$($s.Status)"; start = "$m" }
}

$applied = $false   # F3: set true once Set-AppLockerPolicy has run, so a later throw reports enforcement is LIVE.

try {
  switch ($Mode) {

    'Verify' {
      $svc = Svc-State
      [ordered]@{ mode='Verify'; appidsvc=$svc; effective=(Effective-Summary) } | ConvertTo-Json -Compress
    }

    'Evidence' {
      Ensure-StateDir
      (Get-AppLockerPolicy -Effective -Xml) | Out-File -FilePath (Join-Path $StateDir 'evidence-effective.xml') -Encoding ASCII
      $chan = @()
      foreach ($ch in $AppLockerChannels) {
        $l = Get-WinEvent -ListLog $ch -ErrorAction SilentlyContinue
        if ($l) { $chan += @{ channel=$ch; enabled=[bool]$l.IsEnabled; records=$l.RecordCount } }
        else    { $chan += @{ channel=$ch; enabled=$false; records=-1 } }
      }
      [ordered]@{ mode='Evidence'; effective=(Effective-Summary); channels=$chan } | ConvertTo-Json -Compress -Depth 5
    }

    'Deploy' {
      Ensure-StateDir
      # 1. Back up current effective policy + AppIDSvc state (for rollback). Capture ONCE (guarded) so an
      # Enforce redeploy preserves the ORIGINAL (pre-AppLocker, empty) rollback anchor rather than saving the
      # AuditOnly state over it.
      if (-not (Test-Path $BaselinePolicy)) { (Get-AppLockerPolicy -Effective -Xml) | Out-File -FilePath $BaselinePolicy -Encoding ASCII }
      if (-not (Test-Path $BaselineSvc)) { $svc0 = Svc-State; "status=$($svc0.status);start=$($svc0.start)" | Out-File -FilePath $BaselineSvc -Encoding ASCII }
      # 2. Ensure AppIDSvc Automatic + Running. AppIDSvc (Application Identity) is a PROTECTED service:
      # Set-Service -StartupType is denied even to Administrators. Set the start type via the registry
      # (admins may write Services\*) and start it, falling back to sc.exe if the SCM start path is guarded.
      Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\AppIDSvc' -Name Start -Value 2 -Type DWord
      try { Start-Service -Name AppIDSvc } catch { & sc.exe start AppIDSvc | Out-Null; Start-Sleep -Seconds 3 }
      # 3. Ensure AppLocker event channels enabled (audit collection).
      foreach ($ch in $AppLockerChannels) {
        $l = Get-WinEvent -ListLog $ch -ErrorAction SilentlyContinue
        if ($l -and -not $l.IsEnabled) { & wevtutil.exe sl "$ch" /e:true | Out-Null }
      }
      # 4. Load the template. For -Enforce, prefer the committed drift-checked Enforce artifact so WHAT IS TESTED
      # IS WHAT DEPLOYS (F5); fall back to the AuditOnly template + mode-swap if the Enforce artifact is absent.
      # The canonical STREAM 10B allow model is machine-wide (deny-by-default; NO per-user token) so no SID
      # substitution is needed; the legacy {{HOSTED_USER_SID}} token, if present, is still resolved for back-compat.
      $srcTemplate = $TemplatePath
      if ($Enforce -and (Test-Path $EnforceTemplatePath)) { $srcTemplate = $EnforceTemplatePath }
      if (-not (Test-Path $srcTemplate)) { throw "template not found: $srcTemplate" }
      $xml = Get-Content -Path $srcTemplate -Raw
      $sid = ''
      if ($xml.Contains('{{HOSTED_USER_SID}}')) {
        $sid = (Get-LocalUser -Name $HostedUser).SID.Value
        if ([string]::IsNullOrWhiteSpace($sid)) { throw "could not resolve SID for $HostedUser" }
        $xml = $xml.Replace('{{HOSTED_USER_SID}}', $sid)
      }
      $enforcing = $false
      if ($Enforce) {
        # If we loaded the committed Enforce artifact it is already Enabled (this replace is a harmless no-op); if
        # we fell back to the AuditOnly template, this swaps every collection's mode to Enabled.
        $xml = $xml.Replace('EnforcementMode="AuditOnly"', 'EnforcementMode="Enabled"')
        $enforcing = $true
      }
      $xml | Out-File -FilePath $AppliedPolicy -Encoding ASCII
      # 5. Apply (replace local policy). AuditOnly -> nothing blocked; Enabled -> Enforce.
      Set-AppLockerPolicy -XmlPolicy $AppliedPolicy
      $applied = $true
      Start-Sleep -Seconds 2
      $svc1 = Svc-State
      [ordered]@{ mode='Deploy'; enforce=$enforcing; hosted_user=$HostedUser; hosted_sid=$sid; appidsvc=$svc1;
                  effective=(Effective-Summary); baseline_saved=$BaselinePolicy; applied=$AppliedPolicy;
                  ok=$true } | ConvertTo-Json -Compress -Depth 5
    }

    'Rollback' {
      # F1: restore the ORIGINAL policy captured at first Deploy (Set-AppLockerPolicy REPLACES, no -Merge -> the
      # effective policy returns to EXACTLY its pre-Deploy state, which may be non-empty on a host that already
      # carried an AppLocker policy). Only if no baseline was ever captured do we clear to empty (NotConfigured).
      if (Test-Path $BaselinePolicy) {
        Set-AppLockerPolicy -XmlPolicy $BaselinePolicy
        $applied = $true
        $restoredPolicy = 'restored-baseline'
      } else {
        $empty = Join-Path $env:TEMP 'guvfx-applocker-clear.xml'
        '<AppLockerPolicy Version="1"></AppLockerPolicy>' | Out-File -FilePath $empty -Encoding ASCII
        Set-AppLockerPolicy -XmlPolicy $empty
        $applied = $true
        $restoredPolicy = 'cleared-empty-no-baseline'
      }
      # F2: restore AppIDSvc to its EXACT captured start type (Auto=2 / Manual=3 / Disabled=4), not only Manual.
      $restored = 'left-running'
      if (Test-Path $BaselineSvc) {
        $b = Get-Content $BaselineSvc -Raw
        $startVal = 2
        if ($b -match 'start=Manual')   { $startVal = 3 }
        if ($b -match 'start=Disabled') { $startVal = 4 }
        Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\AppIDSvc' -Name Start -Value $startVal -Type DWord
        $restored = "start=$startVal"
        if ($b -match 'status=Stopped') { try { Stop-Service -Name AppIDSvc -Force } catch {}; $restored = "$restored/Stopped" }
      }
      [ordered]@{ mode='Rollback'; policy=$restoredPolicy; effective=(Effective-Summary); appidsvc_restored=$restored; ok=$true } | ConvertTo-Json -Compress
    }
  }
}
catch {
  # F3: if the policy was already applied before the failure (e.g. an -Enforce deploy that threw AFTER
  # Set-AppLockerPolicy), enforcement is LIVE despite ok=false -- the operator must run -Mode Rollback.
  $note = 'policy not applied'
  if ($applied) { $note = 'policy WAS applied before failure - run -Mode Rollback' }
  [ordered]@{ ok=$false; mode=$Mode; applied=$applied; error=$_.Exception.Message; note=$note } | ConvertTo-Json -Compress
  exit 1
}
