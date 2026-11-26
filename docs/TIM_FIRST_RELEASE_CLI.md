# TIM first governed release — operational CLI workflow + live staging (GO #2d, ADR-0127)

**Status:** repository + disposable-CI **only**. DARK. No operational signing, no live task
registration, no rights change, no NAS publication, no real data. This document describes the reviewed
command-line workflow and the **live staging inventory**; the live run is the separately Sponsor-gated
execution packet. Pins: GuvFX builder/consumer `1878c8818bacde3aa15574c2f886c6c127714062`; producer signer
`836cfd3c32779d661159e5b848f234fc7d48793e`; operational key `ed25519-ff37421d2467768a7cc8050932ef6833`
(window `2026-11-26T01:06:48Z → 2027-11-26T01:06:48Z`), pinned in
`research/market_data/trust_store/operational_public_keys.json`.

The producer signs; TIM only verifies (ADR-0070). `tools/tim_release_cli.py` never signs, never handles a
key, and performs no network / NAS / MT5 / broker / credential / production action of its own. The signature
is produced out of band on the producer host by `scripts/tim_signing_provider.py sign-release`.

---

## 1. The four commands

All four are in `tools/tim_release_cli.py` and are fail-closed and value-blind (receipts carry reason
codes, PUBLIC digests/fingerprints, counts and release-relative paths only — never key bytes, secrets or
host-absolute paths).

| Command | Role | Writes |
|---|---|---|
| `build` | one synthetic v0.3 release under an approved local root + `request.fields.json` (the exact producer `sign-release` fields); candidate kept **UNSEALED** | candidate dir |
| `assemble` | ingest `release.signature.json`, verify against the exact candidate manifest + **pinned** trust store, then **seal** (manifest.sig.json, then READY last) | candidate dir |
| `publish` | copy a sealed candidate to an authenticated producer mount, verify destination identity/scope, refuse overwrite, write **READY last** after all destination hashes verify | mount |
| `validate` | **standalone, read-only** consumer: READY binding, operational signature vs the pinned store, all nine evidence references, every file hash | nothing |

Rejections (value-blind reasons): wrong key → `unknown_signing_key` / `key_fingerprint_mismatch` /
`key_revoked` / `key_algorithm_mismatch`; expired window → `key_not_valid_at_created_at`; bad signature →
`signature_invalid`; bad manifest → `manifest_digest_mismatch` / `identity_mismatch` /
`request_manifest_mismatch`; altered candidate files → `altered_files`; already-sealed / existing release →
`already_sealed` / `release_exists`; unverifiable mount → `mount_identity_unverifiable` /
`mount_identity_mismatch` / `mount_scope_mismatch`; partial/no READY → `missing_ready`; tampered published
file → `file_hash_mismatch`; synthetic signature on the operational path → `operational_required`.

### Live invocation (pinned)

The venv is the research venv (DuckDB) on the controller `mac-control-node`; `--root` is an approved local
root **outside** the repository (`$GUVFX_DATA_ROOT`).

```
# 1. BUILD on the controller (emits request.fields.json; candidate UNSEALED)
python tools/tim_release_cli.py build \
  --root "$GUVFX_DATA_ROOT/tim-first-release" \
  --signing-key-id ed25519-ff37421d2467768a7cc8050932ef6833 \
  --key-not-before 2026-11-26T01:06:48Z --key-not-after 2027-11-26T01:06:48Z \
  --created-at <UTC instant within the key window> \
  --receipt "$GUVFX_DATA_ROOT/tim-first-release/build.receipt.json"

# 2. (OUT OF BAND) the custodian signs on the producer host -> release.signature.json
#    (the one-shot Password-logon task; see §4 and the producer docs/TIM_ONESHOT_SIGNER.md)

# 3. ASSEMBLE on the controller (verify vs the PINNED committed store, then seal)
python tools/tim_release_cli.py assemble \
  --candidate "$GUVFX_DATA_ROOT/tim-first-release/staging/<release_id>" \
  --signature "$GUVFX_DATA_ROOT/tim-first-release/release.signature.json" \
  --receipt "$GUVFX_DATA_ROOT/tim-first-release/assemble.receipt.json"

# 4. PUBLISH to the authenticated producer mount (READY last)
python tools/tim_release_cli.py publish \
  --candidate "$GUVFX_DATA_ROOT/tim-first-release/staging/<release_id>" \
  --mount-root <authenticated producer mount root> --scope exchange/releases \
  --expect-identity guvfx-tim-producer \
  --receipt "$GUVFX_DATA_ROOT/tim-first-release/publish.receipt.json"

# 5. VALIDATE on the Mac mini consumer (read-only, pinned store)
python tools/tim_release_cli.py validate \
  --mount-root <mounted share root> --scope exchange/releases --release-id <release_id> \
  --receipt <consumer receipt path>
```

