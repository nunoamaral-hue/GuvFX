# 0022 — LiveUpdate containment (Variant A): deny the slot identity write on its own MT5 update-staging

- Date: 2026-07-31
- Status: Accepted

## Context

The beta per-slot execution model requires the running `terminal64.exe` to remain **beneath the materialised
slot dir** `C:\GuvFX\beta\slots\<n>\terminal`. This executable-path containment is the control that protects the
operator's production IS6 terminals (same image name, different identity/session) from being attributed-to or
terminated-by a beta slot. It is enforced by VERIFY (`observe_process` requires `is_beneath(image, slot_path)`,
`win_primitives.py`) and by STOP (a fixed per-slot scheduled task terminates **only** the exact path
`<slot>\terminal64.exe`, `install_pool.ps1`).

MT5's **LiveUpdate** breaks this by construction: on launch it stages a copy of `terminal64.exe` into the slot
account's **roaming profile** (`%APPDATA%\MetaQuotes\WebInstall` and `…\Terminal\<hash>\liveupdate`) and
relaunches from there — an executable *outside* the slot. This happens even under `/portable` (which redirects
the data folder into the slot but not the update-staging path). A relocated runtime reads `PRESENT_INVALID`
(VERIFY) and, critically, **survives the exact-path STOP task** — both reproduced on the host.

MetaTrader 5 has **no supported way to disable LiveUpdate** (MetaQuotes: the auto-updater "can not be
deactivated"); this was established by a prior architecture investigation that also rejected AppLocker/WDAC
(machine-wide on the co-hosted production box), profile/`%APPDATA%` redirection (weakens VERIFY), read-only
images (relocation happens in the profile, not the install dir) and network controls (rejected class).

## Verified facts

- Host reversible fail-closed probe (2026-07-31): with a Deny-write on the slot identity's own
  `%APPDATA%\MetaQuotes` update-staging, MT5 build 6036 did **not** relocate, ran from the canonical slot exe,
  stayed alive ~5.7 min, VERIFY = `PRESENT_VALID`, and the exact-path STOP task reached `ABSENT`. Without the
  Deny, the same build relocated and the STOP task missed it. Production terminals untouched throughout.
- The beta agent runs as the least-privilege virtual account `NT SERVICE\GuvFXBetaAgent` (deliberately not
  LocalSystem, `install_service.ps1`); its mutating surface (`win_ops.py`) has **no ACL-write** and cannot reach
  a user profile. The launch wrapper `slot_launch.ps1` runs **as the slot identity** and already performs
  process-object ACL work.
- The slot identity holds **inherited FullControl** on its own `%APPDATA%\MetaQuotes`, so the wrapper can set
  this DACL with no admin / `SeRestorePrivilege`.

## Assumptions

- MT5's fail-closed behaviour (continue on the in-slot build when the update is blocked) is proven for build
  6036; per RULE 11 it must be re-confirmed as a **per-build golden-acceptance criterion** (a future MetaQuotes
  build could refuse to run when it cannot update). The Golden Integrity Rules already require a dedicated,
  never-launched, LiveUpdate-disabled golden.

## Decision drivers

Preserve executable-path containment (never weaken VERIFY or STOP); least privilege (no new agent capability);
zero production blast radius; reversibility; fail-closed; keep the existing single lifecycle (no parallel
lifecycle, no speculative machine-wide policy).

## Options considered

- **Variant A — launch-wrapper Deny-write on the slot's own `%APPDATA%` staging (CHOSEN).** Earliest
  interception; zero prod blast radius (touches only the slot's own profile); no new privilege; most reversible.
  Residual: the slot owns its profile, so a *malicious* in-slot tenant could strip its own Deny — bounded
  (un-stoppable-but-still-sandboxed; cannot reach production).
- **Variant B — installer admin-authoritative permanent Deny.** Robust against a malicious tenant, but adds a
  new install-gate responsibility (force first batch logon to create the profile), a permanent derived-hash-path
  ACL, and prod-adjacent host mutation. Deferred; documented as the hardening path.
- **AppLocker/WDAC execution-control.** Rejected: machine-wide enforcement on the box that also runs the
  operator's production terminals (would even block prod MT5's own post-update relaunch); fails open if AppIDSvc
  stops; CLM landmine. Deferred behind its own ADR + a real in-slot arbitrary-code threat.

## Decision

Implement **Variant A** in `slot_launch.ps1`. Before launching `terminal64.exe`, `Apply-LiveUpdateContainment`
(runs as the slot identity):

1. resolves the slot's own `%APPDATA%\MetaQuotes` (OS-resolved, the same env `terminal64` inherits);
2. empties the update-staging dirs (`WebInstall` — the load-bearing download chokepoint — and any existing
   `Terminal\<hash>\liveupdate`) so generation N+1 begins clean;
3. adds one **inheritable Deny(Write)** for the current-token **SID** on each, idempotently (remove-then-add);
4. **reads the DACL back by SID** (`GetAccessRules(..., [SecurityIdentifier])`, never `.Access`) and, if the
   Deny is not confirmed in force, calls `Fail` (exit 2) so `terminal64` is **not launched** (fail-closed — a
   relocated runtime would be unstoppable by the exact-path STOP task).

VERIFY, STOP, `is_beneath`, and process matching are **unchanged**. The wrapper's hash pin
(`$LaunchWrapperSha256`, `install_pool.ps1`) is recomputed.

**"Removed by RELEASE" deviation (flagged to the Sponsor):** the least-privilege agent structurally cannot write
a user profile, so RELEASE-removal is **not** agent-driven. Instead: the wrapper self-cleans every START (clean
generation), and `uninstall.ps1` removes the Deny at **decommission** (resolving the real profile dir by SID).

## Consequences

- **Positive:** the runtime always launches from the canonical slot exe → VERIFY `PRESENT_VALID` and the
  exact-path STOP reaches `ABSENT` (host-proven); containment preserved by construction, not by interception; no
  new privilege, no machine-wide policy, no production impact; reversible by re-staging the wrapper.
- **Negative / follow-ups:** (1) the per-hash `liveupdate` Deny only covers hashes existing at launch — the
  guarantee rests on the always-applied WebInstall download-chokepoint (host-proven sufficient for 6036); (2)
  the malicious-in-slot-tenant residual (bounded) is closed only by Variant B or AppLocker, both deferred; (3)
  the fail-closed behaviour is a **per-build** golden-acceptance criterion (RULE 11).
- **Out of scope:** broker connectivity / market data (Q5) belongs to the separate broker-login stage
  (`PROVISIONING_REQUIRE_BROKER_LOGIN`); the containment does not touch the network/broker path.

Adversarial review (2026-07-31, multi-lens) found and fixed a would-brick-every-launch defect (read-back had
used `.Access`, which name-translates to `HOST\name` and never equals the SID string) plus three lower-severity
improvements (whole-staging purge; honest WebInstall-chokepoint documentation; decommission profile resolved by
SID). Host lifecycle validation evidence is recorded separately in the implementation handoff.
