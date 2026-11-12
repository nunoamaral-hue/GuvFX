# Copy Trading real-time SOURCE + post-fill reconcile — activation readiness (ADR-0077 + ADR-0094 + ADR-0096)

**Status: preparation only. NOT installed, NOT armed, NOT deployed.** This runbook documents the full
real-time replication chain, the single operational hole at its head (no master-side source on #46), and the
ordered, separately-Sponsor-gated steps that close it. Every mutating step is **[HUMAN]** or **[PROD-OPERATOR]**;
Claude's role is **[READ-ONLY]** readiness and verification only. Nothing here places, modifies or closes a
broker order, provisions a secret, or enables a flag. Packet: GFX-COPIER-REALTIME-SOURCE-AND-RECONCILE-073.

## The chain and its single break

```
#46 MT5 event  →  ADR-0077 emitter (signs via event_ingestion)  →  POST /api/copy-trading/event-ingest/
   (signed, per-tenant)         [THE MISSING SOURCE — bundle exists, NOT installed/armed on the host]
→ SignedEventIngestView.verify + ingest_revisions (immutable CopierIngestedRevision)
→ ADR-0094 COPIER_IMMEDIATE_DISPATCH_ENABLED → dispatch_master_realtime (project → replicate → execute …)
→ follower OPEN PLACE_ORDER job → bridge fill → ExecutionJobViewSet.complete (result["ticket"])
→ ADR-0096 bind_open_result → CopierReplication.follower_ticket MATCHED / EXECUTION_VERIFIED
→ governed automatic MODIFY → governed automatic FULL_CLOSE (both target the bound follower ticket)
```

Measured on production (072 readiness): the backend fast path is deployed DARK and correct, but
`ingested_revisions_total = 0` on #46 — **every** master event is still POLL-sourced because the emitter is not
running. ADR-0096 closes the *downstream* break (the follower ticket can now bind from the OPEN fill result
with no `/mt5/positions` read). The *upstream* break (no source) is closed **operationally**, not by code: the
emitter bundle is reusable as-is.

## The emitter bundle (ADR-0077) — repository artifact, reusable as-is

- **Location (repo):** `deploy/copier-emitter/` — `run_emitter.py`, the four ASCII-only PowerShell scripts
  (`Install-`, `Uninstall-`, `Status-`, `Attest-GuvfxCopierEmitter.ps1`), `emitter_task_template.xml`,
  `config.template.json`, `README.md`, and `MANIFEST.json`.
- **Source commit:** stamped into `MANIFEST.json.source_sha` at bundle-build time (placeholder
  `STAMP_AT_PACKAGE_TIME` in the repo); `lib/` is vendored from the pinned `MANIFEST.json.lib_subset` (the
  Django-free emitter modules + `event_ingestion.py`). Python 3.11+ stdlib only, no third-party dependency.
- **File inventory + SHA-256:** the authoritative per-file hashes are in `deploy/copier-emitter/MANIFEST.json`
  (`files` map) — the single source of truth; do not transcribe them here (a copy would drift). Verify the
  on-host bundle against that manifest before any install.
- **Envelope contract (proven):** the emitter signs with `trade_copier.event_ingestion.sign_ingest_request` —
  the SAME module the backend verifies with — so the envelope is byte-for-byte compatible. Pinned by
  `backend/trade_copier/tests_emitter_ingest_contract.py` for the exact #46 / 55442 / `Tradersway-Live`
  OPEN→MODIFY→CLOSE lifecycle: a known-good envelope verifies; retargeting the signed account 46→48, tampering
  the payload, a bad signature, an unknown key id, or a replayed nonce all fail closed.

### #46-specific config (fill before install — NO secret in the file)

| Field | Value for #46 | Note |
|-------|---------------|------|
| `account_id` | `46` | reserved ids ≤ 1 are refused by the installer |
| `broker_login` | `55442` | TradersWay |
| `broker_server` | `Tradersway-Live` | LIVE |
| `bridge_base_url` | #46's READ-ONLY bridge origin (host port 8806) | positions/snapshots read only |
| `ingest_url` | `https://api.guvfx.com/api/copy-trading/event-ingest/` | the backend signed endpoint |
| `key_id` | `copier-ingest` | keyring id the backend verifies against |
| `ingest_key_env` | `COPIER_INGEST_HMAC_KEY` | **env-var NAME only**; the secret itself is injected out of band |
| gates | `attestation_allowed` / `observation_allowed` / `delivery_allowed` = **false** | a malformed config evaluates OFF |

- **Install dir (host):** `C:\GuvFX\copier-emitter\` (spool at `C:\GuvFX\copier-emitter\spool\`).
- **Task name:** `GuvFX_CopierEmitter_46` — registered **DISABLED**, no trigger, no watchdog, no auto-restart.
- **Health probe:** `Status-GuvfxCopierEmitter.ps1` (read-only task-state). **Attestation:**
  `Attest-GuvfxCopierEmitter.ps1` (one-shot identity proof: build/host/account/broker/terminal/bridge — reads
  NO positions, runs NO loop). **Uninstall:** `Uninstall-GuvfxCopierEmitter.ps1` (idempotent, account-scoped,
  task-only, reversible).

### Why installing the emitter alone CANNOT cause a broker order

1. The emitter adapter observes the master's own positions through the **read-only** bridge and emits SIGNED
   observations — **it has no place/modify/close method**.
2. Every emitter gate (`attestation_allowed`, `observation_allowed`, `delivery_allowed`) defaults OFF; a
   malformed config evaluates OFF; the task is installed DISABLED and never self-triggers.
3. Even with the emitter delivering, the backend fast path stays DARK until `COPIER_IMMEDIATE_DISPATCH_ENABLED`
   **and** the full per-worker execute stack are armed **and** a human `CopierLiveAuthorization` **and** the
   signed host admission on the follower are present (see `COPIER_LIVE_WORKER_SEQUENCE.md` and
   `COPIER_46_48_DEMO_RUNBOOK.md`). The emitter is a SOURCE of signed observations, never an execution path.

## Ordered activation gates (each SEPARATE, Sponsor-gated; none performed here)

1. **[READ-ONLY]** Verify the on-host bundle against `MANIFEST.json`; `ParseFile`-validate all four PS1 scripts
   on the target Windows PowerShell (security RULE 9/11) before first run.
2. **[PROD-OPERATOR]** Provision the ingestion HMAC secret into `COPIER_INGEST_HMAC_KEY` on the host out of band
   (min 32 chars; fail-closed if unset) and the matching keyring entry (`key_id = copier-ingest`) on the
   backend. The secret appears in neither the repo, the config file, nor logs (RULE 1 security).
3. **[PROD-OPERATOR]** `Attest-GuvfxCopierEmitter.ps1` one-shot → require identity match (build/host/account/
   broker/terminal/bridge). No positions read, no loop.
4. **[PROD-OPERATOR]** `Install-GuvfxCopierEmitter.ps1` (dry-run default; then `-Apply`) → registers
   `GuvFX_CopierEmitter_46` **DISABLED**.
5. **[HUMAN]** Arm the backend fast path: `COPIER_IMMEDIATE_DISPATCH_ENABLED` + the per-worker execute stack
   (authoritative list in `COPIER_LIVE_WORKER_SEQUENCE.md`), a fresh ≤15-min `CopierLiveAuthorization`, and the
   signed host admission on #48 (`COPIER_46_48_DEMO_RUNBOOK.md`).
6. **[HUMAN]** Enable the emitter task + its `delivery_allowed` gate.
7. **[READ-ONLY]** Confirm the chain is **event-sourced, not observer-sourced** — see below.

## Acceptance: wait for the EVENT, not the slow observer

The 2026-11-12 demo failed because the ~2-min observer, not the push event, was treated as the trigger. Once
armed, acceptance is keyed on the PUSH path:

- **Source-of-truth check:** after a master action on #46, the follower OPEN must be driven by an **ingested**
  revision — `CopierIngestedRevision` count increments and the resulting `MasterEvent` is push-sourced — and a
  `CopierRealtimeDispatch` row shows source→submission ≤ 2000 ms (target ≤ 1000 ms). Do **not** wait for, or
  accept, a reconciliation-only detection as real-time success (ADR-0094).
- **Binding check (ADR-0096):** the follower OPEN `PLACE_ORDER` completes with a ticket, and
  `CopierReplication.follower_ticket` becomes non-null with `reconciliation_status = MATCHED` /
  `status = EXECUTION_VERIFIED` **from the OPEN result** — no `/mt5/positions` read required. The governed
  MODIFY and FULL-CLOSE then target the bound ticket. (For a position-clean follower — the route precondition —
  the result's order ticket equals the position ticket; see ADR-0096's netting limitation.)
- **Follower-read is now OPTIONAL for the common path.** `COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED` is no
  longer required for MODIFY/CLOSE when the OPEN result carries the ticket; it remains the fallback for an
  UNKNOWN (no-ticket) OPEN and for read-only follower monitoring.

## Containment / OFF

Disarm is the demo runbook's return-to-OFF (`COPIER_46_48_DEMO_RUNBOOK.md` steps 14–17) plus: **[HUMAN]**
disable the emitter task (`delivery_allowed` OFF or `schtasks /change /disable`), and — to remove it entirely —
**[PROD-OPERATOR]** `Uninstall-GuvfxCopierEmitter.ps1`. The emitter holds no execution authority, so disabling
it stops the SOURCE; execution OFF is governed separately by the backend flags + host admission.

**No step here is armed or executed by Claude. Claude's role is read-only readiness and verification only.**