`assemble` and `validate` default to the **committed pinned** trust store (the live path). CI passes an
explicit `--trust-store` isolated store pinning the disposable key.

---

## 2. Live staging inventory — producer signer bundle

Stage the producer signer bundle at the reviewed head (producer squash `836cfd3c…`, feature head
`4ba902af…`) under the custody layout on the producer host. The six reviewed artefacts and their
byte-exact SHA-256 (from the merged signer evidence manifest
`GFX-EVD-GO2D-ONESHOT-SIGNER-042.json`):

| Artefact (producer repo) | SHA-256 |
|---|---|
| `scripts/Install-TimOneShotSigner.ps1` | `cc72b2750163a2b534a1237d00769d27dbb08252d1f0e6ec576545cd63e68d6e` |
| `scripts/tim_signing_provider.py` | `e1e5026d7ae11d3098102219e9f0ca60c916fcb16f48d55532a894116da68378` |
| `tests/oneshot_signer.Tests.ps1` | `000dc46c996174e67f727a7bfc8e1f6b476790132cf481997dbdca3f6c5f960c` |
| `tests/test_tim_oneshot_signer.py` | `19d5ebb95cedcef283e9fe467ccb754efff37b5c433cbb709755b800f9109c88` |
| `.github/workflows/ci.yml` | `3fc753b884db55fb86e7e9963aef9ef20899e81fa94726c20cd3116c08dc5057` |
| `docs/TIM_ONESHOT_SIGNER.md` | `49b0e72930a04f9a6ce11023b18cf57a8bf2d834d5c3387ebb48ecc69cffe18e` |

**Imports / import resolution.** `scripts/tim_signing_provider.py` imports exactly one package:
`from guvfx_windows_history_agent import tim_signing` (the stdlib narrow signer — no `cryptography` in that
module). It is the only file importing the audited `cryptography` Ed25519 primitives (in the provider class,
runtime only). A self-locating bootstrap prepends the repo's own `src` dir to `sys.path` when run by
absolute path (so the one-shot task action needs no `PYTHONPATH` and no shell wrapper). Import resolution is
proven on disposable Windows CI in the producer `tim-provisioning-runtime` lane (run `45836824153`, 9/9
jobs; the exact task action `venv python + signer + --fields-file + --out` produced one valid operational
signature end-to-end with a synthetic key).

**Interpreter.** `C:\GuvFX\TIM\provisioning-venv\Scripts\python.exe` (venv with `cryptography`).

**Paths (fixed custody layout).** Store root `C:\ProgramData\GuvFX\TIM\signing` (one
`*.ed25519.pem`, unencrypted PKCS#8, loaded `load_pem_private_key(data, None)`; **access is governed by the
NTFS ACL bound to the account SID, not a passphrase/DPAPI**). One-shot root
`C:\ProgramData\GuvFX\TIM\oneshot` with `request.fields.json` and `out\release.signature.json`.

**ACLs (from `Install-TimSigner.ps1`, host-deployed).** Store inheritance disabled; owner
`BUILTIN\Administrators`; only `SYSTEM` + `Administrators` FullControl, plus `guvfx_tim_producer`
ReadAndExecute. The one-shot output dir is **not** writable by the producer beyond the single approved
output (exclusive-create, never overwrite).

