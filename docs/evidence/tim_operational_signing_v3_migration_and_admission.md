# TIM operational release-signing v3 — migration/compatibility analysis & first-release admission checklist

**Packet:** GFX-TIM-GO2-RELEASE-FOUNDATION-014 · **ADR:** ADR-0124 · **Date:** 2026-11-24 ·
**Mode:** repository-only, additive, migration-free, operational path dark-by-default.

This document is the governed, value-blind analysis accompanying the v3 operational signing subsystem. It
records (1) what changed and what explicitly did **not**, (2) backward/forward compatibility, (3) the
security-control matrix, (4) a documented limitation, (5) the producer-side `GuvFXData` / `GUVFX_DATA_ROOT`
evidence contract, and (6) the first-release admission checklist. It makes **no** claim that the private
producer repository, exporter, data root, NAS share or operational key are present or verified — those are
unverifiable from this checkout and are marked as such (RULE 11).

## 1. What changed / what did NOT change

**Added (new files only):**

- `research/market_data/tim_ed25519.py` — pure-stdlib Ed25519 **verify** (RFC 8032). No signing, no keygen.
- `research/market_data/tim_trust_store.py` — pinned public-key trust store, deterministic key-id
  selection, fail-closed integrity.
- `research/market_data/trust_store/operational_public_keys.json` — committed **empty** operational store.
- `research/market_data/tim_release_signing_v3.py` — `dataset_release_signature_v3` validator + verifier.
- `tests/fixtures/tim_release_signing_v3/*` — ephemeral-test-key fixtures (public material + signatures).
- `tests/test_tim_release_signing_v3.py` — 50 stdlib tests (wired into governance + CI governance job).
- `docs/architecture/ADR-0124-*.md`, this document, and
  `docs/TIM_OPERATIONAL_SIGNING_RUNBOOK.md`.

**Explicitly NOT changed (no migration, no replacement):**

- The byte-frozen v0.2 trio `contracts.py`, `tim_contracts.py`, `tim_fixtures.py` (their
  `FROZEN_PY_SHA256` guard in `test_tim_contract_v0_3.py` still holds).
- `tim_contracts_v0_3.py` and its `dataset_release_signature_v2` validator/verifier, the pinned v0.3 schema
  set (`len == 11`), and the committed v0.3 fixture dir (`len == 11`).
- Every committed v0.2 / v0.3 fixture; the Parquet candle contract; the smokes.
- No database migration, no backend code, no frontend, no deployment, no workflow behaviour change, no
  Copy-Trading / Dashboard / Withdrawal / Wayond / production artefact.

## 2. Compatibility

- **Backward compatible.** The synthetic placeholder (`synthetic-placeholder-v0`, `synthetic: true`) remains
  fully valid for synthetic releases; the v3 synthetic path signs/verifies over the same identity bindings
  as v2 (plus the v3 domain separator, algorithm and `created_at_utc`). Existing v2 envelopes continue to be
  validated by the unchanged `validate_release_signature_v2`. v3 is a **new** envelope, not a rewrite of v2.
- **Forward compatible / algorithm-agile.** Adding a future operational algorithm is a closed-world
  extension: register it in `KNOWN_ALGORITHMS` + `_SIG_VALUE_HEXLEN`, add its verify branch, and pin keys of
  that algorithm in the trust store. Unknown algorithms are rejected at validation until then.
- **Operational dark-by-default.** With the committed empty trust store, any operational (`ed25519-v1`)
  envelope fails closed with `unknown_signing_key`. No behaviour becomes live on merge; pinning a real key
  is a separate out-of-band Sponsor step.

## 3. Security-control matrix (all fail-closed, all tested)

| Threat | Control | Reason code |
| --- | --- | --- |
| Malformed / mislabelled envelope | `validate_release_signature_v3` (exact keys, consts, per-algorithm hex length, synthetic⇔algorithm) | `invalid_signature_envelope` |
| Manifest tamper | `sha256(manifest_bytes) == manifest_digest` | `manifest_digest_mismatch` |
| Envelope lifted onto another manifest (replay) | manifest digest + identity bound in signed material; K-7 manifest cross-bind | `identity_mismatch` |
| Algorithm relabel (downgrade) | algorithm bound in signed material + per-algorithm length + synthetic⇔algorithm | `invalid_signature_envelope` |
| Cross-version signature reuse | distinct v3 domain separator bound in signed material | `invalid_signature_envelope` |
| Synthetic/placeholder where operational required | `require_operational=True` | `operational_required` |
| Unknown / untrusted key | pinned trust-store resolution (empty default) | `unknown_signing_key` |
| Revoked key | entry `status: revoked` kept and rejected | `key_revoked` |
| Swapped public key | pinned key fingerprint cross-checked (also bound in material) | `key_fingerprint_mismatch` |
| Key used outside validity | pinned window vs signed `created_at_utc` | `key_not_valid_at_created_at` |
| Forged / altered signature | Ed25519 verify against pinned key; strict: non-canonical `S ≥ L`, non-canonical point encoding, and small-order `A`/`R` all rejected | `signature_invalid` |
| Crafted signature against a degenerate/low-order key | verifier rejects small-order `A`/`R`; trust store refuses to pin a low-order key (`is_acceptable_public_key`) | `signature_invalid` / pin-time `TrustStoreIntegrityError` |
| Operational verify with no trust store | required for operational algorithms | `trust_store_unavailable` |
| Duplicate pinned key id | `TrustStoreIntegrityError` at load (never silently picks one) | — |

