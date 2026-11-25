# ADR-0129 — Owner-demo asymmetric window-open authorization + #48 admission probe (contracts; opener deferred)

- **Status:** Accepted (repository-only; DARK; contract-only — the production opener and the #48 admission reader
  stay UNPROVISIONED). Not deployed, not armed, no host rollout, no real key.
- **Packet:** GFX-COPIER-OWNER-DEMO-ASYM-VERIFIER-172
- **Date:** 2026-11-25
- **Builds on:** ADR-0128 (owner-demo execution wiring — this closes its two named gaps as tested contracts),
  ADR-0117 (signed host executor), ADR-0101 + GFX-164 (bounded verifier window), ADR-0057 (live authorization).
- **Identity boundary:** the Copy-Trading window-open trust here is strictly separate from the TIM
  release-signing trust (ADR-0070 / ADR-0124). No cross-import; no shared key, key-id namespace, or trust store.

## Context

ADR-0128 shipped the owner-demo actuator wiring but left two components unprovisioned and documented as remaining
integration: (a) the host-side verifier-window **opener** (because the interim path would have streamed a shared
**symmetric** ingest key host→backend), and (b) the **#48 admission probe** (`host_admission_probe` was `None`
because there was no value-blind, authoritative follower read-back). This ADR builds both as **standalone, fully
tested contracts** exercised only through injected providers + synthetic keys, keeping production DARK.

## Decision

### 1. Host-private / backend-public window-open authorization (`verifier_window_authorization.py`)

The HOST holds a PRIVATE Ed25519 key and signs a bounded "open this INGEST window" attestation; the BACKEND holds
only the corresponding **pinned PUBLIC** key and verifies it. **No secret bytes cross the boundary**, and a backend
compromise can forge nothing. A valid attestation is accepted ONLY when it is signed by a pinned public key AND
binds exactly to the tenant **owner**, the governed **#46 → #48** route, the **`INGEST`** purpose, and a **bounded
window** whose not-after outlives the live authorization. The pinned public identity is resolved from the
Copy-Trading-only `COPIER_WINDOW_OPEN_TRUST` registry (`{key_id: base64 raw public key}`), **never** the TIM trust
store.

- **Expiry:** the attestation must be freshly issued (`issued_at` within `max_issue_age_s`, not implausibly future)
  and the authorized window's `not_after` must be `> now` AND `≥ the authorization's not_after` (mirrors
  `covers_live_authorization`).
- **Replay:** a single-use `nonce` scoped to the route is consumed only after full validation; a repeat is
  `REPLAYED`. (Default nonce store is in-memory; see the gap below.)
- **Revocation:** remove the `key_id` from `COPIER_WINDOW_OPEN_TRUST` ⇒ the provider returns `None` ⇒
  `INVALID_IDENTITY`. **Rotation:** pin a new `key_id`→public key and drop the old. A backend-side registry change
  only; the host private key need not move.
- **Teardown:** closing the verifier window (`verifier_window.close_all`) ends the authorized session; the nonce is
  already spent, so the attestation cannot re-open a window.
- **Fail-closed + value-blind:** the verdict is reason codes + booleans + bound facts only — never a key, a
  signature, or any secret. Ordering is structure → pinned identity → signature → binding → expiry → replay.

### 2. #48 follower admission / identity / flatness probe (`follower_admission_probe.py`)

`evaluate_follower_admission(reader, …)` reads a reviewed, read-only evidence provider and returns a value-blind
verdict. It passes ONLY on **fresh, authoritative** evidence that the follower is the **intended identity**, is
**flat** (no open positions), and has live **admission OFF**. It **fails closed** on `EVIDENCE_UNAVAILABLE`,
`EVIDENCE_STALE`, `EVIDENCE_AMBIGUOUS`, `FOLLOWER_IDENTITY_MISMATCH`, `FOLLOWER_NOT_FLAT`, or `ADMISSION_NOT_OFF`.
It is wired as the owner-demo `host_admission_probe` (the arm consumes `{off}`), so the arm's host-admission entry
gate cannot pass without this evidence. The reader makes no mutation and no order action. **Prepare performs no
host call** (unchanged) — the probe is consulted only at the confirm/arm boundary.

## Integration gap (exact — NOT built here; each separately Sponsor-gated)

1. **The production opener is not wired to the contract.** Today the INGEST feed is authenticated by a **symmetric
   HMAC window key** (`verifier_window.claim_key`). This contract authorizes *who may open the window*, but the full
   no-symmetric-secret end state additionally requires migrating INGEST **event** verification from HMAC to Ed25519
   (host-private signs events / backend-public verifies). Until that migration, the opener stays UNPROVISIONED and
   `build_demo_actuators` keeps `verifier_window_opener = None`.
2. **Durable nonce store.** The default replay guard is in-memory (single process). A production opener needs a
   durable single-use store (DB/cache) surviving restarts and shared across workers.
3. **The #48 admission reader.** A reviewed signed read-only host interface returning follower identity / flatness /
   admission-off + a fresh `observed_at` is not yet built; `production_follower_reader` returns
   `EVIDENCE_UNAVAILABLE`, so the probe fails closed.
4. **Trust provisioning + flags + the live GO.** Pinning a real `COPIER_WINDOW_OPEN_TRUST` public key, enabling the
   execution flag + host executor, and the live #46→#48 demonstration each remain separate, Sponsor-gated steps.

## Consequences

- DARK by default: `COPIER_WINDOW_OPEN_TRUST` is EMPTY (every verify ⇒ `INVALID_IDENTITY`), the production admission
  reader is UNAVAILABLE, and the opener stays `None` ⇒ `confirm` remains fail-closed exactly as after ADR-0128.
- Migration-free; no new model; no host contact; no real key or window. Backend lane uses `cryptography` for
  Ed25519 verification (the TIM lane's stdlib verify-only module is untouched and un-imported).
- Follow-up (separate packets): the host rollout (opener + admission reader + durable nonce store) and the INGEST
  event-signing asymmetric migration.
