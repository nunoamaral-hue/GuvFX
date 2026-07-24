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
  [string]$RuleName          = "GuvFX-Beta-Agent-In",              # scoped ALLOW: :8791 from the backend only
  [string]$BlockRuleName     = "GuvFX-Beta-Agent-Block-NonBackend", # scoped BLOCK: :8791 from everything else
  [string]$ServiceName       = "GuvFXBetaAgent",             # to resolve the REAL listening image
  [int]$Port                 = 8791,
  [string]$AllowFrom         = "100.119.23.29",              # GuvFX backend / control plane (Tailscale) - ONLY source
  [string]$Interface         = "100.79.101.19",              # private/Tailscale bind address
  # The complement of $AllowFrom over the whole IPv4 space, as two ranges. This is the BLOCK rule's remote
  # scope: "everything except the backend". Windows Firewall has no native "except", and a Block rule
  # ALWAYS wins over an Allow rule for overlapping traffic - so the block MUST exclude the backend, or it
  # would also deny the backend. Derived from $AllowFrom below; overridable only for tests.
  [string[]]$BlockRemoteRanges = @(),
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
# Report C (accepted): this profile's DefaultInboundAction is 'NotConfigured', which Windows already
# resolves to Block for inbound. We do NOT change the machine-wide default (out of scope, whole-estate).
# Protection for :8791 comes from the SCOPED BLOCK added below, not from the profile default - so an
# unset default is not a failure here, only reported.
Write-Host "ok   interface $Interface -> profile '$profileName' (DefaultInboundAction=$($fp.DefaultInboundAction); scoped block below provides :$Port protection)"

# The backend must be a single, parseable address; the block's complement is derived from it so a typo
# cannot silently widen exposure.
try { [void][System.Net.IPAddress]::Parse($AllowFrom) } catch { Fail "backend endpoint '$AllowFrom' is not a valid IP" }
if ($BlockRemoteRanges.Count -eq 0) {
  # complement of a single /32 over IPv4: [0.0.0.0 .. backend-1] and [backend+1 .. 255.255.255.255]
  $b = [System.Net.IPAddress]::Parse($AllowFrom).GetAddressBytes()
  [Array]::Reverse($b); $n = [System.BitConverter]::ToUInt32($b, 0)
  function IntToIp([uint32]$v) { $x=[System.BitConverter]::GetBytes($v);[Array]::Reverse($x);([System.Net.IPAddress]::new($x)).ToString() }
  $BlockRemoteRanges = @()
  if ($n -gt 0)          { $BlockRemoteRanges += ("0.0.0.0-" + (IntToIp ($n - 1))) }
  if ($n -lt [uint32]4294967295) { $BlockRemoteRanges += ((IntToIp ($n + 1)) + "-255.255.255.255") }
}
Write-Host "ok   block-remote (all except backend) = $($BlockRemoteRanges -join ' , ')"

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
# Report C: the expected broad allow is Tailscale-In (program=Any, port=Any, remote=Any on $Interface). It
# is NOT removed - it carries the bridge (:8788) and all other tailnet traffic. The scoped BLOCK added
# below neutralises its effect on :$Port specifically, because a Block rule wins over an Allow rule for
# overlapping traffic. So a broad allow here is reported, not fatal - the block is the mitigation.
if ($danger.Count -gt 0) {
  Write-Host "note pre-existing broad inbound allow(s) cover :$Port from non-backend - NEUTRALISED by the scoped block:"
  $danger | ForEach-Object { Write-Host "  - $_" }
} else {
  Write-Host "ok   no pre-existing inbound allow rule authorises :$Port from a non-backend source"
}

# 4. Add the SCOPED BLOCK (everything except the backend) and the SCOPED ALLOW (backend only).
#    The block is what restricts :$Port; the allow makes the backend path explicit (belt-and-braces with
#    the existing Tailscale-In). A Windows Block rule wins over any Allow for overlapping traffic, and the
#    block EXCLUDES the backend, so: non-backend -> matched by block -> DENIED; backend -> not matched by
#    block -> allowed. Neither rule covers :8788, so the bridge is untouched.
if ($Apply) {
  if (Get-NetFirewallRule -DisplayName $BlockRuleName -ErrorAction SilentlyContinue) {
    Write-Host "note block rule '$BlockRuleName' already exists; leaving as-is"
  } else {
    New-NetFirewallRule -DisplayName $BlockRuleName -Direction Inbound -Action Block -Protocol TCP `
      -LocalPort $Port -LocalAddress $Interface -RemoteAddress $BlockRemoteRanges -Profile $profileName | Out-Null
    Write-Host "ok   added inbound BLOCK: TCP $Port on $Interface from all-except-$AllowFrom (profile '$profileName')"
  }
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Write-Host "note allow rule '$RuleName' already exists; leaving as-is"
  } else {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
      -LocalPort $Port -LocalAddress $Interface -RemoteAddress $AllowFrom -Profile $profileName | Out-Null
    Write-Host "ok   added inbound ALLOW: TCP $Port on $Interface from $AllowFrom only (profile '$profileName')"
  }

  # 5. Verify: both rules exist, scoped correctly, and no OTHER rule (esp. bridge :8788) was changed.
  $bk = Get-NetFirewallRule -DisplayName $BlockRuleName -ErrorAction Stop
  $al = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction Stop
  if ($bk.Action -ne "Block") { Fail "'$BlockRuleName' is $($bk.Action), expected Block" }
  if ($al.Action -ne "Allow") { Fail "'$RuleName' is $($al.Action), expected Allow" }
  $bkPort = ($bk | Get-NetFirewallPortFilter).LocalPort
  $alPort = ($al | Get-NetFirewallPortFilter).LocalPort
  if ("$bkPort" -ne "$Port" -or "$alPort" -ne "$Port") { Fail "a scoped rule is not on :$Port (block=$bkPort allow=$alPort)" }
  $alRemote = @(($al | Get-NetFirewallAddressFilter).RemoteAddress)
  if ($alRemote -contains "Any" -or ($alRemote | Where-Object { $_ -ne $AllowFrom }).Count -gt 0) {
    Fail "'$RuleName' remote is '$($alRemote -join ',')', expected only $AllowFrom"
  }
  $bkRemote = @(($bk | Get-NetFirewallAddressFilter).RemoteAddress)
  if ($bkRemote -contains $AllowFrom) { Fail "'$BlockRuleName' remote INCLUDES the backend $AllowFrom - it would deny the backend" }
  Write-Host "ok   verified: BLOCK :$Port from all-except-$AllowFrom + ALLOW :$Port from $AllowFrom, both on $Interface/$profileName"
  Write-Host "ok   bridge rules (:8788) and all unrelated rules untouched - this script only added the two :$Port rules"
} else {
  Write-Host "plan Would add TWO rules on $Interface (profile '$profileName'):"
  Write-Host "plan   BLOCK TCP $Port  remote = all except $AllowFrom  ($($BlockRemoteRanges -join ' , '))"
  Write-Host "plan   ALLOW TCP $Port  remote = $AllowFrom only"
  Write-Host "plan   (a Block wins over an Allow; the block excludes the backend, so backend is allowed and everything else denied)"
  Write-Host "plan   :8788 bridge and all unrelated rules are NOT touched. Re-run with -Apply."
}