Operational consumers should call `verify_operational_release_signature_v3` (hard-codes
`require_operational=True`, trust store mandatory) rather than the general verifier, so the non-cryptographic
synthetic path can never be mistaken for an operational signature.

Errors are value-blind: a fixed label only, never key bytes, signature bytes, paths or secrets.

## 4. Documented limitations (RULE 5 — intent vs implementation kept distinct)

- `created_at_utc` **is** bound into the signed material (so the signing time is tamper-evident), but the
  `key_validity` **window object** is not. The authoritative validity decision is therefore: the key is
  present and `active` in the pinned trust store, and the signed `created_at_utc` lies within the pinned
  window. Rotation and revocation are operator actions on the pinned store.
- `signed_manifest_path` is validated structurally and cross-checked against the manifest under K-7, but is
  not bound into the signed material; the `manifest_digest` (= sha256 of the manifest bytes) and the
  `release_id` are the authoritative identity binding.
- The K-7 manifest cross-bind runs only when the parsed manifest is supplied to the verifier. The genuine
  signature already binds the manifest digest and all identity fields, so this is defence-in-depth against a
  careless/malicious producer; the operational admission path should nonetheless pass `manifest=`.

These are stated so a later reader does not assume more is signed than is. None is a forgery path against a
legitimately pinned full-order key; the operational path is dark by default (empty store).

## 5. Producer-side `GuvFXData` / `GUVFX_DATA_ROOT` evidence contract (verification spec only — NOT verified here)

The operational first release requires producer-side storage readiness. The repository already carries the
**value-blind** storage-gate validator `scripts/check_data_root.py` (tested by `tests/test_data_root.py`).
Its contract — which the producer must satisfy and return as a value-blind PASS — is:

- `GUVFX_DATA_ROOT` is set to an **absolute, off-repository** path on the controller (no repo fallback, no
  default; fail-closed if unset).
- The target is a dedicated data root: a directory named `GuvFXData` **or** containing a `.guvfxdata`
  marker.
- It is **outside any git working tree** (no `.git` at the root or any ancestor).
- It is **atomically writable** (create + `os.replace` probe succeeds).
- It has **≥ 1 GiB** free.
- The validator output **never** contains the path, NAS host, share, username or credential — only boolean /
  logical labels.

**Status from this checkout (unverified / BLOCKED, not asserted as fact):** the real-data producer and its
exporter live in a **separate private repository** plus a controller reading the Windows MT5 host; they are
**not in this checkout**. Current authoritative handoffs record `GUVFX_DATA_ROOT` / `GuvFXData` as **absent
on the controller** and the first durable raw object (P5) as **BLOCKED** at the storage gate (owner action
GFX-PKT-006D-S1). No exporter is run, no data root is provisioned or read, and no NAS is mounted by this
packet. The only contrary "done" claim is a stale `PROGRAMME_STATE.md` the repo's own backlog flags for
reconciliation; it is treated as historical, not authoritative.

## 6. First governed release (GO #2) admission checklist

Each item is a separate, Sponsor-gated, mostly off-repository step. This packet delivers **only** item A
(repository-side) and the verification tooling; it performs none of the others.

- [x] **A. Repository contract (this packet).** v3 algorithm-agile envelope + pure-stdlib Ed25519 verify +
  pinned trust store + fail-closed controls + tests, merged under governance (auto-merge OFF; Sponsor merge
  GO pending).
- [ ] **B. Operational key custody (GO #2a, off-repo).** Sponsor selects algorithm (Ed25519) + custodian;
  the private key is generated and held **on the producer host** (OS keychain / hardware-backed if
  available); never in TIM, tests, argv, logs, fixtures, Notion or any repository.
- [ ] **C. Trust anchoring.** The producer distributes the **public key + sha256 fingerprint**
  out-of-band; it is pinned into `operational_public_keys.json` with a `signing_key_id`, validity window and
  `status: active`. (This repository then stops failing closed for that key id.)
- [ ] **D. Producer storage readiness (Blocker 2 / GFX-PKT-006D-S1).** Provision `GuvFXData`, set
  `GUVFX_DATA_ROOT`, install the producer NAS credential in the host keychain, open the governed
  producer-only write path, return a value-blind storage-gate PASS (§5).
- [ ] **E. Timezone evidence.** Winter offset is VERIFIED (TradersWay-Demo = UTC+2 / 7200 s, ADR-0121);
  summer UTC+3 separate. A release's `timezone_evidence` must reference the season-correct VERIFIED entry.
- [ ] **F. First synthetic release across the real boundary.** The producer builds, signs (operational
  Ed25519) and seals (READY last) one **synthetic** v0.3/v3 release; TIM performs one read-only mount and
  verifies end-to-end (signature + identity + evidence) before any real-data release is considered.
- [ ] **G. First real-data release.** Only after A–F pass with evidence, under a distinct Sponsor GO.

No item beyond A is performed, authorised or claimed here.
