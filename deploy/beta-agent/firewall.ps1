# CVM-Inc-3 B2/B3P-1 - host firewall rule for the agent port, hardened against pre-existing broad allows.
# DARK ARTEFACT: RUN ONLY in B3, on the host, as Administrator. Dry-run by default; pass -Apply to add the rule.
#
# Verification B-8: adding a scoped allow is NOT enough on its own. A pre-existing program-scoped allow for the
# agent's python.exe (very common - Windows offers "Allow" the first time a Python process listens) or any
# broad rule covering :8791 from a non-backend source would authorise the agent port from EVERY tailnet peer,
# bypassing the new rule. So this script HARD-FAILS if such a rule exists, asserts the interface profile is
# default-deny inbound, and scopes the new rule to that profile only (never -Profile Any / Public).
#
# It NEVER touches the bridge (:8788) or backtest (:8787) rules.
param(
  [string]$RuleName          = "GuvFX-Beta-Agent-In",
  [string]$ServiceName       = "GuvFXBetaAgent",             # to resolve the REAL listening image
  [int]$Port                 = 8791,
  [string]$AllowFrom         = "100.119.23.29",              # GuvFX backend / control plane (Tailscale) - ONLY source
  [string]$Interface         = "100.79.101.19",              # private/Tailscale bind address
  # Fallback listener-image list. The service host is PythonService.exe (pywin32); this list is only a
  # fallback for rule scoping. It names the beta VENV interpreter, never C:\GuvFX\python311.exe (the
  # Python installer) - see install_service.ps1.
  [string[]]$AgentProgramPaths = @("C:\GuvFX\beta\agent-venv\Scripts\python.exe"),
  [switch]$Apply
)
$ErrorActionPreference = "Stop"

function Fail($m) { throw "firewall.ps1: $m" }
# Canonicalise a program path for comparison: expand %VAR% forms + resolve to a full path, lowercased. (Residual:
# 8.3 short names / symlinks are not resolved here - the Tailscale ACL is the second layer for that edge.)
function Canon($p) {
  if (-not $p) { return "" }
  $x = [System.Environment]::ExpandEnvironmentVariables("$p").Trim('"')
  try { $x = [System.IO.Path]::GetFullPath($x) } catch {}
  return $x.ToLower()
}

# 1. Resolve the profile bound to the Tailscale interface and assert it is default-deny inbound.
$ip = Get-NetIPAddress -IPAddress $Interface -ErrorAction SilentlyContinue
if (-not $ip) { Fail "interface $Interface not found on this host" }
$conn = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue
if (-not $conn) {
  Fail "interface $Interface has no network connection profile; cannot determine which firewall profile governs it - classify the interface (or resolve manually) before applying"
}
$cat = "$(@($conn.NetworkCategory)[0])"
$profileName = switch ($cat) { "DomainAuthenticated" { "Domain" } default { $cat } }   # NetworkCategory -> firewall profile name
if ($profileName -eq "Public") { Fail "interface $Interface is on the Public profile; refusing (expected Private/Domain)" }
if ($profileName -notin @("Private", "Domain")) { Fail "unexpected profile '$profileName' for $Interface; resolve manually" }
$fp = Get-NetFirewallProfile -Name $profileName
if ($fp.DefaultInboundAction -ne "Block") {
  Fail "profile '$profileName' DefaultInboundAction is '$($fp.DefaultInboundAction)', expected 'Block' - the scoped allow is not safe without default-deny inbound"
}
Write-Host "ok   interface $Interface -> profile '$profileName' (DefaultInboundAction=Block)"

# 2. Resolve the ACTUAL listening image of the installed service. Under pywin32 the socket is owned by the
#    service host image (e.g. PythonService.exe), NOT python311.exe - so a pre-existing broad allow for the
#    real host image must be matched. Fail-safe: if the service is not yet installed we cannot resolve it.
$agentImages = @($AgentProgramPaths | ForEach-Object { Canon $_ })
$svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
if ($svc -and $svc.PathName) {
  $pn = $svc.PathName
  $img = if ($pn.StartsWith('"')) { ($pn.Substring(1) -split '"', 2)[0] } else { ($pn -split '\s', 2)[0] }
  if ($img) { $agentImages += (Canon $img); Write-Host "ok   resolved service listener image: $img" }
} else {
  Fail "service '$ServiceName' not found - run install_service.ps1 -Apply FIRST so the real listener image can be matched against pre-existing rules"
}
$agentImages = $agentImages | Where-Object { $_ } | Select-Object -Unique

