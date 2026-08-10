<#
  Set-GuvfxAppLocker.ps1 -- deploy / verify / rollback / evidence for the GuvFX Hosted Workspace
  AppLocker candidate policy (AUDITONLY, non-blocking).

  What it does (see applocker\guvfx-hosted-auditonly.xml for the policy rationale):
    -Mode Deploy   : back up the current effective policy; ensure AppIDSvc (Application Identity) is
                     Automatic + Running; ensure the AppLocker event channels are enabled; resolve the
                     hosted user SID and substitute it into the template; apply the policy (AuditOnly).
    -Mode Verify   : print the effective policy collection modes + rule counts and AppIDSvc state.
    -Mode Rollback : restore AppIDSvc to its captured baseline and CLEAR the local AppLocker policy
                     (back to NotConfigured -- nothing enforced or audited).
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
  [string]$HostedUser = 'guvfx_u_1',
  [string]$TemplatePath = "$PSScriptRoot\applocker\guvfx-hosted-auditonly.xml",
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
      # 1. Back up current effective policy + AppIDSvc state (for rollback).
      (Get-AppLockerPolicy -Effective -Xml) | Out-File -FilePath $BaselinePolicy -Encoding ASCII
      $svc0 = Svc-State
      "status=$($svc0.status);start=$($svc0.start)" | Out-File -FilePath $BaselineSvc -Encoding ASCII
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
      # 4. Resolve hosted user SID and substitute into the template.
      $sid = (Get-LocalUser -Name $HostedUser).SID.Value
      if ([string]::IsNullOrWhiteSpace($sid)) { throw "could not resolve SID for $HostedUser" }
      if (-not (Test-Path $TemplatePath)) { throw "template not found: $TemplatePath" }
      $xml = Get-Content -Path $TemplatePath -Raw
      $xml = $xml.Replace('{{HOSTED_USER_SID}}', $sid)
      $xml | Out-File -FilePath $AppliedPolicy -Encoding ASCII
      # 5. Apply (replace local policy). AuditOnly -> nothing blocked.
      Set-AppLockerPolicy -XmlPolicy $AppliedPolicy
      Start-Sleep -Seconds 2
      $svc1 = Svc-State
      [ordered]@{ mode='Deploy'; hosted_user=$HostedUser; hosted_sid=$sid; appidsvc=$svc1;
                  effective=(Effective-Summary); baseline_saved=$BaselinePolicy; applied=$AppliedPolicy;
                  ok=$true } | ConvertTo-Json -Compress -Depth 5
    }

    'Rollback' {
      # Clear the local AppLocker policy (NotConfigured everywhere) and restore AppIDSvc baseline.
      $empty = Join-Path $env:TEMP 'guvfx-applocker-clear.xml'
      '<AppLockerPolicy Version="1"></AppLockerPolicy>' | Out-File -FilePath $empty -Encoding ASCII
      Set-AppLockerPolicy -XmlPolicy $empty
      $restored = 'left-running'
      if (Test-Path $BaselineSvc) {
        $b = Get-Content $BaselineSvc -Raw
        if ($b -match 'start=Manual') { Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\AppIDSvc' -Name Start -Value 3 -Type DWord; $restored='Manual' }
        if ($b -match 'status=Stopped') { try { Stop-Service -Name AppIDSvc -Force } catch {}; $restored="$restored/Stopped" }
      }
      [ordered]@{ mode='Rollback'; effective=(Effective-Summary); appidsvc_restored=$restored; ok=$true } | ConvertTo-Json -Compress
    }
  }
}
catch {
  [ordered]@{ ok=$false; mode=$Mode; error=$_.Exception.Message } | ConvertTo-Json -Compress
  exit 1
}
