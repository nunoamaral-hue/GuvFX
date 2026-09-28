# Taurex broker preseed — governed capture + v2 catalogue activation (PR D, 2026-09-28)

> **STATUS: production activation HELD (Sponsor decision, 2026-09-28) — "harden first".** The capture +
> credential-free proof + DEMO-only test coverage are complete and merged, but the v1→v2 activation below is
> **NOT executed**: the mandated adversarial review flagged pre-existing catalogue-mechanism gaps (see "Deferred
> hardening" + `docs/KNOWN_ISSUES.md`). The **Broker-Catalogue Hardening packet runs first**; then this activation
> proceeds. Until then, Taurex-Demo accounts fall back to native MT5 discovery (~5 min) — no breakage. The
> credential-free candidate is staged (read-only, inert, not in any active version) at
> `C:\GuvFX\catalogue\_candidates\taurex\servers.dat` (sha `23fd33b8…`) as evidence for the hardening packet.

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

## Credential-free proof (Phase D3) — bounded evidence
Only `servers.dat` was copied (candidate dir contains only that file; no `accounts.dat`/`logs`/`history` path
markers — `forbidden_path_present=False`). Expanded byte-scan of the 47384-byte artefact for Account-36 identity:
- Login **`830227146` absent in ALL of: ASCII, UTF-16LE, UTF-16BE, int32-LE, int32-BE, int64-LE, packed-BCD,
  zero-padded** (every pattern `present=False`).
- **`support@guvfx.com`, `guvfx`, `Nuno`, `Nuno A` absent** (ASCII + UTF-16LE); the user's other logins
  (`62145672`, `1302587`) absent.
- **Entropy sweep: max 7.3 bits/byte, ZERO 256-byte windows > 7.5** ⇒ no compressed/encrypted (wrapped-secret)
  region where a DPAPI blob / session token could hide.
- ⇒ **`CREDENTIAL_FREE_STRENGTHENED = True`** (fail-closed: any hit aborts before activation, per Phase D3).

**Stated limitations (evidence rule).** A decisive cross-user byte-diff (a SECOND independent Taurex account) was
**NOT** run — creating a production account solely for verification is avoided per the packet's non-destructive
rule, and no pristine broker-shipped Taurex `servers.dat` was available on the host. User-independence therefore
rests on: (a) zero Account-36 PII in any encoding + zero high-entropy regions (above); (b) the MT5 architectural
fact that `config\servers.dat` is the broker's public access-server directory (credentials live in `accounts.dat`,
never copied); and (c) **precedent** — the already-CERTIFIED IS6 catalogue artefact (`a05ddd55…`) was itself
captured from a live CONNECTED runtime (support@/acct25), so live-runtime capture is the established, approved
method, not a novel risk.

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

## Activation attestation (compensating control for the mechanism gaps below)
`activate_catalogue_version` has no host byte-staging attestation, so before running `activate --label v2`,
manually confirm on the host that each `versions\v2\{pepperstone,is6,taurex}\servers.dat` exists and its
`Get-FileHash -SHA256` equals the approved `CatalogueArtefact.sha256` (pep `afd6d65b…`, is6 `a05ddd55…`, taurex
`23fd33b8…`). Confirm the carried-over pepperstone/is6 v2 bytes are byte-identical to their v1 sources (copy +
read-back). Do not activate if any SHA differs.

## Deferred hardening — adversarial findings (recommend a dedicated Broker-Catalogue Hardening packet)
The PR-D adversarial review surfaced **pre-existing** catalogue-mechanism gaps that apply equally to the live V1
(Pepperstone/IS6) — they are NOT introduced by adding Taurex, and fixing them is a shared-gate change (Amber/Red)
beyond an additive broker add (architecture rule: no whole-subsystem rewrites per packet). Recorded in
`docs/KNOWN_ISSUES.md`; compensating controls applied here noted inline:
- **[HIGH] `servers` list is not SHA-guarded / not in `manifest_sha256`** — a wrong `servers_intended` (e.g. adding
  `Taurex-Live`) would silently defeat DEMO-only scope. *Compensating control:* Taurex metadata is `["Taurex-Demo"]`
  only, reviewed at the staff-approve gate; tests assert Demo resolves / Live → None.
- **[HIGH] `CatalogueArtefact` mutable after activation** (admin editable; no save-guard; `manifest_sha256` never
  re-verified at resolution). *Fix (hardening packet):* readonly admin + save-guard for non-DRAFT versions +
  re-verify manifest at resolution.
- **[HIGH] No byte-content sanitiser in build/activate** — only path-based `scan_forbidden`. *Compensating control:*
  the expanded byte + entropy scan above, run manually and recorded. *Fix:* a stdlib content sanitiser wired into
  `build`/`activate`, verdict pinned to the SHA.
- **[MEDIUM] `build_catalogue_version` last-write-wins across duplicate approval rows** (a later PENDING SHA can
  clobber an APPROVED one). *Fix:* select the single APPROVED row per `(kind, ref)`.
- **[MEDIUM] No carried-over-SHA guard** (v2 pep/is6 SHA must equal v1). *Compensating control:* the activation
  attestation above.
- **[MEDIUM] No cross-artefact server-name uniqueness** (first-match resolution). *Fix:* reject duplicate server
  names across a version at build/activate.

## Rollback
`activate_catalogue_version --label v1` re-activates v1 (byte-identical prior behaviour). The v2 host bytes and
approvals remain for a later retry. No live-estate impact either way.
