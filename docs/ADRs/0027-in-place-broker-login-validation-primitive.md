# ADR-0027 — In-place broker-login validation primitive (Phase 1 design)

- Status: **Design proposed — awaiting acceptance before implementation** (per the Phase-1 directive's
  "Before implementation … Once the design is accepted: implement").
- Date: 2026-08-02
- Related: ADR-0021 (broker-login gate), ADR-0025 (broker-server resolution), ADR-0026 (capability plan),
  `.claude/rules/security.md`, `.claude/rules/architecture.md`.

## 1. Observed state

- **No non-destructive credentialed login exists.** `mt5.initialize(path=…, login, password, server,
  portable=True)` is the only login mechanism; today it is used either inside a re-materialise cycle
  (provisioner `configure`, which sends **no** password — broker-INDEPENDENT) or by the production signal
  bridge `login_and_validate`/`/mt5/order` (single terminal, **`mt5.shutdown()` in `finally`** → destroys the
  running session). The wired "Test connection" is an EA-presence check that 409s for `mt5_instance=None`
  accounts.
- The signed management channel (`mgmt_protocol.py`; `ALLOWED_OPERATIONS = MATERIALISE/START/VERIFY/STOP/
  TOMBSTONE/RELEASE + NEGOTIATE`) is **HMAC-signed but carries no credential**. It runs over the private
  Tailscale (WireGuard) tunnel to `100.79.101.19`.
- The beta-agent (`deploy/beta-agent/`) already owns per-host MT5 lifecycle under that channel; the backend
  drives it via `AgentWindowsProvisioner` (`mgmt_client.py`).
- Customer Zero MT5 (pid 316, Session 0) runs broker-INDEPENDENT; the packet forbids touching it.

## 2. Architecture comparison

| Candidate | Non-destructive? | Verdict |
|---|---|---|
| **A. Dedicated ISOLATED validation terminal** — the agent runs `mt5.initialize(path=<a validation-only portable terminal, never a running slot>, login, password, server, portable=True, timeout=…)` → reads `account_info().trade_mode` → **always `shutdown()`** | **Yes** — attaches to a terminal that is NEVER a running slot; no running runtime's session, state, strategy or execution is touched | **SELECTED** |
| B. Log in on the running runtime, then revert | No — transiently flips the running runtime broker-independent→logged-in→out; forbidden for CZ; can disrupt a live session | Rejected |
| C. Reuse the production signal bridge `login_and_validate` | No — Nuno's PRODUCTION terminal + account, `shutdown()` kills the running signal session | Rejected |
| D. New standalone validation micro-service | n/a — speculative infrastructure (architecture rule); the agent already owns host MT5 ops | Rejected |

## 3. Selected design

A new signed, read-only-to-runtimes agent operation **`VALIDATE_LOGIN`** plus a single backend entry point
`BrokerLoginValidator.validate(account)` that every future flow (Add / Edit / Test / Retry / Health /
Recovery) calls — **the single validation mechanism; no duplicate paths.**

- **Isolation invariant.** The agent performs the login only in a **dedicated validation terminal path**
  (config `VALIDATION_TERMINAL_DIR`, e.g. `C:\GuvFX\validation\terminal`) that is asserted, fail-closed, to
  be **distinct from the golden and from every slot's terminal** before any `initialize`. It holds a
  **process-global validation lock** (one probe at a time — a single MT5 python process attaches to one
  terminal), and **always `shutdown()`s** in `finally`. It reads only `account_info().trade_mode`; it never
  calls `order_send`/`order_check`/symbol selection.
- **Non-destructive + idempotent.** Pure probe: no runtime materialise/start/stop, no slot-store write, no
  strategy/execution mutation, repeatable with identical inputs → identical outcome.

## 4. Contracts

**Backend API (internal, secret-safe)** — `validate(account) -> ValidationOutcome`:
```
ValidationOutcome{ status: HEALTHY|NEEDS_ATTENTION|UNAVAILABLE,
                   reason: <taxonomy §5>, is_demo: bool|None, server: str,
                   login_masked: str, checked_at: iso8601 }   # never any password/ciphertext
```
**Protocol op `VALIDATE_LOGIN`** (additive to `mgmt_protocol`): a signed request whose SEMANTIC (signed)
fields extend the existing set with `login`, `server`, and `password` (transported for the login) — all
inside the HMAC-signed body over the Tailscale tunnel; advertised in NEGOTIATE `supported_operations` so an
old agent simply omits it (backward-compatible). **Idempotency is NOT keyed** (a probe is side-effect-free);
a fresh single-use nonce every call.
**Agent op `op_validate_login`** returns `{ ok, reason, is_demo|null, server, login_suffix }` — **no
password, no raw MT5 diagnostic dump, no full login** (only a redacted suffix, per the bridge `_acc_snapshot`
precedent).
**Bridge/agent MT5 contract**: assert isolated terminal → `initialize(login/password/server)` → on failure
map `last_error()` → reason; on success read `trade_mode` → `is_demo` → `shutdown()`.

