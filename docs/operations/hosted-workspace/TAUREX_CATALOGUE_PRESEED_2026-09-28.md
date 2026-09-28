# Taurex broker preseed — governed capture + v2 catalogue activation (PR D, 2026-09-28)

Adds **Taurex / Taurex-Demo** to the broker catalogue so a NEW Taurex-Demo hosted account preseeds the broker
discovery `servers.dat` on first launch (instead of a ~5-minute native discovery). Data-driven: **no backend
model/migration change** — see the code map in `BROKER_CATALOGUE_PRESEED_2026-08-26.md` /
`CATALOGUE_AND_UPDATE_GOVERNANCE_2026-08-26.md`. The reproducible git change for this packet is the Taurex test
coverage in `backend/broker_catalogue/tests.py` + this runbook.

## Authoritative identity
- Broker: **Taurex**  · Server: **Taurex-Demo** (DEMO) — proven by the live Account-36 MT5 (`account_info().server
  == "Taurex-Demo"`; `TradingAccount(id=36).broker_server.server_name == "Taurex-Demo"`, `broker_name == "Taurex"`).
- Catalogue `servers` list is **`["Taurex-Demo"]` only** — `Taurex-Live` is deliberately omitted so a Live account
  never preseeds a demo-captured file; Live falls back to native discovery until separately certified.

## Capture (Phase D2) — evidence
- Source (EVIDENCE ONLY, copied read-only): `C:\GuvFX\accounts\36\terminal\config\servers.dat`. **Only
  `servers.dat` was copied** — never `accounts.dat`, `bases\`, `logs\`, `history\`.
- Candidate: `C:\GuvFX\catalogue\_candidates\taurex\servers.dat`.
- **size_bytes = 47384**
- **sha256 = `23fd33b87f3d6b628ba6cb457189e3cd8caa03617a381306e3873b2bba6fd876`**
- source MT5 build: 5.0.0.6073 (golden).

## Credential-free proof (Phase D3)
- Candidate dir contains only `servers.dat` (no `accounts.dat`/`logs`/`history`/`deals`/`orders`/`positions`/
  `passwords`/`dpapi`/`origin.txt` path markers) — `forbidden_path_present=False`.
- Explicit Account-36 login **`830227146` absent** from the bytes (ASCII and UTF-16LE): both `False`.
- Candidate SHA ≠ `accounts.dat` SHA (`candidate_equals_accounts_dat=False`).
- `servers.dat` is Class A public broker metadata (opaque server list; identical for every Taurex user).
- ⇒ **`CREDENTIAL_FREE_PROVEN = True`**. If any check had failed, capture fails closed and the artefact is NOT
  activated.

## Amber decisions (recorded)
1. **Capture source = Account 36 (a live Provider-B acceptance runtime), not a disposable runtime.**
   `CATALOGUE_AND_UPDATE_GOVERNANCE` step 1 prefers a disposable runtime. This packet authorised Account-36 as
   evidence source; the risk is bounded by copying **only** the credential-free Class-A `servers.dat` and the
   proof above. A future recapture from a disposable Taurex terminal may supersede this artefact under a new SHA.
2. **Version rebump v1 → v2.** `CatalogueVersion` is immutable and activation is version-wide, so Taurex cannot be
   added to the ACTIVE v1. v2 re-includes the byte-identical Pepperstone + IS6 artefacts (same SHAs) plus Taurex.
   Reversible: re-activate v1.

## Production activation (Phase D4/D5/D9) — exact commands (run in `guvfx-backend`)
Prod flags already ON: `HOSTED_BROKER_CATALOGUE_ENABLED=1`, `APPROVALS_ENABLED=1`. v1 artefacts:
pepperstone `afd6d65b…` (86592B), is6 `a05ddd55…` (36528B).

1. Stage bytes on host under the v2 store (immutable):
   - `C:\GuvFX\catalogue\versions\v2\pepperstone\servers.dat` ← copy of `versions\v1\pepperstone\servers.dat`
   - `C:\GuvFX\catalogue\versions\v2\is6\servers.dat` ← copy of `versions\v1\is6\servers.dat`
   - `C:\GuvFX\catalogue\versions\v2\taurex\servers.dat` ← the certified candidate (`23fd33b8…`)
2. Register approvals (re-use identical SHAs for pep/is6):
   - `register_artefact_approval --kind broker_servers_dat --ref pepperstone/v2 --sha256 afd6d65b… --metadata '{"broker":"Pepperstone","servers_intended":["PepperstoneUK-Demo","PepperstoneUK-Live"],"size_bytes":86592,"source_mt5_build":"5.0.0.5833","sanitisation":"PASS","behavioural_cert":"PASS"}'`
   - `register_artefact_approval --kind broker_servers_dat --ref is6/v2 --sha256 a05ddd55… --metadata '{"broker":"IS6 Technologies","servers_intended":["IS6Technologies-Demo","IS6Technologies-Live"],"size_bytes":36528,"source_mt5_build":"5.0.0.6073","sanitisation":"PASS","behavioural_cert":"PASS"}'`
   - `register_artefact_approval --kind broker_servers_dat --ref taurex/v2 --sha256 23fd33b8… --metadata '{"broker":"Taurex","servers_intended":["Taurex-Demo"],"size_bytes":47384,"source_mt5_build":"5.0.0.6073","sanitisation":"PASS","behavioural_cert":"PASS","capture_source":"account36_servers_dat_evidence_only","credential_free_proof":"login_830227146_absent+no_accounts_dat"}'`
3. Staff-approve each: `decide_artefact_approval --id <n> --approve --by <staff-email> --reason "sanitised+credential-free+behaviourally certified (Account 36 live Taurex-Demo)"`.
4. `build_catalogue_version --label v2` (DRAFT + one CatalogueArtefact per `*/v2` approval).
5. `activate_catalogue_version --label v2 --require-certified` (fail-closed; retires v1, records `rollback_to=v1`).

## Verification
- `resolve_active_version().label == "v2"`; v1 status RETIRED, `v2.rollback_to == v1`.
- `resolve_artefact_for_server("Taurex-Demo").broker_id == "taurex"`; `resolve_artefact_for_server("Taurex-Live") is None`.
- Pepperstone/IS6 still resolve unchanged.
- Accounts 25/35/36 unaffected (already provisioned; preseed only runs during new `prepare_hosted_slot`).

## Rollback
`activate_catalogue_version --label v1` re-activates v1 (byte-identical prior behaviour). The v2 host bytes and
approvals remain for a later retry. No live-estate impact either way.
