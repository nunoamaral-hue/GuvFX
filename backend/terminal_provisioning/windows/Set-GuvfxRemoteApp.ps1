<#
  Stream 5 - publish/verify the per-identity terminal64 RemoteApp (idempotent, reversible).

  Publishes EXACTLY ONE RemoteApp (alias 'terminal64') pointing at the per-account portable terminal:
      path: C:\GuvFX\accounts\<id>\terminal\terminal64.exe
      args: /portable
  via the TSAppAllowList registry (the reviewed CZ H2 mechanism), with the allow-list in FilterByName mode so
  NO full desktop is published. Idempotent (re-running re-asserts the same single entry); -Verify checks exact
  path + args; -Remove rolls the single entry back.

  ASCII-only (RULE 9). RULE 11: publication is confirmed by reading the registry value back (a positive control),
  never by "the command exited 0". Emits a single compact JSON object.

  Usage:
    powershell -NoProfile -File Set-GuvfxRemoteApp.ps1 -Mode Ensure -TerminalRoot 'C:\GuvFX\accounts\14\terminal'
    powershell -NoProfile -File Set-GuvfxRemoteApp.ps1 -Mode Verify -TerminalRoot 'C:\GuvFX\accounts\14\terminal'
    powershell -NoProfile -File Set-GuvfxRemoteApp.ps1 -Mode Remove -TerminalRoot 'C:\GuvFX\accounts\14\terminal'
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Ensure","Verify","Remove")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$TerminalRoot,
  # Stream 6 (M2): the server-derived per-account alias (guvfx_mt5_<id>); Customer Zero keeps legacy terminal64.
  [string]$Alias = "terminal64",
  # ARMING (Sponsor 2026-08-25): the RemoteApp start-program target. "terminal64" (default) publishes the legacy
  # per-tenant terminal64.exe /portable byte-identically. "launcher" publishes the certified NATIVE single-
  # instance launcher (C:\GuvFX\launcher\guvfx_launch.exe) with NO command line -- it derives the tenant identity
  # from the Windows token and runs the tenant's OWN terminal64 /portable idempotently (refresh/reconnect ->
  # exactly one terminal). Server-derived (gated by HOSTED_NATIVE_LAUNCHER_GATE_ENABLED); never customer-supplied.
  [ValidateSet("terminal64","launcher")][string]$Target = "terminal64"
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$LAUNCHER = "C:\GuvFX\launcher\guvfx_launch.exe"
if ($Alias -notmatch '^(terminal64|guvfx_mt5_[1-9][0-9]*)$') { throw "refusing: alias must be server-derived (terminal64 | guvfx_mt5_<id>)" }
$ALIAS = $Alias
$TSROOT = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Terminal Server\TSAppAllowList"
$APPKEY = "$TSROOT\Applications\$ALIAS"
$result = [ordered]@{ alias=$ALIAS; mode=$Mode; exe=""; args="/portable"; published=$false; exact=$false; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }

