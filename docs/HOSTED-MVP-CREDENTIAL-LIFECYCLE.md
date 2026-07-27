# HOSTED-MVP-CREDENTIAL-LIFECYCLE — threat model + lifecycle design

- **Status:** DESIGN v1.0 (Phase 3 design deliverable) · **Date:** 2026-07-27 · **Owner (lifecycle):** Sponsor (Nuno)
- **Authority:** operational baseline is [HOSTED-MVP-BASELINE.md](HOSTED-MVP-BASELINE.md); the storage-mechanism
  decision is proposed in **[ADR-0019](ADRs/0019-credential-storage-mechanism.md)**. This document is the credential
  **threat model + target lifecycle**; the storage/encryption **implementation** is a **RED credential-security
  posture change** that requires Sponsor approval of ADR-0019 **before any code lands** (governance overlay + `.claude/rules/architecture.md`).
- **Scope discipline:** design for < 10 hosted customers; no new external secret-management platform without a
  justified ADR; prefer the simplest mechanism that meets the requirements.

---

## 0. Current state (grounded, not assumed)
Two divergent credential models exist: `Mt5Credential` (per-user, key `MT5_CRED_FERNET_KEY`) and the production
`TradingAccount` (per-account, key `GUVFX_FERNET_KEY` **falling back to `sha256(DJANGO_SECRET_KEY)`**). Passwords are
Fernet-encrypted **at rest** (`backend/trading/crypto.py`, `backend/mt5/crypto.py`). Known, documented gaps:
- **Plaintext on the wire + on disk (production path):** the password is POSTed to the Windows agent at intake
  (`backend/trading/views_account_add.py`) and written **decrypted** to a `0600` `launch_account.json` on the handoff
  mount (`backend/mt5/views.py`). The beta `terminal_provisioning` path is deliberately **credential-free**
  (`mgmt_client.configure` is a no-op; `PROVISIONING_REQUIRE_BROKER_LOGIN` defaults OFF).
- **Key coupling** to `DJANGO_SECRET_KEY` (SEC-CRYPTO-001 / SECRET_INVENTORY Gap 7): rotating the secret key silently
  makes stored credentials undecryptable.
- **Keys inline in `docker-compose.yml`** (SECRET_INVENTORY Gap 1).
- **No** customer-credential rotation, revocation, verified destruction, or audit (`log_credential_event` is wired to
  `WorkerIdentity` only).
- **No paper tier** — `is_demo` is a self-declared boolean, `BrokerServer.environment` is not cross-checked.