## 5. Failure model (agent reason → customer-safe outcome)

| Agent reason | `mt5` signal | Customer-safe outcome | status | retryable |
|---|---|---|---|---|
| `invalid_password` / `invalid_login` | `initialize` fails, `last_error` auth-reject | "Invalid login or password" | NEEDS_ATTENTION | no |
| `server_not_found` | last_error unknown server | "Broker server not found" | NEEDS_ATTENTION | no |
| `server_unavailable` | last_error network/server | "Broker temporarily unavailable" | UNAVAILABLE | yes |
| `login_timeout` | init timeout | "Connection timed out" | UNAVAILABLE | yes |
| `account_disabled` | logged in, `trade_allowed`=false + reason | "Account disabled" | NEEDS_ATTENTION | no |
| `demo_ok` | `trade_mode==0` | "Connected (demo)" | HEALTHY | — |
| `live_detected` | `trade_mode==2` | "Live account" (classification returned) | HEALTHY* | — |
| `classification_mismatch` | declared demo, `trade_mode!=0` | "Demo/live classification mismatch" | NEEDS_ATTENTION | no |
| `mt5_unavailable` | `mt5_not_installed`/init env | "GuvFX could not verify the account" | UNAVAILABLE | yes |
| `bridge_unavailable` / `runtime_unavailable` | transport error/timeout | "Broker temporarily unavailable" | UNAVAILABLE | yes |

Transient (retryable) reasons drive bounded-backoff retry and retain credentials; credential/account reasons
move to NEEDS_ATTENTION and never hot-loop.

## 6. Security review

- **Credential at rest:** unchanged (Fernet `password_enc`). Decrypted **only at point of use**, with a P3
  `CREDENTIAL_ACCESSED` audit event (`credential_lifecycle`), never persisted in plaintext.
