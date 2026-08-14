<#
  STREAM 10E - W^X behavioural escape battery, TENANT-CONTEXT attempt runner (ADR-0043).

  Runs AS the hosted tenant (guvfx_u_<id>) INSIDE the tenant RDS/RemoteApp session on the DISPOSABLE certification
  host. For each escape case it ATTEMPTS the escape and records the tenant-observed result (launched / blocked /
  write_ok / write_denied / load_ok / load_denied). It is deliberately NON-authoritative: a non-admin tenant cannot
  read the AppLocker event log, so the AUTHORITATIVE per-case verdict is rendered by the admin-side
  Get-GuvfxCertEvidence.ps1, which correlates this JSON with the 8003/8004/8007 AppLocker events. The two together
  are the RULE-11 measurement path (tenant observation + admin event correlation + a positive control).

  SAFETY: runs ONLY on the disposable cert host, NEVER the Customer Zero production host (packet, 2026-08-14).
  It plants test artefacts ONLY under -WorkDir (a tenant-writable scratch) and the enumerated writable locations,
  and removes them on exit. It performs NO broker action and places NO order. The MQL5 #import native-exec case and
  the "MT5 works normally" positive control require a running MT5 with a demo login, which is a HUMAN (operator)
  step - this runner marks those cases operator_required and does not attempt a broker login.

  ASCII-ONLY (RULE 9); ParseFile()-validate before first execution. Emits one JSON object to -OutFile (and stdout).

  Usage (inside the tenant session):
    powershell -NoProfile -File Invoke-GuvfxEscapeBattery.ps1 -AccountId 90 -HostedUser guvfx_u_90 `
        -RuntimeRoot 'C:\GuvFX\accounts\90' -GoldenTerminal64 'C:\GuvFX\accounts\90\terminal\terminal64.exe' `
        -WorkDir 'C:\GuvFX\accounts\90\terminal\MQL5\Files\_escape' -OutFile 'C:\GuvFX\_cert\tenant_attempts.json'
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$HostedUser,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [Parameter(Mandatory=$true)][string]$GoldenTerminal64,
  [Parameter(Mandatory=$true)][string]$WorkDir,
  [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Stop"
$results = @()
$planted = @()

# Classify why a launch failed: AppLocker Enforce denies surface as Win32 1260 (ERROR_ACCESS_DISABLED_BY_POLICY)
# or a "blocked by group policy" message. Anything else is an ambiguous error, reported verbatim (NOT counted as a
# clean block - the admin-side verifier decides from the event log).
function Classify-LaunchError($err) {
  $m = "$($err.Exception.Message)"
  if ($m -match "1260" -or $m -match "blocked by group policy" -or $m -match "disabled by") { return "blocked_policy" }
  return "error_ambiguous:$m"
}

function New-Attempt([string]$case, [string]$action) {
  return [ordered]@{ case=$case; action=$action; tenant_result="not_attempted"; detail=""; artefact="" }
}

function Try-Launch([string]$case, [string]$action, [string]$path, [string[]]$argl = @()) {
  $a = New-Attempt $case $action
  $a.artefact = $path
  try {
    $p = if ($argl.Count) { Start-Process -FilePath $path -ArgumentList $argl -PassThru -WindowStyle Hidden }
         else { Start-Process -FilePath $path -PassThru -WindowStyle Hidden }
    Start-Sleep -Milliseconds 400
    if ($p -and -not $p.HasExited) { try { $p.Kill() } catch {} ; $a.tenant_result = "launched"; $a.detail = "process started (ESCAPE - expect admin 8004 block under Enforce)" }
    elseif ($p) { $a.tenant_result = "launched"; $a.detail = "process started then exited" }
    else { $a.tenant_result = "blocked"; $a.detail = "no process object" }
  } catch { $a.tenant_result = "blocked"; $a.detail = Classify-LaunchError $_ }
  return $a
}

try {
  if ($HostedUser -notmatch "^guvfx_u_[1-9][0-9]*$") { throw "not_a_hosted_identity" }
  if ($HostedUser -ne ("guvfx_u_" + $AccountId)) { throw "hosted_user_mismatch" }
  New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null

  # --- Case portable_copy_v5: copy the SIGNED terminal64 + a /portable config (AllowDllImport=1) elsewhere, launch it
  $copyExe = Join-Path $WorkDir "terminal64.exe"
  try {
    Copy-Item -LiteralPath $GoldenTerminal64 -Destination $copyExe -Force
    $planted += $copyExe
    $cfgDir = Join-Path $WorkDir "config"; New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
    "[Experts]`r`nAllowDllImport=1`r`n" | Out-File -FilePath (Join-Path $cfgDir "common.ini") -Encoding ASCII
    # launch with /portable so the copy uses the adjacent AllowDllImport=1 config (faithful V5); AppLocker denies the
    # copied EXE at launch regardless, which is the point of this case.
    $results += (Try-Launch "portable_copy_v5" "launch_copied_signed_terminal64" $copyExe @("/portable"))
  } catch { $a = New-Attempt "portable_copy_v5" "launch_copied_signed_terminal64"; $a.tenant_result = "blocked"; $a.detail = "copy_or_launch_failed:$($_.Exception.Message)"; $results += $a }

  # --- Case metaeditor: launch metaeditor64.exe (BinaryName pin must deny it even though MetaQuotes-signed)
  $me = Join-Path (Split-Path $GoldenTerminal64) "metaeditor64.exe"
  if (Test-Path -LiteralPath $me) { $results += (Try-Launch "metaeditor" "launch_metaeditor64" $me) }
  else { $a = New-Attempt "metaeditor" "launch_metaeditor64"; $a.tenant_result = "absent"; $a.detail = "metaeditor64.exe not present in golden (expected)"; $results += $a }

  # --- Case writable_exe: copy a benign OS EXE into a writable dir and launch it (renamed shell / arbitrary EXE)
  $plantExe = Join-Path $WorkDir "notepad_copy.exe"
  try { Copy-Item -LiteralPath (Join-Path $env:WINDIR "notepad.exe") -Destination $plantExe -Force; $planted += $plantExe; $results += (Try-Launch "writable_exe" "launch_planted_exe" $plantExe) }
  catch { $a = New-Attempt "writable_exe" "launch_planted_exe"; $a.tenant_result = "blocked"; $a.detail = "plant_failed:$($_.Exception.Message)"; $results += $a }

  # --- Case writable_script: drop scripts and try to run each via its host (all interpreters must be denied at Exe;
  #     the Script collection is deny-by-default for the tenant)
  foreach ($s in @(@{ext=".ps1";host="powershell.exe";body="'x' | Out-Null"},
                   @{ext=".vbs";host="wscript.exe";body="WScript.Quit 0"},
                   @{ext=".bat";host="cmd.exe";body="@exit /b 0"},
                   @{ext=".js";host="cscript.exe";body="WScript.Quit(0);"})) {
    $sp = Join-Path $WorkDir ("esc" + $s.ext)
    try {
      $s.body | Out-File -FilePath $sp -Encoding ASCII; $planted += $sp
      $args = if ($s.ext -eq ".ps1") { @("-NoProfile","-File",$sp) } else { @($sp) }
      $a = New-Attempt "writable_script" ("run_" + $s.ext.TrimStart('.'))
      $a.artefact = $sp
      try { $p = Start-Process -FilePath $s.host -ArgumentList $args -PassThru -WindowStyle Hidden; Start-Sleep -Milliseconds 300
            if ($p -and -not $p.HasExited) { try { $p.Kill() } catch {} } ; $a.tenant_result = "launched"; $a.detail = "$($s.host) started (ESCAPE - interpreter should be Exe-denied)" }
      catch { $a.tenant_result = "blocked"; $a.detail = Classify-LaunchError $_ }
      $results += $a
    } catch { $a = New-Attempt "writable_script" ("run_" + $s.ext.TrimStart('.')); $a.tenant_result = "blocked"; $a.detail = "plant_failed:$($_.Exception.Message)"; $results += $a }
  }

  # --- Case unsigned_dll_sideload: plant an unsigned DLL in a writable dir. The tenant cannot itself load it
  #     without an allowed loader; the AUTHORITATIVE proof is the admin-side Dll 8004 on any load attempt. Here we
  #     only record that the plant succeeded (write is allowed in the data dir - that is expected; EXECUTION is what
  #     must be denied). The COM-hijack case below is the load trigger.
  $udll = Join-Path $WorkDir "unsigned_evil.dll"
  try { [System.IO.File]::WriteAllBytes($udll, ([byte[]](0x4D,0x5A,0x90,0x00))); $planted += $udll
        $a = New-Attempt "unsigned_dll_sideload" "plant_unsigned_dll"; $a.artefact = $udll; $a.tenant_result = "planted"; $a.detail = "unsigned DLL written to a writable dir (write expected; load must be Dll-denied)"; $results += $a }
  catch { $a = New-Attempt "unsigned_dll_sideload" "plant_unsigned_dll"; $a.tenant_result = "error_ambiguous"; $a.detail = "$($_.Exception.Message)"; $results += $a }

  # --- Case signed_dll_comhijack_from_writable: copy a genuinely-signed OS DLL into a writable dir and register an
  #     HKCU COM InprocServer32 pointing at it. The per-tenant Dll W^X Deny (STREAM 10E) must deny the LOAD from the
  #     writable location. We register the hijack + trigger a tenant-context host to load it; the admin verifier
  #     confirms the Dll 8004 block. (HKCU write is expected to succeed - it is the LOAD that must be denied.)
  $signedDll = Join-Path $WorkDir "signed_proxy.dll"
  $a = New-Attempt "signed_dll_comhijack_from_writable" "com_hijack_load_signed_dll_from_writable"
  try {
    Copy-Item -LiteralPath (Join-Path $env:WINDIR "System32\scrobj.dll") -Destination $signedDll -Force; $planted += $signedDll
    $a.artefact = $signedDll
    $clsid = "{{{0:D8}-0000-0000-0000-00000000c0de}}" -f $AccountId
    $key = "HKCU:\Software\Classes\CLSID\$clsid\InprocServer32"
    $planted += "REGKEY::HKCU:\Software\Classes\CLSID\$clsid"   # register for cleanup BEFORE creating it (no orphan)
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name "(default)" -Value $signedDll -PropertyType String -Force | Out-Null
    # Force the InprocServer32 LoadLibrary via CoCreateInstance (deterministic, unlike a class-moniker display name):
    try { $null = [Activator]::CreateInstance([Type]::GetTypeFromCLSID([Guid]$clsid.Trim("{}"))); $a.tenant_result = "load_ok"; $a.detail = "signed DLL loaded from a writable location via COM (ESCAPE - expect Dll 8004 block)" }
    catch { $a.tenant_result = "load_denied"; $a.detail = "CoCreateInstance failed: $($_.Exception.Message)" }
  } catch { $a.tenant_result = "error_ambiguous"; $a.detail = "$($_.Exception.Message)" }
  $results += $a

  # --- Case common_ini_mutation: attempt to write AllowDllImport=1 into the REAL config\common.ini (NTFS Deny-write
  #     must refuse). This proves the AllowDllImport=0 ceiling is tenant-immutable.
  $commonIni = Join-Path $RuntimeRoot "terminal\config\common.ini"
  $a = New-Attempt "common_ini_mutation" "write_allowdllimport_1"
  $a.artefact = $commonIni
  try { Add-Content -LiteralPath $commonIni -Value "AllowDllImport=1" -ErrorAction Stop; $a.tenant_result = "write_ok"; $a.detail = "common.ini writable (ESCAPE - Deny-write ACE missing)" }
  catch { $a.tenant_result = "write_denied"; $a.detail = "NTFS denied: $($_.Exception.Message)" }
  $results += $a

  # --- Case import_native_exec: OPERATOR-REQUIRED (needs a running MT5 with a demo login). Not attempted here.
  $a = New-Attempt "import_native_exec" "mql5_import_kernel32_native_exec"
  $a.tenant_result = "operator_required"; $a.detail = "needs MT5 running with a demo login (human step); prove AllowDllImport=0 blocks #import"
  $results += $a

  # --- Case mt5_normal_positive_control: OPERATOR-REQUIRED positive control (MT5 must work normally for the tenant)
  $a = New-Attempt "mt5_normal_positive_control" "launch_terminal64_and_use_charts"
  $a.tenant_result = "operator_required"; $a.detail = "operator launches the RX terminal64 via RemoteApp + demo login; MT5 must work normally (RULE-11 positive control)"
  $results += $a
}
finally {
  foreach ($p in $planted) {
    try { if ($p -like "REGKEY::*") { Remove-Item -Path ($p -replace "^REGKEY::","") -Recurse -Force -ErrorAction SilentlyContinue }
          elseif (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue } } catch {}
  }
  try { if (Test-Path -LiteralPath $WorkDir) { Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue } } catch {}
}

$out = [ordered]@{ account_id=$AccountId; hosted_user=$HostedUser; runtime_root=$RuntimeRoot;
                   note="tenant-observed attempts; AUTHORITATIVE verdict = admin-side Get-GuvfxCertEvidence.ps1 (event correlation)";
                   attempts=$results }
New-Item -ItemType Directory -Path (Split-Path $OutFile) -Force | Out-Null
($out | ConvertTo-Json -Depth 6) | Out-File -FilePath $OutFile -Encoding ASCII
$out | ConvertTo-Json -Depth 6 -Compress
