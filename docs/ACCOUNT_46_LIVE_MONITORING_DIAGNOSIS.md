# Account 46 (TradersWay LIVE 55442) — LIVE-monitoring diagnosis (2026-10-08)

**STRICTLY READ-ONLY investigation.** No MT5 restart/reconnect, no orders, no flag change, no self-connect, no
execution/recovery activation. Diagnosis + smallest-safe-fix proposal only — not implemented (Sponsor-gated).

## Observed symptoms (Sponsor)
MT5 RemoteApp connected; a real XAUUSD position visible in MT5; Dashboard shows **zero open trades**; Dashboard
reports **one account unreachable**; the **first-time broker-setup warning persists** after login.

## Evidence (read-only, prod)
- TradingAccount 46: broker `TradersWay`, login `55442`, `is_demo=False`, **`account_environment=LIVE`**,
  `disconnected_at=None`, `is_active=False`, owner `support@guvfx.com`. Trades: **0 open / 0 total**.
- HostedMt5Workspace projection: `observation_version=3628`, `last_decision_at`=today (actively, freshly observed),
  `proj_process_running=True`, `proj_ipc_available=True`, **`proj_connected=True`** (terminal up + logged in +
  broker-connected — the MT5 bridge is HEALTHY), **`proj_account_match=False`**, `proj_trade_allowed=False`,
  `proj_execution_ready=False`, **`canonical_state=WAITING_FOR_LOGIN` / reason `ERROR`**, `broker_ever_matched=False`.
- AccountProvisioning: `PROVISIONED`, credential present.
- **`hosted_live_monitoring_enabled() = False`.** No LIVE execution/recovery/`MT5_ALLOW_LIVE`/self-connect flags set.

## Root cause (SINGLE, shared by all three symptoms)
Account 46 is a **LIVE** account, but **`HOSTED_LIVE_MONITORING_ENABLED` is OFF**. The identity matcher
(`hosted_workspace/live_observe.py:242-247`) builds the EXPECTED identity as **demo-only** (`exp_is_demo=None,
exp_allow_live=False`) unless LIVE monitoring is on; it opts a LIVE account into a LIVE-identity match **only** when
the flag is armed. So the connected LIVE terminal is compared against a *demo* expectation and fails
`account_match` (`proj_account_match=False`) → `canonical_state=WAITING_FOR_LOGIN`. Everything else cascades:
1. **Zero open trades** — positions are ingested only for a matched/monitored account; unmatched ⇒ nothing ingested
   (the real XAUUSD position is never read into GuvFX).
2. **"Account unreachable"** — `account_match=False` + `WAITING_FOR_LOGIN` is surfaced as not-logged-in/unreachable.
3. **First-setup warning persists** — the `broker_ever_matched` latch sets only on `connected AND account_match`
   (`persistence.py:194`); `account_match` never becomes True, so the latch never sets.

`is_active=False` is a **downstream consequence**, not a separate bug: hosted accounts are created `is_active=False`
by design (`provisioning.py:308`) and activated only after a successful match (ADR-0044); with the match blocked,
activation never runs. The MT5 bridge itself is healthy — this is purely an expected-identity gating effect.

**This is correct fail-closed behaviour:** the system refuses to monitor/match a LIVE account until LIVE monitoring is
deliberately enabled. The first-LIVE-monitoring certification (which arms the flag) is Sponsor-gated and has not been
run — so Account 46's state is the *expected* state of a LIVE account that exists before that cert.

