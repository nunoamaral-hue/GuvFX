# Broker Catalogue V3 — carry-forward build, governed staging, attestation, activation (RUNBOOK)

Covers promoting ACTIVE **v2** → **v3** by carrying forward the three approved v2 artefacts and adding the two
separately-approved brokers (TradersWay id=7, Vantage id=8). ADR-0068 is authoritative for the design.

**This repository change is DARK.** It adds the mechanism; it performs **no** production mutation. Every step below
from "Phase 6" on is a human/Sponsor-gated production action — do not run it from this PR.

## State at authoring (verified, read-only)
- Canonical/deployed main: `a120c80b`. Production catalogue: **v2 ACTIVE, no DRAFT**. Execution OFF.
- Approvals: id=7 `tradersway/v1` `cd41d8d6…173bd4af` (37,280 B); id=8 `vantage/v1` `34360ad9…8f637b` (140,176 B).
- Immutable approved-byte pins on the VPS (`ubuntu@100.119.23.29`, read-only `0444`):
  - `~/guvfx-evidence/broker_catalogue/tradersway_v1_20261030T094626Z/servers.dat` (SHA `cd41d8d6…`)
  - `~/guvfx-evidence/broker_catalogue/vantage_v1_20261030T080853Z/servers.dat` (SHA `34360ad9…`)
- v2 host-staged bytes (`C:\GuvFX\catalogue\versions\v2\{is6,pepperstone,taurex}\servers.dat`) match their approved
  SHAs exactly (carry-forward re-stages byte-identical content under `versions\v3\…`).

## Host deployment mechanics (Phase 6 prerequisite — RED)
The two new primitives are account-independent host operations. Running them requires the host-side executor to be
updated and restarted:
1. **Stage the bundle** per `deploy/hosted-executor/stage-manifest.json` (already lists `host_protocol.py`,
   `host_agent_dispatch.py`, `Stage-GuvfxCatalogueArtefact.ps1`, `Attest-GuvfxCatalogueArtefact.ps1`). The daemon
   ParseFile-gates **every** `.ps1` at startup (RULE 9 / RULE 11 positive marker) and refuses to serve if any fails.
2. **Restart the `GuvFXHostedExecutor` service via its supported service mechanism (WinSW)** — never a
   `Start-Process`/`nohup` over interactive SSH (**RULE 1**). Validate the out-of-Git launchers before the restart
   (**RULE 8**).
3. The executor must be **armed + configured** (`HOSTED_HOST_EXECUTOR_ENABLED` + keyring/key_id/base_url) for
   `--attest-host` to have a transport; otherwise `_attest_executor()` is `None` and activation refuses (fail-closed).

## Phase sequence (production — gated)
- **Phase 4 — backend deploy (rollback-first).** Tag the current backend image as rollback; build from merged main
  with exact `GIT_COMMIT`; `docker compose up -d --no-deps --force-recreate guvfx-backend` (NO `--remove-orphans`).
  No DB migration is required by this change. No worker restart unless the changed transport proves it necessary.
- **Phase 5 — build DRAFT v3 (DB-only).**
  `build_catalogue_version --label v3 --carry-forward-active --include-approved-ref tradersway/v1 --include-approved-ref vantage/v1`
  → creates DRAFT v3 = {is6,pepperstone,taurex (carried), tradersway, vantage}. Verify v2 unchanged + still ACTIVE.
- **Phase 6 — stage + attest.** For each v3 artefact, stage the exact approved bytes via the governed staging op
  (Vantage/TradersWay from the immutable pins; carried brokers from their v2-identical approved bytes). Then run
  read-only attestation: every artefact's staged SHA must match AND the staged inventory must equal the DRAFT
  manifest exactly (no extra/missing). STOP on any mismatch. Proves no golden/workspace mutation; no task restart.
- **Phase 7 — activate + accept.** `activate_catalogue_version --label v3 --attest-host` (atomic; retires v2 as the
  rollback target). Verify the resolver returns the v3 preseed for TradersWay + Vantage, and that existing accounts
  are unchanged. Create two **disposable, non-trading** workspaces (TradersWay, Vantage), verify each gets the
  correct broker/server from the staged artefact checksum with no login/order, then dispose them.
- **Phase 8 — safety + rollback drill.** Reconfirm Copy Trading/execution flags OFF, LiveExecutionAuthorization 0,
  sole map 46→48, zero copier jobs/orders, existing workspaces unchanged. Rollback is
  `rollback_catalogue_version --to v2` (re-verifies v2's manifest + approvals + server uniqueness before
  re-activating); dry-run it before relying on it.

## Rollback
`rollback_catalogue_version --to v2` atomically restores v2 (fail-closed: refuses a non-RETIRED target, a failed
manifest integrity, or any unapproved artefact). The staged `versions\v3\…` bytes are inert once v3 is not ACTIVE.

## Safety invariants (hold throughout)
Account-independent ops never touch an account slot, tenant workspace, or the golden image; staging writes only
under `C:\GuvFX\catalogue\versions`; attestation is read-only. No mapping/flag/credential/order change. Execution
stays OFF.