**CLI argument order (the task action, fixed, no embedded quotes):**
```
<venv python>  <repo>\scripts\tim_signing_provider.py  --store-root C:\ProgramData\GuvFX\TIM\signing  sign-release  --fields-file <one approved request.fields.json>  --out <one release.signature.json>
```
This exact order and the fields contract are exercised from the GuvFX side by
`tests/test_tim_release_cli.py` (the `_producer_sign` handoff builds the envelope `P.build_v3_envelope`
consumes the eight `request.fields.json` keys and emits the producer `--out` format), and the GuvFX↔producer
signing-material lock is held by `tests/test_tim_signing_cross_repo_lock.py`.

---

## 3. Disposable-test verification (what CI proves, and what it does not)

`tests/test_tim_release_cli.py` (GuvFX `market-data-foundation` lane) drives the **actual** CLI entry points
end-to-end with a DISPOSABLE ephemeral Ed25519 key (pure-python RFC-8032 test signer reusing
`tim_ed25519`), an **isolated** test trust store, and a **local mount substitute**: build → file handoff to
the pinned signer contract and back → assemble → publish → validate accepts; plus interrupted publication
(destination hash verify fails → no READY → consumer `missing_ready`), missing READY, tampered published
file (`file_hash_mismatch`), wrong key (committed store → `unknown_signing_key`), expired window
(`key_not_valid_at_created_at`), altered candidate files (`altered_files`), overwrite refusal
(`release_exists`), mount identity (`mount_identity_unverifiable`), and a **write-free** consumer run
(read-only tree, byte-for-byte unchanged).

**CI evidence is distinct from live NAS acceptance.** CI uses a disposable key + local directory; it never
touches the real key, the NAS, or the Mac mini. Live first-release completion is declared only after the Mac
mini accepts the published copy (§5).

---

## 4. Custodian handoff (live — separately Sponsor-gated)

Prepared on the controller before any password prompt: `build` has produced the candidate and
`request.fields.json`; `publish`/`validate` are pinned to the committed trust store. Then, on the producer
host, the custodian follows the reviewed handoff in the producer `docs/TIM_ONESHOT_SIGNER.md`:

1. **Stage** the bundle (§2) at the custody layout; the operational key is already provisioned + pinned
   (GO #2a). Transfer exactly two files over the existing verified controller↔Windows route: the approved
   `request.fields.json` into Windows staging, and `release.signature.json` back. Verify the SSH host
   identity (`SHA256:LtXERMx+Qhflcc+lazN/JGIm4erbqAACQWRgvZQy4Lo`) and the SHA-256 across each transfer; keep
   the Windows request administrator-controlled and read-only to the signer.
2. **Run once** the reviewed Password-logon one-shot task under `guvfx_tim_producer`
   (`Install-TimOneShotSigner.ps1 -LogonType Password -ServiceAccountPassword (Read-Host -AsSecureString)`
   → `-Verify` → stage the one `request.fields.json` → `-Run` → collect `release.signature.json`). Preserve
   **both** interactive deny-logon rights; keep the placeholder service `GuvFXTimSigner` **Disabled**.
   If the service-account password is unavailable, use the separately documented human reset **only after**
   confirming the account **SID** and the key **ACL** are preserved (the key is ACL/SID-protected, not
   passphrase/DPAPI, so a reset that keeps the SID keeps key access).
3. **Teardown and verify.** Remove the task and the one `SeBatchLogonRight` added for the run; verify
   teardown **even on failure** (task removed; batch right removed; both deny rights intact; placeholder
   service still Disabled). Stop before publication if teardown or signature verification fails.

The private key never leaves its protected store; the custodian handles the password; the operator enters
approvals and one-time codes.

---

## 5. Publish + Mac mini acceptance

On the controller, `assemble` verifies the returned signature against the pinned store and seals; `publish`
writes the release to the authenticated producer mount as `guvfx-tim-producer` with READY **last**. Then on
the Mac mini (consumer identity `tim-research-m6`), mount the share in Finder, run `validate` (§1 step 5)
against the mounted copy, confirm the consumer receipt `accepted: true` with the operational signature
verified, all nine evidence references and every file hash checked, then eject. **Declare first-release
completion only after the Mac mini accepts the published copy.**

---

## 6. Scope

One **synthetic** release. Real-data acquisition, training, service start, and deployment are out of scope.
The operational signing run, live task registration, rights changes, and NAS publication are the
Sponsor-gated live execution packet, not this repository change.
