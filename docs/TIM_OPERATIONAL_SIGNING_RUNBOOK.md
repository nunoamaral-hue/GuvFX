# Runbook — TIM operational release signing (trust-store pinning, rotation, revocation)

**Subsystem:** `research/market_data/tim_release_signing_v3.py`, `tim_ed25519.py`, `tim_trust_store.py`,
`research/market_data/trust_store/operational_public_keys.json`.
**Packet:** GFX-TIM-GO2-RELEASE-FOUNDATION-014 · **ADR:** ADR-0124.
**Status:** repository tooling only — **no key is generated, held, pinned, rotated or revoked by this
packet, and no release is signed, published or consumed.** The committed operational trust store is
**empty**, so operational verification fails closed until a real public key is pinned under a Sponsor GO.
Every real step below is performed by a human/producer under its own authorisation; **no agent handles any
private key.**

## Boundary (ADR-0070, unchanged)

The **producer signs; TIM only verifies.** This repository contains the **verification** half only: a
pure-stdlib Ed25519 *verify*, a pinned *public-key* trust store, and the v3 envelope validator/verifier.
There is no signing primitive and no key-generation code here, by design. A private key, seed, password or
token must **never** appear in TIM, tests, argv, logs, fixtures, Notion or any repository.

## Default (fail-closed) behaviour

- `operational_public_keys.json` ships with `"keys": []`. Any `ed25519-v1` envelope therefore verifies to
  `unknown_signing_key` (fail closed). Synthetic releases are unaffected.
- `verify_release_signature_v3(signature, manifest_bytes, trust_store=..., require_operational=True)` rejects
  every synthetic/placeholder signature (`operational_required`). A consumer that must only accept
  operational releases passes `require_operational=True` and a non-empty trust store.

## 1. Pin a real operational public key (GO #2a — human/producer, off-repository key custody)

Prerequisite (off-repo, Sponsor-gated): the producer generated the Ed25519 key **on the producer host**
under the named custodian; the **private** seed is held in the host OS keychain (hardware-backed if
available) and is never exported. The producer distributes, out-of-band (not via the release share), only:

- the 32-byte **public key** (64 lowercase hex), and
- its **sha256 fingerprint** (64 lowercase hex), and
- a chosen `signing_key_id` (slug), and the intended validity window.

A reviewer adds one entry to `research/market_data/trust_store/operational_public_keys.json`:

```json
{
  "signing_key_id": "<slug>",
  "algorithm": "ed25519-v1",
  "public_key": "<64 hex: the 32-byte Ed25519 public key>",
  "public_key_fingerprint": "<64 hex: sha256(public_key bytes)>",
  "not_before_utc": "<YYYY-MM-DDThh:mm:ssZ>",
  "not_after_utc": "<YYYY-MM-DDThh:mm:ssZ>",
  "status": "active"
}
```

Then verify the entry is well-formed and self-consistent (the loader fails closed on any mismatch):

```bash
python3 -c "from research.market_data.tim_trust_store import load_pinned_operational_trust_store as L; s=L(); print('pinned key ids:', s.key_ids())"
```

Confirm the printed fingerprint equals `sha256(public_key)` (the loader enforces this; a mismatch raises
`TrustStoreIntegrityError`). Changing the store is a governed, reviewed commit — never an unreviewed host
edit. **Copy no private key.**

## 2. Rotate

Rotation keeps the **current and previous** keys trusted during the overlap so in-flight releases still
verify:

1. Add the new key as a second entry with a **new** `signing_key_id` and `status: active` (duplicate
   `signing_key_id` is a permanent integrity failure — the store never silently picks one).
2. The producer begins signing with the new key id.
3. After the overlap window, set the previous entry's `status: revoked` (keep it — see §3).

## 3. Revoke

Set the entry's `status` to `"revoked"` (do **not** delete it). A revoked key is positively recognised and
rejected with `key_revoked`, which is stronger than deleting it and having it look merely `unknown`. Any
release that depended on a revoked key no longer verifies.

## 4. What this runbook does NOT authorise

No key generation; no private-key handling, export or backup; no credential/NAS/SMB action; no release
build, sign, publish, mount or consume; no deployment; no production-database, Copy-Trading, Dashboard,
Withdrawal or Wayond change. Pinning/rotating/revoking a key is a reviewed Git change to the public trust
store, under its own Sponsor authorisation; the private key stays off-repository in the producer host's
custody at all times.
