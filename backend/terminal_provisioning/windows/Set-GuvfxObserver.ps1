<#
  Stream 5 - prepare the read-only, session-bound hosted observer for one identity (idempotent).

  Reuses the already-certified Hosted Observation architecture (attach -> read -> exit); it does NOT redesign it
  and creates NO permanent execution bridge. For guvfx_u_<id> it ensures:
    * the reviewed observer tooling exists in the AppLocker-allowed location (default C:\GuvFX\observer, admin-owned),
    * guvfx_u_<id> holds ReadAndExecute on that tooling (never write),
    * an ON-DEMAND scheduled task 'GuvFX_HostedObserver_<id>' registered to run AS guvfx_u_<id> (InteractiveToken,
      LeastPrivilege), created only if absent (never duplicated).

  The observer itself remains attach/read/exit and is launched on demand; this primitive only prepares it.
  ASCII-only (RULE 9). Emits a single compact JSON object.

  Usage:
    powershell -NoProfile -File Set-GuvfxObserver.ps1 -Mode Ensure -Username guvfx_u_14 -RuntimeRoot 'C:\GuvFX\accounts\14'
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Ensure","Verify","Remove")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$ObserverDir = "C:\GuvFX\observer"
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$result = [ordered]@{ username=$Username; task=""; observer_dir=$ObserverDir; tooling_present=$false; result_dir_ok=$false; acl_ok=$false; task_present=$false; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }

try {
  if ($Username -notmatch "^guvfx_u_[1-9][0-9]*$") { Fail "refusing: not a hosted identity" }
  $full = [System.IO.Path]::GetFullPath($RuntimeRoot)
  if ($full -like "*..*") { Fail "refusing: path traversal" }
  if (-not ($full.ToLower().StartsWith(($ACCOUNTS_BASE.ToLower() + "\")))) { Fail "refusing: outside accounts base" }
  $taskName = "GuvFX_HostedObserver_" + ($Username -replace '^guvfx_u_','')
  $result.task = $taskName

  if ($Mode -eq "Remove") {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    $result.task_present = [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    $result.ok = (-not $result.task_present); if (-not $result.ok) { Fail "remove did not clear the task" }
    $result | ConvertTo-Json -Compress; return
  }

  if (-not (Test-Path -LiteralPath $ObserverDir)) { Fail "observer tooling dir absent - stage the reviewed observer first" }
  # 9E LOAD-BEARING GATE: the reviewed observer harness itself MUST be present. This is the exact defect the
  # first beta run hit - the task was registered pointing at a run_observer.py that was never staged, so
  # PREPARE_OBSERVER must NEVER report success (or register a task) when the tooling is missing.
  # Both the harness AND its self-contained guarded-attach sibling must be present - run_observer.py imports
  # observer_attach (never the legacy bridge), so a missing attach module is the same dead-on-arrival staging
  # gap the gate exists to catch. Report success (and register a task) ONLY when the full tooling is staged.
  $obsPy = Join-Path $ObserverDir "run_observer.py"
  $attachPy = Join-Path $ObserverDir "observer_attach.py"
  $result.tooling_present = [bool]((Test-Path -LiteralPath $obsPy -PathType Leaf) -and (Test-Path -LiteralPath $attachPy -PathType Leaf))
  if (-not $result.tooling_present) { Fail "observer tooling (run_observer.py + observer_attach.py) absent - staging incomplete" }
  $acctId = ($Username -replace '^guvfx_u_','')
  $resultDir = Join-Path $full "_obs"

  if ($Mode -eq "Ensure") {
    # Grant ONLY ReadAndExecute to the identity on the observer tooling (never write). Additive icacls; the dir
    # is admin-owned and outside every slot tree, so the identity cannot modify the observer.
    & icacls $ObserverDir /grant ("{0}:(OI)(CI)RX" -f $Username) | Out-Null
    # The per-account result dir (inside the tenant runtime) must exist and be WRITABLE by guvfx_u_<id> (the
    # observer writes its snapshot there; the LocalSystem trigger reads it). It is confined to the tenant tree.
    if (-not (Test-Path -LiteralPath $resultDir)) { New-Item -ItemType Directory -Path $resultDir -Force | Out-Null }
    & icacls $resultDir /grant ("{0}:(OI)(CI)M" -f $Username) | Out-Null
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
      # Launch WINDOWLESS (pyw = the windowless Python launcher). The console launcher (py) drew a visible
      # C:\Windows\py.EXE console into the tenant's own RemoteApp session and stole keyboard focus from the MT5
      # login dialog (AJ#3 input blocker). pyw runs the same script with no console/window/focus-steal; the
      # observe-runner drift-check (Invoke-GuvfxObserver.ps1) already allows pyw.
      $action = New-ScheduledTaskAction -Execute "pyw" -Argument ('"{0}" --account {1}' -f $obsPy, $acctId)
      $principal = New-ScheduledTaskPrincipal -UserId $Username -LogonType Interactive -RunLevel Limited
      $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
      Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    }
  }

  # Verify: harness present + identity has RX on the observer dir + writable result dir + the on-demand task
  # exists (exactly one). Any gap fails closed - PREPARE_OBSERVER cannot report success on partial staging.
  $rules = (Get-Acl -Path $ObserverDir).Access | Where-Object { $_.IdentityReference -like "*$Username" -and $_.FileSystemRights -match "ReadAndExecute|Read" }
  $result.acl_ok = [bool]$rules
  $result.result_dir_ok = [bool](Test-Path -LiteralPath $resultDir -PathType Container)
  $result.task_present = [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
  if (-not ($result.tooling_present -and $result.acl_ok -and $result.result_dir_ok -and $result.task_present)) { Fail "observer preparation incomplete" }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch {
  # Diagnostic hardening (AJ#3): a terminating exception must be DIAGNOSABLE, not collapsed to "error".
  # Emit a stable reason plus safe, machine-readable exception metadata. This primitive handles NO secret
  # (no password/stdin arg), and username/runtime paths are non-secret derived identities, so surfacing the
  # exception type/HResult/message here cannot leak a credential. The message is single-lined and capped.
  $result.ok = $false
  $result.reason = "observer_prepare_exception"
  $ex = $_.Exception
  if ($ex) {
    $result.exception_type = $ex.GetType().Name
    try { $result.exception_hresult = ("0x{0:X8}" -f $ex.HResult) } catch { }
    $msg = ("" + $ex.Message) -replace "[\r\n]+", " "
    $msg = $msg.Trim()
    if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
    $result.exception_message = $msg
  }
  $result | ConvertTo-Json -Compress
  exit 1
}