try {
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing: path traversal" }
  if (-not ($full.ToLower().StartsWith(($ACCOUNTS_BASE.ToLower() + "\")))) { Fail "refusing: outside accounts base" }
  if (-not ($full.ToLower().EndsWith("\terminal"))) { Fail "refusing: not a terminal root" }
  $exe = Join-Path $full "terminal64.exe"
  # Arming: the RemoteApp start-program becomes the native launcher instead of the runtime terminal64. BOTH
  # targets are published with the SAME command-line policy (CommandLineSetting=1 / RequiredCommandLine=/portable
  # -- see Ensure below). The launcher derives its identity from the Windows token and ignores argv entirely
  # (GuvfxLaunch.Main takes no parameters), so the fixed /portable is inert for it; publishing it byte-identically
  # to the proven terminal64 policy is what makes the Guacamole/FreeRDP client (which always sends
  # remote-app-args=/portable) able to start it.
  if ($Target -eq "launcher") { $exe = $LAUNCHER }
  $result.exe = $exe

  if ($Mode -eq "Remove") {
    # Defence in depth: never remove Customer Zero's published legacy alias through this per-account tool.
    if ($ALIAS -eq "terminal64") { Fail "refusing: Customer Zero (terminal64) alias removal is forbidden here" }
    if (Test-Path $APPKEY) { Remove-Item -Path $APPKEY -Recurse -Force }
    $result.published = [bool](Test-Path $APPKEY)
    $result.ok = (-not $result.published); if (-not $result.ok) { Fail "remove did not clear the entry" }
    $result | ConvertTo-Json -Compress; return
  }

  if ($Mode -eq "Ensure") {
    if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64.exe not present - materialise runtime first" }
    # FilterByName mode: only explicitly allow-listed apps are published (no full desktop).
    # CRITICAL (Stream 7D cert): `New-Item -Force` on an EXISTING registry key DELETES it and ALL its subkeys
    # (recreates it empty). Applied to the TSAppAllowList / Applications CONTAINERS it erased every OTHER
    # published alias -- it wiped Customer Zero's terminal64 when this per-account tool published a SECOND alias.
    # Create the containers ONLY when absent; never -Force a shared parent. -Force the LEAF alias key only (its
    # own values are (re)set below; that touches no sibling alias).
    $APPSROOT = "$TSROOT\Applications"
    if (-not (Test-Path $TSROOT))   { New-Item -Path $TSROOT   -Force | Out-Null }
    if (-not (Test-Path $APPSROOT)) { New-Item -Path $APPSROOT -Force | Out-Null }
    New-ItemProperty -Path $TSROOT -Name "fDisabledAllowList" -Value 1 -PropertyType DWord -Force | Out-Null
    New-Item -Path $APPKEY -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "Name" -Value $ALIAS -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "Path" -Value $exe -PropertyType String -Force | Out-Null
    # CommandLineSetting=1 with a fixed RequiredCommandLine=/portable for BOTH targets. RDS FORCES exactly this
    # command line (a customer-supplied one is overridden, never appended), so no customer argument can reach the
    # program -- the same isolation the launcher arming intended -- while remaining compatible with the delivery
    # payload, which always sends remote-app-args=/portable. The earlier launcher-only CommandLineSetting=2 ("no
    # command line permitted") caused RDS to REFUSE the client's /portable and tear the RemoteApp session down
    # immediately ("You have been disconnected") for every launcher tenant; =1/-/portable matches the proven,
    # already-working terminal64 publication exactly. The launcher ignores argv, so /portable is inert for it.
    New-ItemProperty -Path $APPKEY -Name "CommandLineSetting" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "RequiredCommandLine" -Value "/portable" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "ShowInTSWA" -Value 0 -PropertyType DWord -Force | Out-Null
  }

  # Verify (also the tail of Ensure): read the registry back and assert EXACTLY this alias -> exact path + args.
  if (-not (Test-Path $APPKEY)) { Fail "RemoteApp alias not published" }
  $p = Get-ItemProperty -Path $APPKEY
  $result.published = $true
  $pathOk = ($p.Path -ieq $exe)
  # Both targets: CommandLineSetting=1 with RequiredCommandLine fixed to /portable (RDS forces it; no customer arg).
  $argsOk = (([int]$p.CommandLineSetting -eq 1) -and ($p.RequiredCommandLine -eq "/portable"))
  # Per-account (M2): verify THIS alias resolves to THIS account's exact terminal64.exe + /portable. We do NOT
  # assert a machine-wide single-app count any more (multiple per-account aliases legitimately coexist); a
  # different account's alias points at ITS own tree, so cross-account program access is impossible by path.
  $result.exact = ($pathOk -and $argsOk)
  if (-not $result.exact) { Fail "published RemoteApp alias does not match this account's exact path/args" }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.reason="error"; $result | ConvertTo-Json -Compress; exit 1 }
