# ADR-0019 — Customer credential storage & lifecycle mechanism

- **Status:** **ACCEPTED — Sponsor (Nuno), 2026-07-27** (amended with the formal secret classification below at the
  Sponsor's request). This was a RED credential-security-posture decision (governance overlay: *credential / security
  posture → Sponsor approval*; `.claude/rules/architecture.md`: *no silent architecture replacement — requires an
  approved decision before merge*); it is now **ratified**. Implementation may proceed under the normal pipeline
  (design → tests → review → make check → CI → merge), including the credential re-encryption migration.
- **Date:** 2026-07-27 · **Phase:** Hosted MVP Completion Programme, Phase 3.
- **Relates to:** [HOSTED-MVP-CREDENTIAL-LIFECYCLE.md](../HOSTED-MVP-CREDENTIAL-LIFECYCLE.md) (threat model + lifecycle),
  [HOSTED-MVP-BASELINE.md](../HOSTED-MVP-BASELINE.md), SEC-CRYPTO-001, SECRET_INVENTORY Gaps 1 & 7, `.claude/rules/security.md`.
- **Scope:** the mechanism by which a **customer broker credential** is stored, encrypted, keyed, rotated, and destroyed.
  Not the runtime lifecycle (certified, ADR-0018) and not worker/bridge secrets (RULE 3 / core.credentials).

## Context
Today two divergent models (`Mt5Credential`, production `TradingAccount`) Fernet-encrypt the broker password at rest,
but: the `TradingAccount` key falls back to `sha256(DJANGO_SECRET_KEY)` (SEC-CRYPTO-001 — rotating the secret key
destroys all credentials); keys sit inline in `docker-compose.yml` (Gap 1); the production path transmits the password
**plaintext** to the Windows agent and writes it **decrypted** to a handoff file; and there is **no** per-customer key
boundary, rotation, revocation, verified destruction, or customer-credential audit. The certified beta
`terminal_provisioning` path, by contrast, is deliberately **credential-free**.

## Secret classification (Sponsor-ratified, 2026-07-27)
Every secret in the platform belongs to exactly ONE of three classes; storage, key boundary, lifetime, rotation and
audit obligations differ by class:

- **Customer Secrets** — secrets the CUSTOMER owns and the platform holds only in trust: the broker **password** and any
  customer-supplied broker credential. Highest sensitivity. Per-customer encryption boundary; never shared; never reused
  across VPSs; redacted in all display / logs / evidence; auditable operator access; verified destruction on
  offboarding. **A broker password is an ONBOARDING ARTEFACT, not a long-lived operational secret — its operational
  lifetime MUST be minimised.** It is required only to establish the broker session; once the customer's runtime holds
  its own broker session the password should be **evicted and not retained**. The strongest minimisation is not holding
  it at all (Option A strategic: direct onboarding into the customer's `/portable` runtime). Any period the platform
  retains a broker password is a deliberate, time-bounded exception, not the steady state.
- **Platform Secrets** — secrets the PLATFORM owns: encryption key material (`GUVFX_FERNET_KEY`, key ids), agent/bridge
  auth tokens, worker secrets, `DJANGO_SECRET_KEY`, the signed-lifecycle HMAC. Governed by RULE 3 (own secret, fail
  closed, no substitution), the secret inventory, and Sponsor-held rotation. They must be **decoupled from one another**
  — in particular the Customer-Secret encryption key must NOT derive from `DJANGO_SECRET_KEY` (SEC-CRYPTO-001).
- **Runtime Secrets** — secrets that exist on the customer VPS as a consequence of running: the terminal's own persisted
  broker session state, DPAPI-protected material, the auto-logon LSA secret (ADR-0018 §4 R1). Bounded to the VPS; never
  transit the control plane; destroyed with the runtime (`TOMBSTONE`/`RELEASE` + credential wipe).

**Organising principle:** Customer Secrets minimise operational lifetime and prefer never-held; Platform Secrets are
decoupled and Sponsor-rotated; Runtime Secrets stay on the box and die with it. This taxonomy governs the rest of this
ADR and [the lifecycle design](../HOSTED-MVP-CREDENTIAL-LIFECYCLE.md).

## Options
**A — Windows DPAPI (per-VPS, credential lives on the VPS).** The broker credential is protected by DPAPI on the
customer's Windows VPS; the Linux backend never holds the plaintext. Pros: OS-native, key never leaves the box, natural
per-VPS boundary, realises the packet's *"direct onboarding into the customer's runtime"* preference (password never
transits the platform). Cons: **DPAPI is Windows-only** — the Linux backend cannot decrypt, so this only works if the
credential is *not* stored in the backend DB; that is a larger architectural move (credential ownership shifts backend →
VPS) and does not fit the *current* backend-holds-credential production path without reworking intake/handoff.

