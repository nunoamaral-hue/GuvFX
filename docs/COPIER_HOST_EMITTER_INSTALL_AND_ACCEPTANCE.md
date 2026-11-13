# Copy Trading host emitter — install, attestation, synthetic acceptance & rollback (ADR-0077)

Packet GFX-COPIER-HOST-EMITTER-BUILD-006. **Repository-only / DARK. Prepared, NOT executed.** Every host step
below is gated on a separate, explicit Sponsor/operator authorization. Nothing here is run by this packet: no
Windows host access, no credential provisioning, no service installation, no enablement, no broker contact.

## 0. What ships (repository artifacts)
- Emitter application (Django-free): `backend/trade_copier/emitter/{config,mt5_adapter,attestation,app}.py`.
- Backend non-lifecycle attestation verifier: `backend/trade_copier/attestation_verify.py` +
  `attestation_api.py` + flag `COPIER_ATTESTATION_VERIFY_ENABLED` (default OFF) + route
  `POST /api/copy-trading/attest-verify/`.
- Windows bundle: `deploy/copier-emitter/` (launcher, Install/Uninstall/Status/Attest `.ps1`, disabled task
  template, secret-free config template, `MANIFEST.json`).

## 1. Packaging (bundle-build step — separate gate)
1. Check out the pinned build commit; stamp `MANIFEST.json.source_sha`.
2. Populate `deploy/copier-emitter/lib/` with the pinned source subset in `MANIFEST.json.lib_subset` (the
   Django-free modules under `lib/trade_copier/...`). The emitter needs **Python 3.11+ stdlib only** — no pip
   dependency.
3. Verify every file hash matches `MANIFEST.json.files` and every `.ps1` passes
   `[System.Management.Automation.Language.Parser]::ParseFile()` on the TARGET Windows PowerShell (RULE 9/11) —
   ASCII-only, no BOM.
4. Provision the emitter's authentication material (the `COPIER_INGEST_HMAC_KEY`) **out of band**, least-
   privilege, referenced by env-var NAME in the config — never on a command line, in the config, in logs or in
   Git. Record only its fingerprint.

## 2. Install (host step — separate gate; dry-run first)
```
powershell -File Install-GuvfxCopierEmitter.ps1 -AccountId <id> -ConfigPath <cfg> -EntryPoint <run_emitter.py>          # PLAN (dry-run)
powershell -File Install-GuvfxCopierEmitter.ps1 -AccountId <id> -ConfigPath <cfg> -EntryPoint <run_emitter.py> -Apply   # register DISABLED
```
The task `GuvFX_CopierEmitter_<id>` is created **disabled**, with no trigger, no watchdog and no auto-restart.
Installation leaves it **stopped**. A first start is a later, explicit operator action — never automatic.

## 3. One-shot identity attestation (host step — separate gate)
```
powershell -File Attest-GuvfxCopierEmitter.ps1 -AccountId <id> -ConfigPath <cfg> -EntryPoint <run_emitter.py>
```
Requires `attestation_allowed: true` in the config. It reads ONLY account identity (`/mt5/snapshots/account`),
NEVER positions; it builds + self-verifies a signed attestation envelope and emits it as JSON (with a key
fingerprint, never the secret). Verify it against the backend by arming `COPIER_ATTESTATION_VERIFY_ENABLED` and
POSTing to `/api/copy-trading/attest-verify/`. Attestation creates no lifecycle state, ExecutionJob, alert or
broker activity.

## 4. Synthetic / inert acceptance (CI + repo; no host, no broker, no real MT5)
Run the synthetic suites; they use fake identities + fake MT5/bridge responses and block the network:
- `trade_copier.emitter.tests_config` — gates default OFF, malformed→OFF, no-start without complete identity.
- `trade_copier.emitter.tests_adapter` — read-only (no order surface), missing SL/TP preserved, open_time, reason codes.
- `trade_copier.emitter.tests_attestation` — positive + full negative-control matrix.
- `trade_copier.emitter.tests_app` — OPEN/SL/TP/partial/full-close detection, unchanged→nothing, restart/idempotency, bounded spool, gates inert.
- `trade_copier.emitter.tests_e2e` — emitter → ingest → projection → lifecycle: stale OPEN → ENTRY_FAILED_FINAL (no late entry), ZERO ExecutionJob.
- `trade_copier.tests_attestation_verify` — backend verify: gate-off 503, identity/replay negatives, no lifecycle/job.
- `trade_copier.emitter.tests_bundle` — manifest hashes, ASCII/ParseFile, disabled defaults, no order verb, no secret.
- `trade_copier.emitter.tests_capacity` — per-stage + e2e latency evidence (one account).

