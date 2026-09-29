<#
  Probe-GuvfxSession.ps1  (P3 reboot-recovery readiness - READ-ONLY)

  Reports THIS account's OWN RDS session state (via qwinsta) so a future, human-gated recovery reconciler can tell
  which per-tenant terminals need re-establishing after a cold boot. It mutates NOTHING: no launch, no login, no
  session logoff/reset, no order, no file/registry change. It is a pure observation.

  Confinement (defence in depth over the signed dispatcher, which already refuses Customer Zero and derives the
  identity server-side): refuse reserved ids (Customer Zero), and require -Username == guvfx_u_<AccountId>. There is
  NO caller-supplied username or session target - the dispatcher injects the server-derived identity only, and this
  script filters qwinsta to exactly that derived username.

  RULE 11: a NEGATIVE result (no session for this account) is only trusted when qwinsta actually produced parseable
  output. If qwinsta yields nothing at all, that is a measurement failure (ok:false, reason qwinsta_no_output) - it
  is NOT reported as session_found:false. ``sessions_seen`` is included so a certifier can confirm the probe saw
  real data (a positive control: the host always has at least the services/console rows).

  ASCII-only (RULE 9). Emits one compact JSON object as its last line.

  LIMITATIONS (documented, acceptable for the DARK/unarmed op; revisit before any P4/P5 caller is armed):
    * English-locale host assumed: the header token ("SESSIONNAME") and the "Active" state literal are the
      en-US qwinsta strings. The host is pinned en-US; on a non-English UI, session_active would read false and
      the header row would be miscounted. A locale-agnostic parse is deferred (over-engineering for a pinned host).
    * Single session per user assumed: the host runs fSingleSessionPerUser=1, so a tenant has at most one session
      and the first match is the only match. If that invariant changed, the first-listed session would be reported.
    * The authoritative behavioural proof (real qwinsta output, active + disconnected + no-session cases) is the
      host smoke gate run before arming; these are structural/read-only guarantees, not a full behavioural cert.
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][int]$AccountId
)
$ErrorActionPreference = "Stop"
# SACRED identities, never probed here: Customer Zero (1) + the account-18 demo-control account. This matches the
# sibling identity-confining primitives (Contain-GuvfxLiveUpdate / Relaunch-GuvfxTerminal reserve @(1, 18)); it is
# defence in depth even though the probe is read-only. Customer Zero is ALSO refused server-side (dispatcher
# reserved default {1}) before this op maps; account 18 is added here to keep the estate's sacred-identity set whole.
$RESERVED_ACCOUNT_IDS = @(1, 18)

$result = [ordered]@{
  ok = $false; account_id = $AccountId; username = $Username; session_found = $false;
  session_state = ""; session_active = $false; session_id = -1; sessions_seen = 0; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

try {
  # --- identity confinement (defence in depth) ---
  if ($RESERVED_ACCOUNT_IDS -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }

  # --- read-only session probe ---
  # Run bare qwinsta (ALL sessions) and filter to the server-derived username in PowerShell; the username is never
  # passed to qwinsta as an argument (no caller-influenced argument surface).
  $lines = @(qwinsta 2>$null)
  # RULE 11 positive control: qwinsta must have produced output. Count the non-empty, non-header data rows we can
  # actually tokenize; the host always has at least the services (0) / console rows, so 0 means the probe is blind.
  $dataRows = 0
  $found = $false; $state = ""; $sid = -1

  # Iterate EVERY data row so sessions_seen is the true count (a strong RULE-11 positive control); the first row
  # matching the derived username is recorded WITHOUT breaking the count. The host runs fSingleSessionPerUser=1, so
  # a tenant has at most one session and "first match" is the only match; if that invariant ever changed, this would
  # report the first-listed session (documented in the header limitations).
  foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    # Strip the leading current-session marker '>' (and '#') and any leading whitespace, then split on runs of
    # whitespace. A disconnected session has a BLANK SESSIONNAME column, so the username can be the first token.
    $clean = $line -replace '^[>#\s]+', ''
    $parts = @($clean -split '\s+' | Where-Object { $_ -ne "" })
    if ($parts.Count -eq 0) { continue }
    # Skip the header row ("SESSIONNAME USERNAME ID STATE TYPE DEVICE"); it is the only row with no numeric ID and
    # a first token of exactly 'SESSIONNAME' (English-locale host; see header limitations).
    if ($parts[0] -eq "SESSIONNAME") { continue }
    $dataRows++

    if (-not $found) {
      $ui = -1
      for ($i = 0; $i -lt $parts.Count; $i++) { if ($parts[$i] -eq $Username) { $ui = $i; break } }
      if ($ui -ge 0) {
        $found = $true
        # The first numeric token after the username is the session ID; the token after it is the STATE.
        for ($j = $ui + 1; $j -lt $parts.Count; $j++) {
          if ($parts[$j] -match '^\d+$') {
            $sid = [int]$parts[$j]
            if (($j + 1) -lt $parts.Count) { $state = $parts[$j + 1] }
            break
          }
        }
      }
    }
  }

  $result.sessions_seen = $dataRows
  if ($dataRows -lt 1) { Fail "qwinsta_no_output" }   # blind measurement - never trust a negative here (RULE 11)

  $result.session_found = $found
  $result.session_state = $state
  $result.session_id = $sid
  # qwinsta abbreviates "Disconnected" to "Disc"; treat only an explicit "Active" state as an active session.
  $result.session_active = ($found -and ($state -eq "Active"))
  $result.ok = $true
  $result.reason = if ($found) { "ok" } else { "no_session_for_account" }
  Emit
  exit 0
}
catch {
  Fail "probe_session_exception"
}
