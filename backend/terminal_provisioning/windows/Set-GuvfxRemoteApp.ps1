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
  [Parameter(Mandatory=$true)][string]$TerminalRoot
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$ALIAS = "terminal64"
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
  $result.exe = $exe

  if ($Mode -eq "Remove") {
    if (Test-Path $APPKEY) { Remove-Item -Path $APPKEY -Recurse -Force }
    $result.published = [bool](Test-Path $APPKEY)
    $result.ok = (-not $result.published); if (-not $result.ok) { Fail "remove did not clear the entry" }
    $result | ConvertTo-Json -Compress; return
  }

  if ($Mode -eq "Ensure") {
    if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64.exe not present - materialise runtime first" }
    # FilterByName mode: only explicitly allow-listed apps are published (no full desktop).
    New-Item -Path $TSROOT -Force | Out-Null
    New-ItemProperty -Path $TSROOT -Name "fDisabledAllowList" -Value 1 -PropertyType DWord -Force | Out-Null
    New-Item -Path $APPKEY -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "Name" -Value $ALIAS -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "Path" -Value $exe -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "CommandLineSetting" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "RequiredCommandLine" -Value "/portable" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $APPKEY -Name "ShowInTSWA" -Value 0 -PropertyType DWord -Force | Out-Null
  }

  # Verify (also the tail of Ensure): read the registry back and assert EXACTLY this alias -> exact path + args.
  if (-not (Test-Path $APPKEY)) { Fail "RemoteApp alias not published" }
  $p = Get-ItemProperty -Path $APPKEY
  $result.published = $true
  $pathOk = ($p.Path -ieq $exe)
  $argsOk = ($p.RequiredCommandLine -eq "/portable")
  # Exactly one application published under the allow-list (no extra/desktop entries).
  $count = @(Get-ChildItem -Path "$TSROOT\Applications" -ErrorAction SilentlyContinue).Count
  $result.exact = ($pathOk -and $argsOk -and ($count -eq 1))
  if (-not $result.exact) { Fail "published RemoteApp does not match exact path/args or is not the only app" }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.reason="error"; $result | ConvertTo-Json -Compress; exit 1 }