Acceptance PASS requires every applicable check to run and meet its criterion; a check that cannot run is
recorded PARTIAL/FAIL with the reason. No real account/position/broker order is used.

## 5. Rollback
- **Uninstall (reversible, account-scoped):**
  `powershell -File Uninstall-GuvfxCopierEmitter.ps1 -AccountId <id> -Apply` — ends + removes ONLY
  `GuvFX_CopierEmitter_<id>`; never another account's unit, never a broad recursive delete, never by image name.
- Remove any provisioned authentication material out of band; confirm no emitter process or listener remains;
  reconfirm all copier/execution flags OFF. The backend verifier is disabled by clearing
  `COPIER_ATTESTATION_VERIFY_ENABLED` (no migration, no schema change — nothing to roll back DB-side; this
  packet adds no model/migration).

## 5a. One-shot emitter bundle rollout — byte-recoverable rollback (ADR-0104)

Scope: the separately Sponsor-gated Windows-host rollout of the account-46 one-shot attest bundle (the emitter
bundle + hosted-executor daemon update + the DISABLED, triggerless `GuvFX_CopierEmitterOneShot_46` task). Governs
packet `GFX-COPIER-WINDOWS-EMITTER-ROLLOUT-PREFLIGHT-100` and its successors. The installer itself is fixed under
`GFX-COPIER-ONESHOT-INSTALLER-NATIVE-SCHTASKS-FIX-103` (the PS 5.1 absent-task native-stderr defect that aborted the
first rollout's PLAN; see `deploy/copier-emitter/Install-GuvfxCopierEmitterOneShotTask.ps1` and its real-schtasks
regression).

**Lesson paid for (packet 100 residual).** The first rollout replaced the on-host account-46 bundle manifest
(`...\46\MANIFEST.json`) **without backing up the prior file first**, so that one file was not byte-recoverable on
rollback. It was non-runtime and harmless in that instance, but the rollback set was therefore incomplete. The next
rollout MUST close this gap:

1. **Back up every file before it is replaced.** Before overwriting any pre-existing on-host file -- the daemon files,
   the account-46 bundle `MANIFEST.json` included -- copy the current file into the rollback directory
   (`C:\GuvFX\_rollback\<rollout-id>\`), preserving its relative path. No pre-existing file is replaced before its
   backup copy exists.
2. **Record each backed-up file's SHA-256 and ACL** (owner + DACL, e.g. `Get-Acl | ConvertTo-...`/`icacls ... /save`)
   in a rollback inventory alongside the copies, so a restore can verify byte- and permission-fidelity.
3. **Restore every backed-up file on rollback** from the rollback directory, reapplying the recorded ACL, not just
   the daemon files. The account-46 `MANIFEST.json` is restored like any other pre-existing file.
4. **Include every restored file in the post-rollback byte comparison.** Rollback is complete only when each restored
   path hashes equal to its recorded pre-rollout SHA-256 and carries its recorded ACL.
5. **Distinguish pre-existing files from newly-added files in the plan.** Pre-existing files are *restored* on
   rollback; newly-added files (the `Trigger-*` primitive, the bundle one-shot artefacts, the new task) are *removed*
   on rollback. Each file in the rollout plan is tagged `pre-existing (restore)` or `new (remove)` before mutation.
6. **Preserve the rollback directory as evidence.** Do not delete `C:\GuvFX\_rollback\<rollout-id>\` or its inventory
   on rollback or on success; it is retained until a later packet explicitly authorizes its cleanup.
7. **Treat staging cleanup separately from operational rollback.** The staging directory
   (`C:\GuvFX\_staging\<rollout-id>\`) is a transfer buffer, not the rollback set; cleaning or leaving it is a
   separate, independently authorized step and never part of the fail-closed rollback.
8. **Never copy or back up a secret.** The HMAC ingest key and any credential are out of scope for both the rollout
   and the backup set; the rollback directory holds only non-secret bundle/daemon/task artefacts. If a candidate file
   to back up could contain a secret, it is excluded and flagged, not copied.

The task remains DISABLED and triggerless throughout; rollback removes the newly-created one-shot task and new
primitive, restores the prior daemon files and the account-46 `MANIFEST.json` with their ACLs, restarts the executor,
and re-verifies the original state. No flag, key, verifier window, trigger or broker action is part of this rollout.

## 6. HARD STOP — separate future Sponsor gates
Continuous emitter operation; scheduler enablement; ingestion/projection/dispatch/alert flag enablement; a real
alert transport; follower activation; read-only/shadow production observation; any live one-order acceptance.
Each is a distinct authorization. This packet stops at a reviewed, unmerged PR (auto-merge OFF).
