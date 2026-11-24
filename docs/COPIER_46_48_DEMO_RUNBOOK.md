# #46 → #48 XAUUSD demo runbook — authoritative, deterministic (2026-11-10)

**Status: preparation only. NOT armed.** Executed only under a separate, explicit, Sponsor-present GO. The
live path is DARK and default-OFF. Every step is labelled:

- **[HUMAN]** — the Sponsor performs it (all MT5 actions + all arming/authorization commands).
- **[READ-ONLY AGENT]** — Claude may run it; it mutates nothing (reads/verifies/observes).
- **[PROD-OPERATOR]** — a separately-authorized production operator runs the signed host command.

**Governed route (sole):** master **#46** TradersWay / `Tradersway-Live` (55442, LIVE) → follower **#48** IS6FX /
`IS6Technologies-Live` (51763831, LIVE). Symbol **XAUUSD → XAUUSD.std**. Size **0.01 lot**, **1 order**, **1
position**. Authorization window **≤ 15 min**. Authorities: **MODIFY + FULL_CLOSE only** (no partial, no new
entry after expiry). No credentials appear here.

## Quick-start — one page (GFX-115; what actually has to happen tomorrow)

The next live acceptance needs **Nuno at the keyboard for exactly three things**; Claude/the operator drive
everything else (non-secret). Nuno never pastes a key into chat and never asks Claude to place a trade.

**What Nuno clicks/types (the ONLY human steps):**

1. **[HUMAN]** RDP into the account-46 Windows desktop (`WIN-RD8VDS93DK7`).
2. **[HUMAN]** In an elevated, non-transcribed PowerShell, run the reviewed hidden-input key provisioner and type
   the ingest key **once** at the hidden prompt (confirm phrase `provision-key-account-46`):
   ```
   cd C:\GuvFX\copier-emitter\operator
   .\Provision-GuvfxCopierOneShotKey.ps1
   ```
   Reply only `DONE` (or a redacted one-line `reason`). See `COPIER_ONE_SHOT_ATTESTATION_RUNBOOK.md` Role B2.
   *(Provisioner staged + hash-verified `84e931fa…` under GFX-COPIER-ONESHOT-KEY-PROVISIONING-114; the ingest
   key is `COPIER_INGEST_HMAC_KEY`, held by Nuno.)*
3. **[HUMAN]** During the armed window, perform the explicit **master** MT5 actions in MetaTrader on #46, one at
   a time: **OPEN** 0.01 XAUUSD (with SL+TP) → **MODIFY** (tighten SL) → **FULL CLOSE**. Nothing on #48.

**What Claude / the operator drive (non-secret, no trade, no key):**
preflight (`copier_live_demo_preflight`) · authorization read-back · ARM/DISARM orchestration (ADR-0097, staff
command) · verifier-window sequencing + signed attestation read-back (non-secret) · admission verification ·
per-action latency observation (target **≤1 s**, hard ceiling **≤2 s** acceptance→job — see the synthetic proof
below) · follower read-back / ADR-0096 ticket binding · deadline + abort enforcement · teardown and final
zero-state verification.

**Latency expectation (proven synthetically, no broker):** OPEN/MODIFY/CLOSE each clear well under the **2 s hard
ceiling** from authenticated event acceptance to governed job creation. Evidence: `trade_copier.tests_synthetic_e2e_latency`
(p50/p95/max printed; broker fill is **not** part of this measurement and must never be reported as such).

**Expected success markers (in order):** preflight `verdict=READY_TO_ARM` → authorization `status=ACTIVE` (sole
for the pair) → one `CopierLiveArm` engaged → OPEN → one LIVE `PLACE_ORDER`, `orders_consumed=1`,
`CopierRealtimeDispatch OPEN_SUBMITTED` (`is_late=false`) → ADR-0096 bind `reconciliation_status=MATCHED` →
MODIFY → one `MODIFY_POSITION` → FULL CLOSE → one `CLOSE_TRADE` → DISARM → zero-state (no arm, no in-flight job,
key file absent).

