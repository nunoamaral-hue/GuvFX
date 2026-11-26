# ADR-0130 — Ed25519 INGEST event verification + durable replay protection (contracts; emitter & trust pin deferred)

- **Status:** Accepted (repository-only; DARK; contract-only — the production trust registry stays EMPTY and the
  host emitter is UNPROVISIONED). Not deployed, not armed, no host contact, no real key.
- **Packet:** GFX-COPIER-INGEST-EVENT-ED25519-173
- **Date:** 2026-11-25
- **Builds on:** ADR-0074 (signed HMAC event ingestion), ADR-0101 + GFX-164 (bounded verifier window),
  ADR-0129 (owner-demo asymmetric window-open authorization — this closes its documented durable-nonce and
  INGEST-event-migration gaps as tested contracts).
- **Identity boundary:** the INGEST-event trust (`COPIER_INGEST_EVENT_TRUST`) is strictly separate from the
  Copy-Trading WINDOW-OPEN trust (`COPIER_WINDOW_OPEN_TRUST`, ADR-0129) and from the TIM release-signing trust
  (ADR-0070 / ADR-0124). No cross-import; no shared key, key-id namespace, trust store or signing domain.

## Context

The signed fast-path event-ingestion endpoint (ADR-0074) authenticates each emitted revision batch with a
SYMMETRIC HMAC key (`COPIER_INGEST_HMAC_KEY`) that both the emitter and the backend hold, resolved for a bounded
operator session from the verifier window (ADR-0101). ADR-0129 recorded the remaining gap toward a
no-symmetric-secret end state: the INGEST **event** feed itself must migrate from HMAC to an asymmetric
signature (host-private signs / backend-public verifies), and the window-open authorization's in-memory replay
guard must become durable. This ADR builds both as **standalone, fully tested contracts**, exercised only
through injected providers + synthetic keys, keeping production DARK.

## Decision

### 1. Versioned, domain-separated Ed25519 event signing (`ingest_event_signing.py`)

The HOST holds a PRIVATE Ed25519 key and signs each INGEST event; the BACKEND holds only the corresponding
**pinned PUBLIC** key and verifies it — no secret bytes cross the boundary, and a backend compromise forges
nothing. A valid event is accepted ONLY when it is signed by a pinned public key AND binds, exactly, to: the
configured **signer** (`key_id`), the tenant **owner** (`owner_id`), the governed **#46 → #48** route, the
**`INGEST`** purpose, the **authorized window** (`window_not_after_epoch`), its own **expiry** (`issued_at_epoch`
freshness + `expiry_epoch`), a single-use **nonce**, and the **payload** (`payload_digest`).

- **Versioning:** `protocol_version = "copier-ingest-event-ed25519-1"`, `alg = "ed25519"`.
- **Domain separation:** the signing input is `INGEST_EVENT_DOMAIN || "\n" || canonical(signed-fields)`, where
  `INGEST_EVENT_DOMAIN = b"guvfx.copier.ingest-event.v1"`. The domain prefix is bound into every signature, so a
  signature from the window-open domain (which has no domain prefix) or TIM can never verify as an event, and
  vice-versa. A test asserts a non-domain signature over the same field set is rejected `bad_signature`.
- **Authorized-window binding (read-only).** The live INGEST window is **uncapped** (bounded only by TTL —
  `covers_live_authorization` refuses a use-capped window, because a cap would lapse mid-lifecycle and strand an
  open position). The event therefore binds to the window by its `not_after` boundary: verification requires an
  OPEN INGEST window whose `not_after` **equals** the event's `window_not_after_epoch` and is still in the future,
  reading window METADATA only (never the key value / tmpfs path) and burning **no** window use. An event may not
  outlive its window (`expiry_epoch ≤ window_not_after_epoch`).
- **Expiry:** freshly issued (`issued_at` within `max_issue_age_s`, not implausibly future), not past
  `expiry_epoch`, bounded TTL (≤ 600 s).
- **Pinned identity / revocation / rotation:** resolved from `COPIER_INGEST_EVENT_TRUST`
  (`{key_id: base64_raw_public_key}`). Revocation = un-pin the `key_id` (⇒ `unknown_key_id`); rotation = pin a new
  `key_id` and drop the old. **DARK by default:** the registry is EMPTY, so every event fails closed
  `unknown_key_id`.
- **Fail-closed + value-blind:** the verdict is a stable reason code, never a key, signature or secret. Ordering:
  structure → alg/protocol → pinned identity → signature → binding → authorized window → expiry → payload digest
  → single-use replay.

### 2. Fail-closed algorithm dispatcher (`ingest_envelope_dispatch.py`)

The dispatcher selects the verification algorithm **once**, from the envelope's declared `alg`, then calls only
that verifier:

