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
