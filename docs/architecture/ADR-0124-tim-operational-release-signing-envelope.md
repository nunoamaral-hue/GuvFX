# ADR-0124 — TIM operational release-signing envelope (v3, algorithm-agile, Ed25519 verify)

- **Status:** Accepted (repository-only; additive; migration-free; the operational verification path is
  **dark by default** because the committed trust store is empty; no production deploy, no key generation,
  no key custody, no host/NAS/broker contact, no release built, signed, published or consumed by this
  packet). The PR is opened auto-merge OFF and is **not merged** without a new Sponsor authorisation.
- **Packet:** GFX-TIM-GO2-RELEASE-FOUNDATION-014 (TIM — first governed release foundation; GO #2a
  repository half).
- **Date:** 2026-11-24.
- **Relates to:** ADR-0070 (GuvFX publishes governed market data; TIM is a read-only consumer — producer
  signs, TIM verifies, signing is an injected synthetic placeholder until a later gated packet selects the
  real key + algorithm), ADR-0073 §G9 (algorithm-agile detached signature envelope), ADR-0076 §K-7
  (manifest↔signature identity binding), and the GO #2 blocker-closure preflight
  (GFX-TIM-FIRST-RELEASE-BLOCKER-CLOSURE-PREFLIGHT-007, Blocker 1 — operational signing custody).

## Context

GO #2 (the first governed TIM release) is withheld on three blockers; Blocker 1 is **operational signing
custody**. The existing v0.3 signature envelope `dataset_release_signature_v2` is structurally
algorithm-agile — it already carries `algorithm`, `signing_key_id`, `public_key_fingerprint` and
`key_validity` — **but** two things pin it to the synthetic pilot:

1. `signature_value` is schema-locked to a 64-hex sha256 (`SHA256_RE`). A real signature scheme
   (Ed25519 = 64 bytes = 128 hex) does not fit that field.
2. `validate_release_signature_v2` **hard-pins** `synthetic: true` and `algorithm ==
   synthetic-placeholder-v0` (enforced, not defaulted). Verification is injectable via a `verifier=`
   seam whose default is the synthetic signer.