- `alg = "ed25519"` → the Ed25519 verifier. **Any failure is terminal — it NEVER falls back to HMAC.** A test
  asserts the HMAC verifier is not even invoked when the Ed25519 verifier raises.
- `alg = "hmac"` / absent (the ADR-0074 shape) → the legacy HMAC verifier `event_ingestion.verify_ingest_request`,
  but **only while the explicitly-configured legacy path is enabled** (`copier_ingest_hmac_legacy_enabled`,
  default ON to preserve existing behaviour; an operator may set `COPIER_INGEST_HMAC_LEGACY_ENABLED=0` to refuse
  the symmetric path once emitters migrate, after which HMAC fails closed `hmac_legacy_disabled`). A test proves
  a routed HMAC envelope yields byte-for-byte what a direct `verify_ingest_request` yields.
- anything else → `unknown_alg` (fail closed).

### 3. Durable single-use replay guard (`durable_nonce.py`)

A durable replay guard reusing the EXISTING `CopierIngestNonce` table (ADR-0074) — **no schema change**. One row
per `(domain, scope, nonce)`; the table's UNIQUE constraint makes a replay (including a concurrent one) fail
closed, and the row's durability makes single-use hold across a restart. The stored token is
`sha256(domain, scope, nonce)` (value-blind — the raw nonce is never stored) with a short per-domain prefix, so
the three namespaces (window-open / ingest-event / legacy-HMAC-raw) are disjoint and the token never overflows
the `varchar(80)` column. Any persistence fault is treated as a replay (fail closed). Two consumers:

- `durable_window_open_nonce_store()` — the DURABLE drop-in for the ADR-0129 `NonceStore`, so the window-open
  authorization is single-use across concurrent requests AND restarts (closes ADR-0129 gap #2); and
- `durable_ingest_event_nonce_burn()` — the event verifier's `(nonce, expiry) -> bool` burn.

## No migration

`CopierIngestNonce` is reused as-is (generic single-use burn table): its columns (`nonce` unique `varchar(80)`,
`expires_at`, `created_at`) satisfy the contract. Only its module docstring is unchanged in meaning (a generic
burn store). **No model field, index or constraint changes; `makemigrations` produces nothing.** A dedicated
table was considered and rejected as unnecessary schema churn for an identical access pattern; if a future packet
wants separate retention policies per domain, that is the point to introduce one, with documented
migration/compatibility/rollback.

## Integration gap (exact — NOT built here; each separately Sponsor-gated, with its owning component)

1. **Host emitter (owner: the host MT5 copier emitter / `scripts` bridge + `bridge_emitter.py`).** No emitter
   produces Ed25519 events yet; the bridge still signs HMAC. Migrating the emitter to sign the ADR-0130 envelope
   with a host-private key is a host rollout.
2. **Endpoint wiring (owner: `event_ingestion_api.SignedEventIngestView`).** The live view still calls
   `verify_ingest_request` (HMAC) directly. Routing it through `verify_ingest_envelope` — resolving the bound
   owner/route for an `alg:"ed25519"` envelope and the durable event burn — is the wiring step; until then the
   Ed25519 path is a tested contract, reachable only in tests. The legacy HMAC path is unchanged.
3. **Production trust pin + flag (owner: operator / Sponsor).** Pinning a real `COPIER_INGEST_EVENT_TRUST` public
   key and enabling `COPIER_INGEST_EVENT_VERIFICATION_ENABLED` (and, when ready, disabling the HMAC legacy path).
4. **Host verifier-window opener (owner: ADR-0129 host rollout).** Unchanged and still UNPROVISIONED — the
   event path binds to a window but does not open one.
5. **Signed read-only #48 admission reader (owner: ADR-0129 / the host admission interface).** Unrelated to event
   signing; still UNPROVISIONED.
6. **Live #46 → #48 demonstration (owner: Sponsor).** A separate, head/runtime-pinned real-money GO.

## Consequences

- DARK by default: `COPIER_INGEST_EVENT_TRUST` is EMPTY (every event ⇒ `unknown_key_id`),
  `COPIER_INGEST_EVENT_VERIFICATION_ENABLED` is OFF, no emitter signs Ed25519, and the production endpoint's
  behaviour is byte-identical to ADR-0074 (HMAC legacy preserved, default ON).
- Migration-free; no new model; no host contact; no real key or window. Backend Ed25519 verification uses the
  `cryptography` library (the TIM lane's stdlib verify-only module is untouched and un-imported).
- A minor, deliberate duplication: the event domain carries its own local `_ed25519_verify` rather than importing
  the window-open module's, to keep the two signature domains decoupled.
- Follow-up (separate packets): the host emitter migration (1), the endpoint wiring (2), and the trust/flag
  rollout (3).