# 3. Enumerate every ENABLED inbound allow rule; fail on any that could ALSO authorise :8791 from a non-backend peer.
$danger = @()
$agentProg = $agentImages
foreach ($r in (Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -ErrorAction SilentlyContinue)) {
  if ($r.DisplayName -eq $RuleName) { continue }   # our own rule (idempotent re-run)
  $pf = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
  $af = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
  $appf = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue

  # Does this rule cover TCP/:8791 (explicit, a covering range, or Any)?
  $coversPort = $false
  if ($pf) {
    if ($pf.Protocol -in @("TCP", "Any")) {
      foreach ($lp in @($pf.LocalPort)) {
        if ($lp -eq "Any") { $coversPort = $true }
        elseif ($lp -eq "$Port") { $coversPort = $true }
        elseif ($lp -match "^\d+-\d+$") {
          $lo,$hi = $lp -split "-"; if ([int]$lo -le $Port -and $Port -le [int]$hi) { $coversPort = $true }
        }
      }
    }
  }
  if (-not $coversPort) { continue }

  # Does it admit a remote OTHER than the backend?
  # $af is Get-NetFirewallAddressFilter with -ErrorAction SilentlyContinue, so it CAN be $null. @($null)
  # would then yield a one-element array holding $null: -contains "Any" is false and the Where-Object
  # yields nothing, so an unreadable rule would be judged NOT to admit a foreign remote - this exposure
  # gate would fail OPEN. Treat an unreadable filter as maximally exposing instead.
  if ($null -eq $af) {
    Write-Host "WARN rule '$($r.DisplayName)': address filter unreadable - treating as exposed"
    $remote = @("Any")
  } else {
    $remote = @($af.RemoteAddress)
  }
  $nonBackendRemote = ($remote -contains "Any") -or (($remote | Where-Object { $_ -ne $AllowFrom }).Count -gt 0)

  # Is it program-scoped to the agent listener image (or Any program)? Handle "Any" BEFORE canonicalising
  # (GetFullPath would otherwise turn the literal "Any" into a path).
  $progRaw = if ($appf -and $appf.Program) { "$($appf.Program)" } else { "Any" }
  if ($progRaw -eq "Any") { $prog = "any"; $broadProgram = $true }
  else { $prog = Canon $progRaw; $broadProgram = ($agentProg -contains $prog) }

  if ($nonBackendRemote -and $broadProgram) {
    $danger += "[$($r.DisplayName)] port=$($pf.LocalPort) remote=$($remote -join ',') program=$prog"
  }
}
if ($danger.Count -gt 0) {
  Write-Host "DANGER pre-existing inbound allow rules could expose :$Port beyond the backend:"
  $danger | ForEach-Object { Write-Host "  - $_" }
  Fail "resolve the above (narrow/remove them or add a higher-priority block for :$Port from non-backend) BEFORE adding the agent rule or starting the service"
}
Write-Host "ok   no pre-existing inbound allow rule authorises :$Port from a non-backend source"

# 4. Add the single scoped allow (only with -Apply).
if ($Apply) {
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Write-Host "note rule '$RuleName' already exists; leaving as-is"
  } else {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
      -LocalPort $Port -LocalAddress $Interface -RemoteAddress $AllowFrom -Profile $profileName | Out-Null
    Write-Host "ok   added inbound allow: TCP $Port on $Interface from $AllowFrom only (profile '$profileName')"
  }
} else {
  Write-Host "plan Would add inbound allow: TCP $Port on $Interface from $AllowFrom only (profile '$profileName'). Re-run with -Apply."
}