ADR-0070 reserves the selection of the operational key + algorithm — and "replacing the injected synthetic
verifier" — to "a later packet … under its own Sponsor authority." Going operational is therefore a
**governed contract change**, not merely key material: the envelope and validator must be able to accept a
*verified non-synthetic* signature. That contract change is the subject of this ADR; the key material and
its custody remain a separate Sponsor-gated step (GO #2a), performed **off-repository**.

A hard constraint shapes the design: the TIM contract tests and the governance lane run **stdlib-only**
(`cryptography` is importable in the Django backend venv but **not** in the Python-3.14 research venv or
the governance `python3` lane). TIM must therefore verify a producer signature with no third-party
dependency.

## Decision

Add a **new, additive v3 operational signing subsystem** that extends — never replaces — the v0.3 contract.
The byte-frozen v0.2 modules (`contracts.py`, `tim_contracts.py`, `tim_fixtures.py`) and the pinned v0.3
schema/fixture counts are **untouched**; the synthetic placeholder path stays fully valid for synthetic
releases (backwards compatible).

1. **Verify-only, pure-stdlib Ed25519** (`research/market_data/tim_ed25519.py`). An RFC 8032 Ed25519
   **verification** primitive with no third-party dependency, validated against RFC 8032 test vector 1 and,
   in the tests and an offline differential fuzz, against an independent implementation. It implements **no
   signing and no key generation** — deliberately, because under ADR-0070 the producer signs and TIM only
   verifies. It verifies **strictly** and fails closed (returns `False`): it rejects a non-canonical scalar
   `S ≥ L` (malleability guard), non-canonical point encodings (`y ≥ p`, or a sign bit set on `x == 0`), and
   **small-order `A` or `R`** (so a crafted signature against a degenerate/low-order key cannot be accepted).
   It never logs or echoes inputs. `is_acceptable_public_key` exposes the same canonical/non-small-order
   check for pin-time key vetting.

2. **Pinned local trust store** (`research/market_data/tim_trust_store.py` +
   `research/market_data/trust_store/operational_public_keys.json`). Trust flows from this pinned,
   immutable, **public-key-only** store — never from the signature envelope. Deterministic key-id selection
   (exactly one entry per `signing_key_id`; a duplicate id is a permanent `TrustStoreIntegrityError`), each
   entry fail-closed-validated (`public_key_fingerprint == sha256(public_key)`, known algorithm, status,
   ordered validity window). Rotation = add the new `signing_key_id`, keep the previous active during the
   overlap window; revocation = set `status: revoked` (kept, not deleted, so a revoked key is positively
   recognised and rejected rather than looking merely unknown). **The committed default store is empty**, so
   operational verification fails closed (`unknown_signing_key`) until a real public key is pinned
   out-of-band under GO #2a.

3. **Algorithm-agile v3 envelope + fail-closed validator/verifier**
   (`research/market_data/tim_release_signing_v3.py`). `dataset_release_signature_v3` keeps the v2 field set
   and identity bindings, and:
   - makes `signature_value` **algorithm-agile** — 64 hex for the synthetic placeholder, 128 hex for
     `ed25519-v1` — with the length enforced per algorithm and the algorithm **closed-world** (an unknown
     algorithm is rejected at validation);
   - requires `synthetic` to be true **iff** the algorithm is the synthetic placeholder
     (confusion/downgrade guard);
   - binds, into the signed material, a **distinct v3 domain separator**, the **algorithm**, and the
     **`created_at_utc` signing time**, in addition to the v2-bound release id / manifest digest /
     contract- + validator-bundle digests / producer identity / signing-key id / public-key fingerprint.
     Binding the algorithm blocks relabelling; binding the signing time makes the validity check
     tamper-evident; the distinct domain separator blocks cross-version signature reuse (downgrade);
   - resolves the verification key from the pinned trust store by `signing_key_id`, confirms the pinned
     key's fingerprint matches the envelope, checks the validity window and revocation status, and only then
     verifies the detached signature against the **pinned** public key;
   - fails closed, with **value-blind reason labels** (never key bytes, signature bytes, paths or secrets),
     on: malformed envelope, manifest-digest mismatch (tamper), manifest identity mismatch (K-7),
     unknown/relabelled algorithm, unknown/revoked/fingerprint-mismatched/out-of-window key, invalid
     signature, and — when the caller requires an operational release — any synthetic signature
     (`operational_required`, so operational releases fail closed on placeholder/synthetic signing);
   - provides `verify_operational_release_signature_v3`, the verify an operational consumer should call: it
     hard-codes `require_operational=True` (so the non-cryptographic synthetic path can never be mistaken
     for an operational signature) and makes the trust store mandatory. The trust store also refuses to pin
     a degenerate/low-order key (`is_acceptable_public_key`), belt-and-braces with the verifier's own
     small-order rejection.

4. **Tests + CI.** `tests/test_tim_release_signing_v3.py` (stdlib-only, 50 tests) is wired into
   `make governance-check` and the CI **governance** job (where it runs without the research venv). Fixtures
   under `tests/fixtures/tim_release_signing_v3/` were produced offline by an **ephemeral** Ed25519 test key
   whose private seed was never written; only the public key, its fingerprint and the (public) signatures
   are committed.

The producer-side signer lives in the private producer repository, **not** here; this repository gains the
*verification* half only.

## Consequences

- TIM can now verify a real operational Ed25519 producer signature end-to-end, with no third-party
  dependency, while the producer-signs/TIM-verifies-only boundary (ADR-0070) is preserved — this repository
  contains no signing primitive and no private key.
- Operational verification is **dark** until a real public key is pinned out-of-band (GO #2a); the empty
  default trust store makes "operational fails closed" the repository default, provably.
- The synthetic pilot (`synthetic-placeholder-v0`, `synthetic: true`) remains valid for synthetic releases;
  the v2 envelope/validator and all v0.2/v0.3 fixtures and frozen hashes are unchanged.
- **Known, documented limitations (stated, not implied; see
  `docs/evidence/tim_operational_signing_v3_migration_and_admission.md`):** (a) `key_validity` (the window
  *object*) is not part of the signed material, though `created_at_utc` is; the authoritative trust decision
  is trust-store membership + active status + the pinned window checked against the signed `created_at_utc`,
  and rotation/revocation are operator actions on the pinned store. (b) `signed_manifest_path` is validated
  structurally and cross-checked against the manifest (K-7) but is not bound into the signed material (the
  manifest digest and release id are the authoritative identity binding). (c) The K-7 manifest cross-bind
  runs only when the parsed manifest is supplied; the operational admission path should pass `manifest=`.
  None of these is a forgery path against a legitimately pinned full-order key.
- Still-open GO #2 items are **out of scope** and unchanged: key generation/custody (GO #2a, off-repo),
  producer `GuvFXData`/`GUVFX_DATA_ROOT` readiness (Blocker 2, BLOCKED), winter timezone (now VERIFIED,
  ADR-0121). No release is built, signed, published or consumed here.

## Alternatives considered

- **Relax `signature_value` in the v2 validator in place.** Rejected: it would mutate the pinned v0.3
  contract and its fixture/hash guards, and blur the synthetic pilot with operational acceptance. A new,
  additive v3 envelope keeps the change reviewable and backwards compatible.
- **Depend on `cryptography` for Ed25519 verify.** Rejected: it is not importable in the stdlib governance
  lane or the research venv where TIM runs; a pure-stdlib verify keeps the governance gate dependency-free
  and matches "TIM verifies only" (verification is the only half TIM needs).
- **Trust the public key embedded in the envelope.** Rejected: trust must be pinned. The key is resolved
  from the local immutable trust store; the envelope's fingerprint is cross-checked against the pinned key
  (and is bound into the signed material, so a mismatch cannot yield a valid signature).
- **Hardware/KMS-backed custody (Design C) or minisign tooling (Design B).** Deferred to the Sponsor's GO
  #2a custody decision; they concern key custody off-repository and do not change this verification contract.

## Addendum — operational signing custody binding (GFX-TIM-GO2A-CUSTODY-BINDING-016, 2026-11-24)

**Status:** Accepted (repository-only; records the authoritative Sponsor custody decision and the read-only
host capability findings. This addendum binds host / identity / path / ACL / provider; it does **not**
create the service identity, directory, or key, and does **not** pin any public key. Operational
verification remains DARK — the committed trust store stays empty. No host mutation, no key generation,
no merge/deploy by this packet.)

**Supersedes:** the deliberately-generic custody language in the ADR-0124 runbook ("on the producer host
… host OS keychain"), which reserved host/identity/path/ACL/provider to this GO.

**Sponsor custody decision (authoritative — resolves the GFX-TIM-GO2A-OPERATIONAL-SIGNING-CUSTODY-015 host
ambiguity):**

- **Canonical TIM signer host:** the Windows bridge/producer host **`WIN-RD8VDS93DK7`** (reached as
  `guvfx-windows-mt5`).
- **Signer:** the dedicated **GuvFX Windows History Agent** (private repo
  `nunoamaral-hue/guvfx-windows-history-agent`). `mac-control-node` is orchestration/consumer only and
  must never hold the private key.
- **Dedicated service identity:** `WIN-RD8VDS93DK7\guvfx_tim_producer` (no interactive logon rights).
- **Protected local store root:** `C:\ProgramData\GuvFX\TIM\signing\` — local NTFS only; never a repo,
  NAS, SMB mount, user profile, temp directory, environment variable, registry value, clipboard, or
  shared store.
- **Private-key custody ACL:** inheritance disabled; owner `BUILTIN\Administrators`; only
  `NT AUTHORITY\SYSTEM` and `BUILTIN\Administrators` FullControl, plus
  `WIN-RD8VDS93DK7\guvfx_tim_producer` Read/ReadAndExecute as strictly required by the selected provider.
  No other ACEs.
- **Algorithm:** Ed25519. The `signing_key_id` is derived **deterministically from the public key /
  fingerprint after generation** (no preselected, misleading ID).

**Read-only host capability findings (value-blind; GFX-TIM-GO2A-CUSTODY-BINDING-016 Phase 1; no mutation,
no key generation, no secret read):**

- Host identity confirmed `WIN-RD8VDS93DK7`; `C:` is local **NTFS**, Fixed; `C:\ProgramData` present.
- **Clean pre-state:** `C:\ProgramData\GuvFX` absent, `C:\ProgramData\GuvFX\TIM\signing` absent,
  `guvfx_tim_producer` local user absent; no competing TIM key / signing service / provisioning process /
  scheduled task. (Unrelated existing services `GuvFXBetaAgent` and `GuvFXHostedExecutor` are not reused
  and not touched.)
- Runtime: Windows Server 2025, Windows PowerShell 5.1, Python 3.11.9 at `C:\Program Files\Python311`.
- **Provider gate:** `cryptography` is **not currently installed** on that Python (ModuleNotFoundError);
  no OpenSSL; no WinSW/nssm on PATH. The signer therefore targets an **in-process, audited `cryptography`
  Ed25519 provider** (stable raw/PKCS#8 key contract, no shelling, no custom signing math) behind a
  provider abstraction with a **fail-closed preflight**; installing `cryptography` on the producer host is
  a documented dependency gate for the later, separately-authorised provisioning step — it is **not** done
  here, and the signer is **not** claimed operational until it is.

**Cross-repo signing contract (locked):** the producer signer MUST sign exactly the byte-identical
`signing_material_v3` that TIM's merged verifier reconstructs — the NUL-joined, domain-separated material
`GUVFX-TIM-RELEASE-SIGNATURE-v3 \x00 algorithm \x00 release_id \x00 manifest_digest \x00
contract_bundle_digest \x00 validator_bundle_digest \x00 producer_identity \x00 signing_key_id \x00
public_key_fingerprint \x00 created_at_utc` — producing a 64-byte Ed25519 signature encoded as 128 lowercase
hex in `signature_value`, with `synthetic: false` and `algorithm: ed25519-v1`. This contract is pinned by
the cross-repo lock so the two repositories cannot drift.

**Boundary unchanged (ADR-0070):** the producer signs; TIM verifies only. The private key is generated and
held solely in the producer-host protected store under `guvfx_tim_producer`; it never enters TIM, agents,
Git, Notion, NAS, datasets, logs, argv, fixtures, or chat. Key generation and public-key pinning remain
separately Sponsor-gated and are not performed here.