**Maximum waiting periods:** authorization window **≤ 15 min** total; per-action observe **≤ 90 s**; hidden-key
step **≤ 10 min** before the Abort path; latency per action **≤ 2 s** acceptance→job (a slower observation means
the slow reconciler ran — investigate, do not retry).

**Immediate STOP / containment triggers:** wrong account/symbol/side/volume; a second follower order or any extra
job; a PARTIAL_CLOSE on this LIVE route (must never happen); `UNKNOWN_OUTCOME` / `PRE_STATE_MISMATCH` /
`DEADLINE_EXCEEDED`; identity/authorization/expiry drift; any `#48`-initiated action; bridge/observer/watchdog
degradation. **Containment:** `copier_live_arm --disengage …` (or emergency `copier_live_disarm` = kill switch +
disarm-all), `.\Provision-GuvfxCopierOneShotKey.ps1 -Remove`, close the verifier window, restore host admission
OFF (`restore_copier_bridge_execution_off`), verify zero-state. **No automatic retry or late recovery, ever.**

The authoritative, fully-labelled ordered sequence follows below; the quick-start is a timed index into it.

> **ADR-0094 update (real-time + arming corrections).** The 2026-11-12 demo exposed two facts corrected here:
> (1) the ~2-min observer is **reconciliation only**, not the copy trigger — the primary path is now the signed
> ingestion endpoint driving `dispatch_master_realtime` synchronously when `COPIER_IMMEDIATE_DISPATCH_ENABLED`
> is armed (target ≤1s, ≤2s ceiling); and (2) the arming flag stack previously omitted the sub-gates
> **`COPIER_OBSERVER_ENABLED`, `COPIER_REPLICATION_ENABLED`, `COPIER_LIFECYCLE_ENABLED`** (and, for the
> real-time path, `COPIER_EVENT_PROJECTION_ENABLED`, `COPIER_RECONCILE_ENABLED`, `COPIER_IMMEDIATE_DISPATCH_ENABLED`),
> so observe/replicate/lifecycle silently no-op'd. Use the authoritative per-worker stack in
> `docs/COPIER_LIVE_WORKER_SEQUENCE.md`. The governed MODIFY/CLOSE still needs the **route-scoped** follower-read
> authority (`COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED` + an active authorization for #48) — a separate
> Sponsor-gated enablement; until then #48's `/mt5/positions` returns `account_not_demo` and a copied LIVE OPEN
> needs manual close. All of this remains DARK/default-OFF; this runbook stays preparation-only.

> **ADR-0096 update (post-fill follower-ticket binding).** The copied OPEN's follower ticket is now bound
> **event-driven from the OPEN order's fill result** at job completion (`bind_open_result`), with **no
> `/mt5/positions` read**. So for a **position-clean** follower (this route's precondition) the governed MODIFY
> (step 11) and FULL_CLOSE (step 13) can fire **without** the route-scoped follower-read authority
> (`COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED`) — the positions-read reconcile is now the FALLBACK, used only
> when the OPEN result carries no ticket (UNKNOWN). At step 9, confirm the follower ticket bound from the OPEN
> result (`reconciliation_status=MATCHED`, `status=EXECUTION_VERIFIED`). The result reports the opening ORDER
> ticket; for a position-clean fresh open it equals the position ticket the mutations resolve by (a NETTING
> follower with a pre-existing same-symbol position would diverge and fail CLOSED at MODIFY/CLOSE — the route's
> position-clean precondition forecloses this). Still DARK/default-OFF; preparation-only. See the end-to-end
> source + reconcile readiness in `docs/COPIER_REALTIME_SOURCE_ACTIVATION.md`.

> **ADR-0097 update (one-action ARM/DISARM — replaces the multi-flag/worker ceremony).** Step 4's ~15-flag +
> 7-worker arming is superseded by a SINGLE audited command: **[HUMAN/STAFF]** `copier_live_arm
> --authorization-id <N> --by <STAFF_EMAIL> --reason "..."` creates one authorization-bound, auto-expiring
> `CopierLiveArm` (capped at the authorization expiry) after re-running the backend preflight fail-closed. The
> LIVE admission now requires that engaged, route-exact arm as an ADDITIONAL AND — the base `COPIER_*` gates stay
> deployment config and do nothing without an arm. The real-time pipeline (immediate dispatch + ADR-0096 post-fill
> binding) drives OPEN/MODIFY/CLOSE event-driven, so **no worker is re-run between trade actions**. Return-to-OFF
> is one command: **[HUMAN/STAFF]** `copier_live_arm --disengage --authorization-id <N> --by <STAFF_EMAIL>
> --reason "demo complete"` (does NOT engage the durable kill switch). Backend DISARM does NOT restore Windows
> host admission — the signed `restore_copier_bridge_execution_off` remains a separate operator step. The
> emergency `copier_live_disarm` engages the kill switch AND disengages all arms. Still DARK/default-OFF. See ADR-0097.

## Timing / containment limits
- Authorization window ≤ 15 min; disarm immediately after step 13 regardless of time left.
- Per-step observe wait: **≤ 90 s** after each human MT5 action (observer cadence is 2 min — run the live-path
  workers on demand immediately after each action rather than waiting for a cron).
- Containment triggers: wrong account/symbol/side/volume; duplicate follower position; UNKNOWN_OUTCOME /
  PRE_STATE_MISMATCH / DEADLINE_EXCEEDED; identity/authorization/expiry drift; bridge/observer/watchdog
  degradation; any extra job/order. **No automatic retry or late recovery is ever permitted.**

## Authoritative ordered sequence
1. **[READ-ONLY AGENT]** `copier_live_demo_preflight --master-account-id 46 --follower-account-id 48 --source-symbol XAUUSD --follower-symbol XAUUSD.std` → require `verdict=READY_TO_ARM` for the backend gates; resolve every `HOST_PROBE_REQUIRED` item out of band ([PROD-OPERATOR] confirms bridge/watchdog health, host admission absent, #48 position-clean).
2. **[HUMAN]** Create a fresh 15-min authorization (revoke any stale ACTIVE row first — see step 16 command):
   `grant_copier_live_authorization --user-email support@guvfx.com --master-account-id 46 --follower-account-id 48 --source-symbol XAUUSD --follower-symbol XAUUSD.std --max-volume 0.01 --max-concurrent 1 --max-orders 1 --valid-minutes 15 --approved-by <STAFF_EMAIL> --reason "..." --modify-authority --full-close-authority`
3. **[READ-ONLY AGENT]** Independent DB read-back: exact identities/scope/window/watermark/authorities, staff approver, `status=ACTIVE`, unexpired, sole ACTIVE for the pair.
4. **[HUMAN/STAFF]** Backend ARM (ADR-0097) — one audited command: `copier_live_arm --authorization-id <N>
   --by <STAFF_EMAIL> --reason "..."`. It re-runs the backend preflight fail-closed and creates one
   authorization-bound, auto-expiring `CopierLiveArm` (capped at the authorization expiry). With the base
   `COPIER_*` gates set as deployment config, this single arm is the additional AND that enables the LIVE path;
   the real-time pipeline (immediate dispatch + ADR-0096 post-fill binding) then drives OPEN/MODIFY/CLOSE
   event-driven — no worker is re-run between trade actions. (The legacy per-worker stack in
   `COPIER_LIVE_WORKER_SEQUENCE.md` is superseded for arming.)
5. **[PROD-OPERATOR]** Signed host-admission **dry-run** (with the flags injected): `provision_authorized_live_copier_bridge --account-id 48 --authorization-id <N>` → require `gates_ok=true`, `env_will_be_live=true`.
6. **[PROD-OPERATOR]** Signed host-admission **commit**: same command **+ `--commit`** → sets `MT5_ALLOW_LIVE` + exact-account/route/symbol/time admission env on #48's bridge + restart.
7. **[READ-ONLY AGENT]** Read-back the exact #46→#48 XAUUSD boundary: authorization ACTIVE+unexpired; the 5 gates TRUE; host admission exact-account/route/symbol/time-pinned; **0 jobs; 0 broker tickets**; kill switch clear; sole route.
8. **[HUMAN]** Open exactly **one 0.01 XAUUSD** position on #46 (SL/TP optional).
9. **[READ-ONLY AGENT]** Observe exactly **one** follower OPEN on #48: master/follower tickets, volume, latency, deviation. On any trigger → containment (step 14).
10. **[HUMAN]** Add or change SL/TP on #46.
11. **[READ-ONLY AGENT]** Observe the follower MODIFY on #48: master/follower revision, latency, resulting SL/TP, any broker normalization (report it exactly — never claim equality when values differ).
12. **[HUMAN]** Close the #46 master position.
13. **[READ-ONLY AGENT]** Observe exactly one follower FULL_CLOSE on #48; both flat; no residual; no pending mutation; no retry scheduled.
14. **[HUMAN/STAFF]** Immediate backend return to OFF (ADR-0097): `copier_live_arm --disengage --authorization-id <N> --by <STAFF_EMAIL> --reason "demo complete"` (does NOT engage the kill switch). This makes the arm-dependent gates false at once; it does NOT restore Windows host admission (step 15).
15. **[PROD-OPERATOR]** Signed host-admission OFF: `restore_copier_bridge_execution_off --account-id 48` (dry-run) then `--commit` → removes `MT5_ALLOW_LIVE` + admission env, restarts #48's bridge to DARK.
16. **[HUMAN]** Revoke the authorization: `revoke_copier_live_authorization --authorization-id <N> --owner-email support@guvfx.com --master-account-id 46 --follower-account-id 48 --revoked-by <STAFF_EMAIL> --reason "demo complete"` (or `reconcile_expired_copier_authorizations --commit` for an expired one).
17. **[READ-ONLY AGENT]** Final OFF read-back: all copier flags OFF; host admission absent (bridge DARK); 0 ACTIVE authorizations; `LiveExecutionAuthorization` 0; kill switch clear; 0 queued/running copier jobs; no residual #48 position; route monitoring-only; sibling accounts/routes unchanged.

## Failure classifications
`OPEN_REJECTED/TIMEOUT` → record FAILED, **never place later** (position abandoned); master-open/follower-not is an accepted asymmetric outcome. `MODIFY_UNKNOWN` → record, no retry without a separate human decision. `CLOSE_FAILED/TIMEOUT` → alert + **[HUMAN] manual close of #48 in MT5** (contingency). `IDENTITY/ROUTE/SYMBOL/EXPIRY drift` / `GUARD/CONTROL missing` → admission fails closed, no broker contact, record.

## Containment (any step)
[HUMAN] set backend flags OFF → [PROD-OPERATOR] `restore_copier_bridge_execution_off --commit` → [HUMAN] manually close any #48 position in MT5 → [HUMAN] revoke the authorization → capture state. The kill switch (`copier_live_disarm`) is engaged only if separately authorized for containment. **The system never re-opens or late-reconciles a failed/late entry** (transport one-shot; admission time-bound + fail-closed on restart/expiry).

**No step here is armed or executed by Claude. Claude's role is read-only preflight, read-back verification, and observation only.**

## Real-arm prerequisites (GFX-167 — verified read-only 2026-11-24)

Three preconditions, each proven value-blind in the GFX-167 real-arm readiness preflight, must hold **before**
opening the INGEST window / arming (ADR-0114 below). None weakens any existing gate — each closes an operational
hazard the programme already paid for (pkt-162). With all three met the backend control plane and host are
otherwise DARK-and-ready: identities route-exact (#46 Tradersway-Live → #48 IS6Technologies-Live, both LIVE,
same owner), symbol map XAUUSD→XAUUSD.std, sole route, every copier flag OFF, 0 authorizations/arms/windows/
dispatch, kill switch clear, #46 clean, teardown command installed.

1. **Verifier-window key lifecycle — deploy GFX-164, or clear-before-open.** The GFX-164 fix
   (`verifier_window.open_window` writes the key ITSELF, after reap + single-flight, to a per-window-UNIQUE
   tmpfs path) is merged to `main` (squash `97d21765`) but may be **undeployed** — production was `f047cf5e`
   (exactly one commit short of `97d21765`) on 2026-11-24. On a **pre-GFX-164** backend the operator command
   pre-writes the key to a FIXED default path, so opening a fresh INGEST window can reap an expired prior window
   sharing that path and shred the just-written key → an OPEN but **keyless** window. The deployed arm gate
   (`verifier_window.covers_live_authorization`, `key_file_present`) fails closed on this
   (`VERIFIER_WINDOW_NOT_READY`) so a keyless window can never strand a position — but it DOES refuse the arm.
   Therefore, before the real arm EITHER **(a)** deploy `97d21765` (migration-free, DARK) — the structural fix,
   preferred; OR **(b)** on a pre-GFX-164 backend, run `copier_verifier_window --close` immediately before
   opening the INGEST window (clears the default path + any stale window), then open exactly one uncapped window.
   Confirm `copier_verifier_window --status` shows the fresh window with `key_file_present=true` before arming.

2. **Open the INGEST window only AFTER the final backend container recreate.** The window's key lives on
   `/dev/shm` (tmpfs/RAM) and is erased by any `guvfx-backend` recreate/restart. Perform ALL deployment-config
   flag arming (which recreates the container) and any backend deploy **before** opening the window, and never
   recreate/restart `guvfx-backend` between opening the window and teardown — a recreate silently blanks the key
   and 503s the follower CLOSE mid-lifecycle. (The atomic arm is a DB-only management command and does not
   recreate the container, so the arm itself is safe after the window is open.)

3. **Hosted-executor keyring currency (pkt-166C).** The signed backend↔daemon channel must be current, or the
   daemon rejects the executor signature. On `WIN-RD8VDS93DK7` confirm value-blind: `daemon_config.py` is the
   file-first loader (SHA-256 `a4193d0d`), the keyring file `C:\GuvFX\hosted\secrets\hosted_executor_keyring.json`
   is present with ACL {SYSTEM FullControl, Administrators FullControl, `NT SERVICE\GuvFXHostedExecutor` Read} and
   active id `hx-20261115` == backend `HOSTED_EXECUTOR_KEY_ID`, service Running. A pre-file-first daemon, or one
   whose env was cached at a boot predating the key rotation, would be stale (see pkt-166/166B/166C).

**Teardown completeness (what `teardown_copier_live_dark` does NOT do).** The teardown command returns the
backend (disarm + revoke) and host (admission OFF + disable `GuvFX_CopierEmitter_46` + shred the host ingest key)
to DARK, but it does NOT close the verifier window or clear the base ingestion/immediate-dispatch deployment
flags. Teardown therefore ALSO requires, as explicit operator steps: `copier_verifier_window --close` and
restoring the base `COPIER_*` ingestion/immediate-dispatch flags OFF. The final OFF read-back (step 17 /
`copier_live_demo_preflight`) must show **0 open verifier windows** and **every copier flag OFF**.

## Corrected atomic arming (pkt-148, ADR-0114)

pkt-147 found two gaps the manual sequence above could not prevent: the standing emitter had **no ingest key**
(the <=1s path could not run), and `provision_authorized_live_copier_bridge` arms OPEN without proving the
**MODIFY/FULL_CLOSE execution flags** are on — a copied OPEN could strand a real #48 position. ADR-0114 adds a
single fail-closed path; prefer it over hand-sequencing the steps above.

1. **[HUMAN — single entry]** Provision the standing emitter key AND open the backend INGEST verifier window
   from ONE hidden key entry (does not enable/trigger the task, a flag, an authorization or an arm). Run the
   reviewed wrapper in the operator's own console:
   `deploy/copier-emitter/operator/provision_and_open_ingest.py --windows-ssh <admin@host> --backend-ssh <ubuntu@vps>`.
   It (a) **PREFLIGHTS before prompting** — refuses if a verifier window is already open, if a present-and-valid
   standing key already exists, or if either state is unreadable (ambiguous ⇒ refuse); (b) reads the account-46
   ingest key **ONCE, hidden (getpass), re-entered to confirm**; (c) delivers the **identical bytes over ssh STDIN**
   to BOTH the host provisioner `Provision-GuvfxCopierEmitterKey.ps1 -KeyFromStdin` (account-46-pinned, refuses
   #48/Customer Zero; then value-blind `-Verify` must pass — path/ACL/tenant-read-only/task-DISABLED, never reads
   the value; writes `C:\GuvFX\copier-emitter\46\ingest.key`, which `run_emitter.py` auto-loads at startup) AND the
   backend `copier_verifier_window --open --purpose ingest --max-uses 0 --ttl <=900` (**UNCAPPED** — the arm refuses
   a finite cap because every ingested OPEN/MODIFY/CLOSE/heartbeat burns one use, so a capped window would lapse
   mid-lifecycle and 503 the follower CLOSE). On **SUCCESS it deliberately LEAVES both** the standing host key and
   the OPEN INGEST window in place for the arm (step 4). On **any partial failure / exception / interrupt** it closes
   the window and shreds the host key (`-Remove`), then **INDEPENDENTLY verifies** zero open windows + host key absent,
   reporting `CLEANUP_INCOMPLETE` with exact operator remediation if it cannot prove a DARK result (never a false
   rollback). The key never reaches argv/env/logs/output. Removal at teardown remains the separately-governed step 5
   (`Provision-GuvfxCopierEmitterKey.ps1 -Remove` + `copier_verifier_window --close`). *(Distinct from the one-shot
   ATTEST wrapper `provision_and_open_oneshot.py`, which opens a capped ATTEST window and tears it down after one
   verification.)*
2. **[READ-ONLY]** `copier_live_demo_preflight` — confirm the new `arm_readiness` section's blockers and
   `arm_ready` (fail-closed: `EMITTER_KEY_ABSENT`, `FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED`, `HOST_ADMISSION_NOT_OFF`,
   `EMITTER_TASK_NOT_DISABLED`, `OPEN_WITHOUT_CLOSE_REFUSED`, `MODIFY/FULL_CLOSE_EXECUTION_NOT_ARMABLE`,
   `LIFECYCLE_AUTHORITY_FLAG_MISMATCH`, `TEARDOWN_NOT_PROVEN`).
3. **[HUMAN]** Visually confirm #46 and #48 flat.
4. **[STAFF]** Dry-run the atomic arm (no mutation):
   `arm_copier_live_lifecycle --master-account-id 46 --follower-account-id 48 --source-symbol XAUUSD
   --follower-symbol XAUUSD.std --authorization-id <N> --by <STAFF> --reason "demo"
   --follower-flat-confirmed --emitter-key-ready --host-admission-off --emitter-task-disabled`. It refuses unless
   the complete OPEN+MODIFY+FULL_CLOSE authority AND execution flags agree, partial-close is excluded, caps hold,
   #46 is clean, and every host/human gate is attested. Then `--commit` performs, atomically:
   provision admission -> enable emitter -> create the arm (arm LAST), each read-back-verified, with **full
   rollback to DARK on any failure** (no partial arm, no stranded position).
5. **[STAFF]** Teardown (idempotent, any state): `teardown_copier_live_dark --master-account-id 46
   --follower-account-id 48 --authorization-id <N> --by <STAFF> --commit` — disarm, revoke, signed host-admission
   OFF; disable the emitter task + `Provision-GuvfxCopierEmitterKey.ps1 -Remove` are the host steps. Reports a
   machine-readable DARK verdict; host steps without a signed actuator are UNPROVEN, never "done".

**Still DARK / repository-only:** no agent arms, enables, provisions, or trades. A real run wires signed-host
actuators and is separately Sponsor-gated.

---

## Optional pre-flight: NON-TRADING one-shot INGEST-signing probe (GFX-160, ADR-0119)

A cheap, repeatable, **zero-trading** way to prove the ingest-authentication chain (standing-key signing ->
transport -> backend HMAC auth -> bounded-window key resolution -> single-use nonce / replay) **before** the real
OPEN/MODIFY/CLOSE demo. It exercises **no** MT5 state, master action, projection, replication, dispatch, admission,
arm, follower (#48) or order, and it **cannot** become an OPEN/MODIFY/CLOSE — so a green probe means only "the
key/window/transport/replay chain is sound," never "copy trading works." It is **not** a substitute for the real
E2E demo (steps 1-5 above).

Pre-req: the standing account-46 host key is present and ACL-verified (pkt-159). Default-OFF; the endpoint 503s
until armed.

1. **[HUMAN — fresh window]** Open a fresh bounded INGEST window with the standing-key bytes
   (`deploy/copier-emitter/operator/provision_and_open_ingest.py`, one hidden entry; TTL <= 900). A probe needs an
   OPEN INGEST window; if the pkt-159 window has expired, open a new one (the standing host key persists, so this
   re-opens only the window).
2. **[STAFF]** Arm the probe gate at deploy-time config: `COPIER_ENABLED`, `COPIER_EVENT_INGESTION_ENABLED` and
   `COPIER_INGEST_PROBE_ENABLED` on (the probe flag ANDs the first two). No authorization, arm, task, worker or
   host-admission is enabled.
3. **[OPERATOR — one-shot, on the host]** Run the probe once (reads the standing key locally; never argv/env/stdin):
   `python run_ingest_probe.py --config C:\GuvFX\copier-emitter\46\config.json --replay`
   It signs ONE non-trading envelope, POSTs once (expect `verified`), then (with `--replay`) re-POSTs the SAME
   envelope once to prove **replay rejection** (expect `replayed`). Output is sanitised JSON (verified / reason /
   correlation id / timestamp / attempts) — never the key or envelope. The standing key file is **retained**.
4. **[READ-ONLY]** Confirm value-blind: backend logged `copier_ingest_probe_verify verified=true`; exactly one
   `CopierIngestNonce` burned for the probe nonce; `copier_verifier_window --status` still `open` with `uses`
   incremented (uncapped window, not exhausted); **no** `MasterEvent` / lifecycle / `ExecutionJob` / authorization
   / arm / `LiveExecutionAuthorization` created; #48 untouched.
5. **[STAFF]** Close the window (`copier_verifier_window --close`) and restore the probe/ingestion flags OFF
   (back to DARK). **Keep the verified standing host key in place** (removal is the separately-governed teardown
   step, not part of the probe).

**Notes (safety):**

- **The INGEST window MUST be uncapped** (`--max-uses 0`, TTL-bounded only — exactly what `provision_and_open_ingest.py`
  opens). The verifier resolves the key by *claiming* it before the signature is checked (you need the key to verify
  the HMAC), so every request to the `AllowAny` endpoint — including the `--replay` attempt and any garbage — burns one
  `uses` increment. On a **use-capped** window that would exhaust and close the window mid-probe; on the uncapped
  window it is harmless. The endpoint is rate-limited (`copier_ingest_probe` throttle) and fails closed, but run the
  probe only under an uncapped window.
- **Governance coupling (PM):** `COPIER_INGEST_PROBE_ENABLED` ANDs `COPIER_EVENT_INGESTION_ENABLED`, and the probe
  shares the one `INGEST` verifier window + the one `CopierIngestNonce` space with real ingestion. So while the probe
  is armed, the real signed `/event-ingest/` endpoint is also live (itself persist-only / non-trading unless
  `COPIER_IMMEDIATE_DISPATCH_ENABLED` is *separately* armed). A "non-trading probe window" is therefore simultaneously
  a live real-ingestion window — close it (step 5) as soon as the probe is done.

**Still DARK / repository-only:** no agent arms, enables, provisions, or trades; the probe is inert until a
Sponsor arms the flag and opens a window.