## 1. Assets, actors, trust boundaries
**Secret classification (Sponsor-ratified — see [ADR-0019 §Secret classification](ADRs/0019-credential-storage-mechanism.md)).**
Every secret belongs to exactly one class: **Customer Secrets** (the broker **password** + customer broker
credentials — highest sensitivity, held only in trust), **Platform Secrets** (encryption keys, agent/bridge tokens,
`DJANGO_SECRET_KEY`, the lifecycle HMAC), and **Runtime Secrets** (the VPS-local terminal broker session, DPAPI
material, the auto-logon LSA secret). **A broker password is an ONBOARDING ARTEFACT, not a long-lived operational
secret — its operational lifetime MUST be minimised** (established for the broker session, then evicted; strongest
form is never held: direct onboarding into the customer's runtime). The **encryption key(s)**, **audit trail**, and
**backup copies** are governed per class.
**Actors:** customer (owns the broker relationship); GuvFX **operator** (custody + runtime ops); the **backend**; the
per-VPS **agent/bridge**; the MT5 **terminal**; the **Sponsor** (approves credential/security posture). Per
HOSTED-MVP-BASELINE §4 the tenancy boundary is the VPS; per `.claude/rules/security.md` research/paper/live
credentials are **distinct and non-interchangeable**, and least privilege + per-account isolation are preserved.

## 2. Threats (STRIDE-style, focused)
| # | Threat | Current exposure | Target mitigation (design) |
|---|---|---|---|
| T1 | Password disclosure at rest (DB dump) | Fernet-encrypted, but key coupled to `DJANGO_SECRET_KEY` | envelope encryption with an explicit, rotatable key id; decouple from `DJANGO_SECRET_KEY` (ADR-0019) |
| T2 | Password disclosure in transit (backend→agent) | POSTed plaintext at intake | prefer **direct onboarding into the customer's `/portable` runtime** (customer/broker-side login), so the password never transits the platform; where a controlled migration is unavoidable, a dedicated separately-reviewed secure channel (never the signed control channel) |
| T3 | Password disclosure on disk (handoff file) | decrypted `launch_account.json` (0600) on a shared-family mount | per-VPS-scoped, short-lived, wiped-after-use; ideally the terminal holds its own broker session and the platform never writes the password |
| T4 | Key compromise → mass decryption | keys inline in compose; one key for all customers | per-customer encryption boundary (key id per customer); keys out of compose into the VPS secret store |
| T5 | Rotation-time silent data loss | `DJANGO_SECRET_KEY` fallback | key-id + MultiFernet-style re-encryption; rotation never hard-swaps |
| T6 | Credential reuse across VPSs | shared DB/keys | no credential reuse across VPSs; per-customer key boundary (baseline §4) |
| T7 | Wrong-classification use (demo cred on live / vice versa) | `is_demo` self-declared, not broker-verified | broker-truth classification (bridge P2-A already refuses cross-classification at execution); intake cross-check `BrokerServer.environment` vs `is_demo`; **live requires explicit human activation** (baseline §3) |
| T8 | Undetectable operator access / no audit | no customer-cred audit | `log_credential_event` extended to customer creds: intake / access / rotation / revocation / destruction, redacted, append-only |
| T9 | Credential survives offboarding (backup/DB) | plain DB delete; no crypto-shred; ungoverned backups | verified destruction (crypto-shred key material + row) with destruction evidence; backups explicitly governed (Phase 8) |
| T10 | Leak in logs/evidence/screenshots | rules forbid it; provisioner claims no-secret evidence | keep the no-secret evidence invariant; redacted display (suffix only); never log/print a password |

## 3. Target lifecycle (14 stages → design)
1. **Intake** — prefer **direct onboarding into the customer's governed `/portable` runtime** (T2): the broker login
   happens against the customer's own runtime, so GuvFX ideally never holds the plaintext password. Where the platform
   must accept a password (compat with the current `TradingAccount` path), it is captured over TLS, never logged, and
   encrypted immediately with the customer's key id.
2. **Account verification** — verify the login is real + the classification matches broker truth (`account_info().trade_mode`)
   before activation; do not rely on the self-declared `is_demo` alone (T7).
3. **Demo / paper / live classification** — three distinct classes (baseline §3), cross-checked at intake and enforced
   at execution (bridge P2-A). **Live requires an explicit human-gated activation state** — never auto-enabled on a
   successful connect.
4. **Secure storage** — envelope encryption, explicit key id, per-customer key boundary, key material out of Git/compose
   into the VPS secret store (ADR-0019). No plaintext at rest.
5. **Runtime provisioning** — the certified per-slot `/portable` lifecycle (unchanged).
6. **Terminal authentication** — prefer the terminal holding its own broker session; if the platform supplies the
   password, via a dedicated, separately-reviewed secure path (never the signed control channel), short-lived, wiped.
7. **Broker reconnect** — unattended reconnect from the terminal's own persisted state (certified); no password re-transit.
8. **Rotation** — per-customer password rotation flow + key-id rotation (MultiFernet-style, never a hard swap), audited.
9. **Logout** — evict any transient credential material; broker session ended per the runtime's own logout.
10. **Revocation** — an operator/customer action that disables the credential + quarantines the runtime, audited.
11. **Backup handling** — credential ciphertext + key material governed per-customer (Phase 8), never a shared store.
12. **Recovery** — restore requires BOTH ciphertext and the customer key id; recovery tested (SEC-CRYPTO-001 asks for this).
13. **Offboarding** — end-to-end: disable → revoke → runtime TOMBSTONE/RELEASE → destroy credential material.
14. **Verified destruction** — crypto-shred (destroy the customer key id so ciphertext is unrecoverable) + delete the row,
    with **destruction evidence** (an append-only audit record, no secret), mirroring the certified no-secret evidence model.

## 4. Requirements → design mapping (packet Phase-3 requirements)
- no broker passwords in Git / ordinary logs / screenshots / evidence → §2 T10, §3.14; keep the provisioner's no-secret
  evidence invariant.
- no shared customer credential store; no reuse across VPSs; customer-specific encryption boundary → §2 T4/T6, §3.4
  (per-customer key id).
- least-privilege + auditable operator access; redacted display → §2 T8, §3.4/§3.14 (audit extended to customer creds).
- secure destruction; backups explicitly governed → §3.11/§3.13/§3.14 (crypto-shred + evidence).
- mechanism: **do not** add an external secret-management platform without a justified ADR → ADR-0019 evaluates DPAPI vs
  encrypted-app-store vs improved-Fernet-envelope, all in-repo/in-VPS; recommendation there.

## 5. Governance — why implementation is gated
Reworking customer-credential storage/encryption/handling changes the **security posture** of the highest-sensitivity
asset in the platform, touches **all existing stored credentials** (a migration), and intersects the OPEN SEC-CRYPTO-001
finding. Under the governance overlay (**Red — credential access / security posture → Sponsor approval**) and
`.claude/rules/architecture.md` ("no silent architecture replacement… requires an approved decision before merge"), the
**implementation is a Sponsor gate**: ADR-0019 must be **accepted by the Sponsor** before any credential-storage code
lands. This design + ADR-0019 are the autonomous, no-credential-access deliverables; nothing is implemented here.

## 6. Deferred to later phases / follow-ups
Backup credential isolation (Phase 8); the SEC-CRYPTO-001 `DJANGO_SECRET_KEY` decoupling + MultiFernet key rotation
(pulled in here as a design input, implemented under ADR-0019); consolidating the two credential models
(`Mt5Credential` vs `TradingAccount`) into one source of truth; the dead `WorkerAccountCredentialsView.broker_password`
path (confirm + remove).