## Smallest safe fix (PROPOSAL — not implemented; Sponsor-gated)
Enable **`HOSTED_LIVE_MONITORING_ENABLED`** (LIVE monitoring ON; **execution + recovery stay OFF**, self-connect OFF,
no `MT5_ALLOW_LIVE`, no authorization, no order) — i.e. run the first-LIVE-monitoring certification per
`docs/FIRST_LIVE_MONITORING_RUNBOOK.md`, with Account 46 as the subject. Then the matcher expects a LIVE identity, the
connected terminal matches (`proj_account_match=True`), `broker_ever_matched` latches (warning clears),
`canonical_state` advances, the account activates, and its positions/equity ingest (the XAUUSD position + balance
become visible; "unreachable" clears). This is **read-only observation** (no orders — execution remains blocked at
readiness condition 11 + the bridge gate). No code change is required; it is a flag/cert the Sponsor controls.

**Caveat for Sponsor review:** Account 46 is a **real, funded** TradersWay LIVE account (not the fresh zero-balance
account the runbook anticipated). Enabling LIVE monitoring begins **read-only** observation of a funded live account
(balance/equity/positions) — no trading. It is a global flag; Account 46 is currently the only LIVE hosted account, so
it is the sole subject. **Recommend the Sponsor review + authorize** before enabling; do not enable as a silent fix.

---

## First LIVE-monitoring certification attempt (2026-10-08) — RESULT: **BLOCKED**
Sponsor authorized the cert; Phase 1 safety gate PASSED (Account 46 sole active Model-A LIVE instance; only acct 46
monitoring-eligible; 0 LiveExecutionAuthorization; exec/recovery/self-connect flags OFF; MT5_ALLOW_LIVE unset).
Phase 2: enabled `HOSTED_LIVE_MONITORING_ENABLED=1` via beta.env + backend recreate (backup
`beta.env.bak.pre-live-monitoring-acct46`); verified monitoring=True, execution=False, recovery=False,
MT5_ALLOW_LIVE=None. Phase 3 (production observation path, `run_hosted_observations`): **still
`proj_account_match=False`, `canonical_state=WAITING_FOR_LOGIN`.**

**NEW BLOCKER (downstream of the flag):** the per-tenant observer reads **no logged-in account** from Account 46's
terminal — `currently_attached_login=''`, `observed_is_demo=None` — despite the Sponsor's RemoteApp session showing
the LIVE account logged in with an open XAUUSD position. So enabling LIVE monitoring correctly set the EXPECTED
identity to LIVE, but there is **no OBSERVED identity** to match against. (Earlier `proj_connected=True` reflects a
process/IPC-up signal; the account-identity read is empty.) This is an observer/bridge identity-read gap — the
observation layer is not attached to the Sponsor's logged-in LIVE session (consistent with the known acct37
observer bare-launch / `MT5_GUARDED_ATTACH` + IPC-readiness class), NOT the monitoring flag and NOT execution.

**Execution remains impossible** (0 ExecutionJobs; `is_live_execution_authorized=False`; exec/recovery OFF).
**Regression clean** (25/35/36 EXECUTION_READY/unaffected; 33 untouched; no order dispatch; no infra restart).

**Flag left ON** (authorized read-only monitoring; harmless — sole LIVE account, execution impossible; it will
certify automatically once the observer attaches to the correct session). Rollback available any time: remove the
`HOSTED_LIVE_MONITORING_ENABLED` line + recreate backend (image `da4b15e7`).

**Smallest safe next step (READ-ONLY, Sponsor review before any fix):** investigate why the observer reads an empty
attached-login for Account 46's workspace — which terminal/PID it attaches to, whether a stray bare (login-less)
terminal exists alongside the Sponsor's RemoteApp session, and the `MT5_GUARDED_ATTACH`/IPC-readiness state — WITHOUT
restarting or reconnecting the terminal. Then propose the fix. Do not activate execution/recovery/self-connect.

---

## ACCOUNT46_OBSERVER_ROOT_CAUSE (read-only Windows-host forensic, 2026-10-08)
Host `WIN-RD8VDS93DK7`. Read-only only (no launch/kill/attach/reconnect; account funded, open XAUUSD position).