- **Credential in transit:** carried inside the **HMAC-signed** request over the **Tailscale WireGuard**
  tunnel (network-layer confidentiality) to the agent that must have it to log in. This is the one credential
  the channel now carries — documented explicitly (RULE-5/6): it is a **new confidentiality dependency on the
  tunnel**, not silent. (A future hardening option — envelope-encrypting just the password field to the
  agent's key — is noted as a follow-up, not required for the private tunnel.)
- **No exposure:** the backend outcome and the agent result contain **no password, no ciphertext, no raw MT5
  diagnostic, no stack trace, no signing secret, no infra address** — only a masked login suffix + server +
  classification + a mapped reason. Logging asserts the same (tested).
- **No trade / non-destructive:** the agent never calls order/symbol APIs; the isolated-terminal + always-
  shutdown invariants guarantee no running runtime is touched. Fail-closed if the validation terminal cannot
  be proven isolated.

## 7. Timeout / retry / rollback

- Per-op transport timeout for `VALIDATE_LOGIN` (login can be slow): default read 45s, clamp 90s (added to
  `OP_TRANSPORT_TIMEOUTS`).
- The backend **does not auto-retry** a definitive credential reject (fail closed). Transient reasons are
  surfaced to the caller with `retryable=true` for the caller's bounded policy (the health loop / user Retry).
- Rollback: the probe has no durable effect; `shutdown()` in `finally` is the only teardown. A transport
  timeout is AMBIGUOUS but harmless (no runtime change) → reported as UNAVAILABLE/retryable.

## 8. Remaining risks

- **Host prerequisite:** a dedicated isolated validation terminal must exist on the host (a pre-staged
  portable MT5, like the golden). Staging + host-certification is the **separate deployment authorisation** —
  not done here.
- **Concurrency:** one probe at a time per host (global lock) — acceptable for beta scale; note the cap.
- **Tunnel dependency** for password confidentiality (see §6) — accepted for the private network; envelope
  encryption is a documented future hardening.
- Live certification against a real demo login remains gated (Phase 5).

## 9. As-built (implemented 2026-08-02, branch `feat/broker-login-validation-primitive`)

Status: **design accepted (the Phase-1 directive authorised implementation); implemented + tested; pending
review + host certification.** Lifecycle status is the PM's to advance. This section supersedes PR #256's
standalone design record.

**One deliberate upgrade over §6.** The Sponsor's credential-transit decision mandated **envelope
encryption now, not as a future option.** The customer's broker password is sealed to the Windows agent's
**public** key so the backend can encrypt but **cannot decrypt** what it built; only the agent (holding the
matching private key) opens it at point of use. Construction: ephemeral-static ECIES / sealed box using
`cryptography` only (no new dependency, no custom primitive) — X25519 ECDH (fresh ephemeral sender key,
discarded on return) → HKDF-SHA256 → AES-256-GCM whose **AAD binds the ciphertext to
(operation, runtime_uuid, correlation_id, nonce)**. Keys are a **distinct scope** from the HMAC signing
keyring (`BROKER_CRED_ENC_KEY_ID` / `_PUBKEYS` / `_PRIVKEYS`), addressed by `key_id` for rotation. This
changes §4/§6: `password` is **not** a signed protocol field; instead the credential travels as a `payload`
carried verbatim and bound to the signature via a signed **`payload_digest`** (SHA-256 over the payload).
Tampering/stripping/substituting the payload fails the signature or the digest check **before** the agent
opens the envelope; a lifted ciphertext fails the AEAD AAD check.

**Files.**
- `backend/terminal_provisioning/broker_cred_envelope.py` — envelope seal/open (backend seals; settings/env
  keys). `+ tests_broker_cred_envelope.py` (9 adversarial tests).
- `backend/terminal_provisioning/broker_login_validation.py` — `BrokerLoginValidator.validate(account)`
  (single entry). `+ tests_broker_login_validation.py` (10).
- `backend/terminal_provisioning/mgmt_protocol.py` (+ byte-identical `deploy/beta-agent/lib/mgmt_protocol.py`)
  — `CREDENTIALED_OPERATIONS = ("VALIDATE_LOGIN",)`, `SUPPORTED_OPERATIONS`, `payload_digest` binding.
- `backend/terminal_provisioning/mgmt_agent_core.py` (+ byte-identical `deploy/beta-agent/lib/…`) —
  `_handle_validate_login` branch (integrity-gated, delegates entirely to an injected validator; the core
  stays Django/crypto/MT5-free per the `win_ops` boundary rule); NEGOTIATE advertises `SUPPORTED_OPERATIONS`;
  `is_demo` added to the response allowlist.
- `deploy/beta-agent/broker_cred_envelope.py` — agent copy (Django-free; env keys; opens only).
- `deploy/beta-agent/validate_login.py` — `assert_isolated_validation_terminal` (contained-under-dedicated-
  root **and** disjoint-from-every-slot/golden/accounts root, fail-closed), `classify_init_error`,
  `RealMt5Probe` (login/classify surface ONLY — no order/symbol/position API), `LoginValidationHandler`
  (opens envelope under the request AAD → global single-flight lock → probe → **always `shutdown()`** →
  allowlisted `{ok, reason_code, is_demo}`).
- `deploy/beta-agent/config.py` — optional `BETA_AGENT_VALIDATION_TERMINAL_DIR` / `_ROOT` /
  `_FORBIDDEN_ROOTS` / `_LOGIN_TIMEOUT_MS`; `deploy/beta-agent/agent.py` — `_build_login_validator` (built
  ONLY when the dedicated terminal **and** an envelope private key are configured, else the op fails closed
  `validation_unconfigured`).
- `deploy/beta-agent/manifest.py` — `broker_cred_envelope.py` + `validate_login.py` added to `IMPL_MODULES`;
  `manifest.json` regenerated (17 modules, `supported_operations` includes `VALIDATE_LOGIN`, integrity_ok).
- `deploy/beta-agent/lifecycle.py` — the four new protocol/core reason codes classified.
- `backend/terminal_provisioning/tests_validate_login_agent.py` — 33 agent-handler/isolation/taxonomy/no-
  leak/parity/wiring tests.

**Reason field.** The agent's response carries the login taxonomy in its universal `reason_code` field (not
`reason`) and the classification in `is_demo`; the backend echoes only the server/login **it** submitted,
never the agent's — no host path, password, or ciphertext in any response (§6 upheld, tested).

**Not covered here (separate authorisations):** deploy; staging/host-certifying the isolated validation
terminal + the exact `mt5.last_error()` code→reason calibration with positive+negative controls (RULE-11);
generating/installing agent envelope keys; any live broker login; enabling `PROVISIONING_REQUIRE_BROKER_LOGIN`;
Customer Zero re-provision.

**New host dependencies (deferred to host certification, not added to the install artefacts here).** The
agent venv gains two runtime dependencies used ONLY by the login primitive: `cryptography` (to open the
envelope — the minimum for the mandated encryption, and already a backend dependency) and `MetaTrader5` (the
`RealMt5Probe`, imported lazily so nothing else in the bundle needs it). Both are deliberately **not** added
to `provision_beta_venv.ps1` in this packet: a PowerShell install artefact must be validated by the real
target parser before first execution (RULE-9), the MT5 build is version-pinned to the golden image, and the
install runs only under a separate deployment authorisation. They are recorded here so the host-certification
packet installs them under the parse gate.