**B — External secret-management platform (Vault / cloud KMS).** Rejected: the packet forbids a new external secret
platform without a justified need, it is operationally heavy for < 10 customers, and it adds an aggregation point.

**C — Improved application-level envelope encryption (in-repo, evolves the existing Fernet scheme).** Keep Fernet but:
(1) **decouple the key from `DJANGO_SECRET_KEY`** (require an explicit `GUVFX_FERNET_KEY`, fail closed if unset — closes
SEC-CRYPTO-001); (2) add an explicit **key id** per ciphertext + a **per-customer key boundary**; (3) **MultiFernet**
rotation (add-new-key / re-encrypt / retire-old, never a hard swap); (4) move key material **out of compose** into the
VPS secret store; (5) extend `log_credential_event` to customer creds (intake/access/rotation/revocation/destruction,
redacted, append-only); (6) **verified destruction** = crypto-shred the customer key id + delete the row + destruction
evidence. Pros: in-repo, no new platform, fits the current backend-holds-credential path, directly closes the OPEN
SEC-CRYPTO-001 + Gap 1/7, and unifies the two models behind one source of truth. Cons: the backend still holds
decryptable credentials (mitigated, not eliminated — the strongest protection is not holding them at all).

## Decision (proposed)
1. **Near-term (this programme): Option C.** It is the smallest, in-repo change that closes the documented credential
   risks and delivers the full Phase-3 lifecycle (rotation/revocation/audit/verified-destruction) for the credentials
   the backend holds today.
2. **Strategic direction: Option A.** Steer the architecture toward **direct onboarding into the customer's governed
   `/portable` runtime** so the platform **holds no broker password** (the certified beta path already proves a
   credential-free runtime). Backend-held credentials become the *exception* (a controlled, separately-reviewed
   migration), and where a credential must live on the VPS it is DPAPI-protected. This is the target; it is not built
   in this phase.
3. **Do NOT** adopt Option B (no new external secret platform).

## Consequences
- **Migration:** existing `TradingAccount.password_enc` / `Mt5Credential` ciphertext must be re-encrypted under an
  explicit `GUVFX_FERNET_KEY` + key id (MultiFernet, downtime-free). This touches **all stored customer credentials** —
  a security-critical migration requiring a rehearsed, reversible plan and Sponsor sign-off.
- **Backend still decrypts** to feed the current send-to-agent path (T2/T3 only fully close under the strategic Option A).
- **Requirements met by C:** per-customer boundary, no reuse across VPSs, no secrets in Git/logs/evidence, redacted
  display, audit, rotation, verified destruction. Backup governance is Phase 8.

## Why this is a Sponsor gate (not self-accepted)
It changes the security posture of the **highest-sensitivity asset** (customer broker passwords), rewrites the
encryption/key model, and performs a **migration over all existing credentials** intersecting an OPEN security finding
(SEC-CRYPTO-001). Under the governance overlay this is **RED**. Implementation (code + migration) is therefore
**blocked on Sponsor acceptance of this ADR**. Until then, the credential path is unchanged.

## Acceptance asks (for the Sponsor)
- Accept **Option C near-term + Option A strategic** (or return with a different steer).
- Approve proceeding to the Phase-3 **implementation** increments (envelope/key-id, MultiFernet rotation, customer-cred
  audit, verified destruction) under the normal pipeline, including the credential re-encryption migration.