- **Expected (working topology, per demo accts 25/35/36):** terminal `C:\GuvFX\accounts\<id>\terminal\terminal64.exe
  /portable` running headless as `guvfx_u_<id>` in a **DISCONNECTED** RDS session; the per-account observer
  (`Invoke-GuvfxObserver.ps1`) + tenant bridge (guarded `mt5.initialize(path=)`) read `account_info` from it.
- **Actual (acct 46):** terminal **PID 9104** = `C:\GuvFX\accounts\46\terminal\terminal64.exe /portable` as
  `guvfx_u_46` in an **ACTIVE** RDS session **7** (the Sponsor's interactive RemoteApp LIVE login). Observer PID 6564
  (`-TerminalRoot C:\GuvFX\accounts\46\terminal`) and tenant bridge port 8806 (`MT5_GUARDED_ATTACH=1`,
  `MT5_REQUIRE_IDENTITY_PIN=1`, `MT5_ALLOW_LIVE` unset) both target the CORRECT path.
- **No bare/duplicate terminal; observer/bridge target is correct → NOT the acct-37 defect.** (The Administrator
  console terminal PID 10616 is a separate manually-launched IS6 install, unrelated to acct 46.)
- **IPC/account-info failure:** the guarded attach reads `mt5.account_info()` path-only; `MT5_ALLOW_LIVE` gates
  ORDER execution, NOT the read, so it is not the blocker. The bridge returns HTTP 400 on the balance/account-info
  read → `currently_attached_login=''`, `observed_is_demo=None`, `proj_account_match=False`, `WAITING_FOR_LOGIN`.
- **Exact cause (session-topology mismatch):** the headless, never-launch guarded attach reads a terminal in a
  DISCONNECTED/background session (demo topology); Account 46's terminal is held in an **ACTIVE interactive session**
  (the human LIVE login, required to enter live credentials), from which the headless attach cannot read the
  connected account. The monitoring flag is correctly ON; execution remains impossible; allow_live is irrelevant to
  the read. (Mechanism inferred from the strong ACTIVE-vs-DISCONNECTED differential + the guarded-attach read path;
  the bridge does not file-persist a per-call log and I did not probe the terminal/bridge per the do-not list.)
- **Smallest safe remediation (PROPOSAL — not implemented; Sponsor action, zero code):** the Sponsor **DISCONNECTS**
  the Account 46 RemoteApp session — close the RemoteApp window **WITHOUT logging off** — so the terminal keeps
  running + stays broker-connected but its RDS session goes DISCONNECTED (matching 25/35/36). The next 1-minute
  observation cycle should then attach, read the LIVE identity, match, and certify. **Zero risk to the open XAUUSD
  position** (positions are broker-side; a disconnect is not a logoff/close/reconnect). This both validates the root
  cause and is the likely fix.
- **Governed PR?** No for the remediation (operational disconnect + a runbook note: "after the human LIVE login,
  disconnect the RemoteApp, do not log off"). A code change to observe an active interactive session would be a
  separate governed item if desired. **Production restart?** No. **Risk to open positions?** None.

---

## Disconnect-test baseline (2026-10-08) — session-topology hypothesis IN DOUBT
Pre-test read-only baseline: session 7 (`guvfx_u_46`) is now **DISCONNECTED** (was Active during the forensic),
terminal PID 9104 still running — yet GuvFX still shows `proj_connected=True`, `proj_account_match=False`,
`broker_ever_matched=False`, `WAITING_FOR_LOGIN`, `observation_version=3800` (advancing). The working demo accounts
(25/35/36) are ALSO in DISCONNECTED sessions and DO match. **Therefore a DISCONNECTED session does not by itself
produce a match for Account 46 → the ACTIVE-vs-DISCONNECTED hypothesis is NOT supported by current evidence; the
blocker is more likely LIVE-specific** (the headless guarded-attach `account_info` read returning HTTP 400 for the
LIVE account, independent of session state). Not assumed either way — pending (a) Sponsor confirmation that a
deliberate disconnect was performed, (b) a few observer cycles after it, then (c) if still failing, redirect the
root-cause to the LIVE-specific account-info read path (why `account_info()`/`terminal_connected` is empty for the
LIVE terminal while demo terminals read fine).

## PRODUCT REQUIREMENT (recorded per Sponsor)
**LIVE observation must work in BOTH ACTIVE and DISCONNECTED RDS session states.** The observer/bridge headless
guarded attach must reliably read a LIVE account's identity + positions regardless of whether a human RemoteApp
session is attached (ACTIVE) or detached (DISCONNECTED). This is a V1 monitoring robustness requirement for the
TradersWay pilot.

---

## POST-DISCONNECT VERIFICATION (2026-10-08) — RESULT: **LIVE_MONITORING_ACCOUNT46_BLOCKED** (root cause PROVEN)
9-step read-only verification after the Sponsor's confirmed disconnect:
1. Terminal PID **9104 unchanged**. 2. Session 7 **still DISCONNECTED**. 3. Bridge **healthy** (8806 LISTENING,
backend connected). 7. Observer/bridge target correct. 8. After more cycles (obs_version 3800→3825)
`proj_account_match=False`, `broker_ever_matched=False` **unchanged**. 9. 0 trades, no equity. ⇒ **session-topology
hypothesis REFUTED** (disconnected, like the working demos, still fails).

**EXACT failing guard (steps 4-6) — server-name CASE mismatch:** the acct-46 host observer snapshot
(`C:\GuvFX\accounts\46\_obs\observation.json`) is VALID + successful: `observed_login=55442`,
`observed_server="TradersWay-Live"`, `observed_trade_mode=2 (REAL)`, `terminal_connected=true`, `ipc_available=true`,
`ok=true`. So `account_info()` is NOT rejected and returns the full identity. The failure is in the backend matcher
`hosted_workspace/matching.py:96` (`str(obs.server) != str(expected.server)`, **case-sensitive**): observed
`"TradersWay-Live"` vs GuvFX-stored `BrokerServer.server_name="Tradersway-Live"` — `exact-equal=False`,
`casefold-equal=True`. Login (55442) matches; trade_mode REAL == expected LIVE (monitoring ON ⇒ allow_live=True).
**The SOLE blocker is the server-name case.** No LIVE-specific READ guard is involved (allow_live gates execution,
not the read). The dashboard HTTP-400 balance read is a downstream symptom of the unmatched account.

**Data finding:** TWO case-variant LIVE BrokerServer rows exist — `'TradersWay-Live'` (correct, id 96257f08) and
`'Tradersway-Live'` (wrong, id ef0ee702). **Account 46 is bound to the WRONG-case row** (acct 44, tombstoned, is also
on it). (Matches the known "reconcile case-variant TradersWay-Live records" item.)

**Smallest safe remediation (PROPOSAL — not implemented; Sponsor review):**
- **Recommended (code, governed DARK PR + adversarial review):** make the matcher's SERVER comparison
  case-insensitive (casefold + strip) in `matching.py` — login stays EXACT, demo/live env check stays EXACT; MT5
  server names are case-insensitive identifiers, so this is a correct normalization, not a weakening. Fixes acct 46
  regardless of which case-variant row it is bound to, no data mutation, no write-once-binding issue. Backend recreate
  only; no terminal/MT5/broker touch; **zero risk to the open position**.
- **Alternative/follow-up (data-ops, governed):** reconcile the duplicate case-variant BrokerServer rows (retire
  `'Tradersway-Live'` ef0ee702, re-point to the canonical `'TradersWay-Live'` 96257f08) — more involved (write-once
  `broker_server` bindings; dependents acct 44/46); the known reconciliation task.
- **Governed PR?** Yes (either option). **Production restart?** No (backend recreate only). **Risk to open positions?**
  None. **Not implemented** — Sponsor review.
