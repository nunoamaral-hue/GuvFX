# Copy Trading host emitter — install, attestation, synthetic acceptance & rollback (ADR-0076)

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

## 6. HARD STOP — separate future Sponsor gates
Continuous emitter operation; scheduler enablement; ingestion/projection/dispatch/alert flag enablement; a real
alert transport; follower activation; read-only/shadow production observation; any live one-order acceptance.
Each is a distinct authorization. This packet stops at a reviewed, unmerged PR (auto-merge OFF).
