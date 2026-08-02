# GUVFX PROGRAMME DIRECTIVE (DRAFT — awaiting Sponsor authorisation)
## Customer Zero – Dedicated Validation Environment Provisioning and Interactive IPC Proof
### STATUS: DRAFT / PROPOSED · CREDENTIAL-FREE · NO BROKER LOGIN
### Prepared by: Claude · For: Programme Sponsor authorisation

> This packet operationalises the accepted **Option F** decision (a dedicated low‑cost Windows validation
> environment). It is **credential‑free**: it must not use, decrypt, request or transmit Customer Zero's
> broker password, and must not perform a broker login. Execution **STOPS** after proving credential‑free
> MetaTrader5 Python IPC, synthetic encrypted‑payload handling, clean shutdown and exact terminal‑baseline
> restoration. The **first live broker validation remains separately authorised.**

---

## 1. Context and decision (why this environment)
Established by prior evidence (all read‑only / non‑credentialed):
- MetaTrader5 Python IPC **fails in Windows Session 0** (services) with `(-10005, "IPC timeout")` — reproduced 4×.
- The same integration **works in an interactive session** (the production bridge `mt5_signal_bridge.py`, Session 1).
- The production host is **Windows Server 2025 without RDS** → **2 concurrent admin sessions**, already at cap
  (console Admin + RDP Admin + disconnected `guvfx-rdp`), so a **dedicated interactive session cannot be added**
  without disconnecting a session or adding RDS licensing — both forbidden.
- Sharing an existing session that runs a **production MT5** risks the `MetaTrader5` "connect to a running
  terminal" behaviour disrupting the **live bridge**, and cannot be tested without production risk.
- The Session‑0 agent (`NT SERVICE\GuvFXBetaAgent`) is a virtual account **without SeTcbPrivilege**, so it
  cannot broker a session.

**Decision:** run the isolated validation worker + terminal on a **dedicated Windows validation VM** with its
own interactive session, no production MT5, and **zero production blast radius**. The VM also serves as the
**safe environment** to prove interactive IPC and MT5 coexistence without touching the live bridge.
Do **not** repurpose the `guvfx-rdp` session; do **not** use either Administrator session.

---

## 2. Minimum VM specification
| Item | Minimum | Notes |
|---|---|---|
| vCPU | 2 | MT5 + one Python worker; single‑flight, low concurrency |
| RAM | 4 GB | MT5 terminal ~300–600 MB resident + Python + OS headroom |
| Disk | 60 GB SSD | OS + Python + `cryptography`/`MetaTrader5` + one golden‑copy terminal (~380 MB) + a pristine golden reference copy for restore |
| OS | Windows 11 Pro **or** Windows Server 2022/2025 (Std/DC) | Single **console** interactive session only — **no RDS role, no RD CALs** |
| Region | Same region/provider as the prod estate (e.g. OVH) | Broker/network parity + Tailscale latency |
| Lifecycle | Always‑on (Trusted‑Beta scale) | An on‑demand spin‑up variant is possible later but adds orchestration; not for the first proof |

The VM is **single‑purpose**: it hosts only the validation worker + isolated terminal. It runs **no**
production MT5, **no** Customer Zero runtime, **no** Golden authority (only a copy‑for‑restore), and is **not**
part of the provisioning/execution plane.

---

## 3. Windows licensing assumptions
- Only **one interactive console session** is required (the VM's own auto‑logon). **RDS / Remote Desktop
  Session Host is NOT installed and NOT required; no RD CALs.**
- A standard per‑VM Windows license (cloud‑image‑included or BYOL) suffices. The 2‑session limit that blocked
  the prod host does not apply, because the VM's own console session is used (not a third RDP session on a
  shared host).
- If a cloud Windows image is used, the license is bundled in the instance price (see §17).

---

## 4. Network and Tailscale requirements
- The VM joins the existing **Tailscale tailnet** as a **new node** (e.g. `guvfx-validation`), authenticated
  via a Sponsor‑supplied auth key (secret; §Secret boundary).
- **Inbound:** the validation worker binds a **single port** (proposed `:8792`) reachable **only** from the
  backend over Tailscale, enforced by a **Tailscale ACL + host firewall rule** (no public exposure). The port
  is refused from any non‑backend peer.
- **Outbound:** the VM must reach the **IS6 demo broker server** over the internet (for the future live login;
  not exercised in this packet) and the Tailscale control plane. It needs **no** route to the production host,
  Customer Zero, the Golden, or the provisioning plane.
- The VM must **not** be reachable from the public internet on any management port.

---

## 5. Dedicated low‑privilege identity
- A local user **`guvfx_validation`** on the VM: **not** Administrator, **not** a slot account, **not** the
  Beta Agent identity, **not** `guvfx-rdp`.
- Access limited to: its own profile, the worker directory, the worker Python environment, and the isolated
  validation terminal subtree. **No** access to any production MT5 path (none exist on the VM), the Golden
  authority, or other users' profiles.
- Password / auto‑logon secret is **Sponsor‑inserted or generated on‑VM and never printed/committed** (see
  Secret boundary). Auto‑logon on this **dedicated** VM is acceptable (no session contention, single purpose).

---

## 6. Interactive‑session startup model
- `guvfx_validation` **auto‑logs on** to the VM console at boot → a persistent **interactive session** with a
  real `WinSta0` (where MT5 IPC works).
- The **validation worker** is started by a **startup scheduled task / logon task** owned by
  `guvfx_validation` (the same pattern the production bridge uses via `GuvFX_SignalBridge`), so it runs inside
  that interactive session.
- Session/worker **absence is detectable and fails closed** (`validation_session_unavailable` /
  `validation_worker_unavailable`).

---

## 7. Isolated MT5 terminal source
- A **clean portable MT5** terminal, sourced from the **certified clean Golden** (byte‑for‑byte, pinned by
  `.guvfx_golden_manifest`; **no `accounts.dat`**, no account cache, no logs, no EAs) — per RULE‑10 (a clean
  install, never a used/production terminal). The Golden is copied to the VM; the **VM's Golden copy is a
  restore reference only and is never used as the active terminal**.
- The active validation terminal lives at a fixed VM path (e.g. `C:\GuvFX\validation\terminal`), owned by
  `guvfx_validation` with **Modify** on that subtree only (write is required for portable MT5 — proven
  necessary on the prod host).
- Isolation is enforced by the deployed ADR‑0027 `assert_isolated_validation_terminal` contract (contained
  under a dedicated root; disjoint from any golden/other path; `..`/bare‑drive rejected; exe present).

---

## 8. Worker deployment
- New component **`validation_worker.py`** (in `deploy/beta-agent/` or a new `deploy/validation-worker/`),
  deployed to the VM with its own Python venv (`cryptography`, `MetaTrader5`).
- Responsibilities: bind the local Tailscale endpoint; accept **only** the validation‑request contract;
  verify the signed request; **decrypt the envelope at point of use**; run the isolated MT5 probe; return
  **only** allowlisted secret‑safe fields; **always** shut down + restore the terminal to baseline;
  single‑flight; expose health/readiness without secrets.
- Reuses the existing, reviewed building blocks: `broker_cred_envelope` (open), the `RealMt5Probe`/
  `LoginValidationHandler` probe + isolation contract, `mgmt_protocol` (HMAC signing) adapted to a local scope.

---

## 9. Envelope‑key ownership (re‑key to the worker)
- The **worker generates its X25519 private key on the VM**; the private key **never leaves the VM** (stored
  in the VM's host secret store; never printed/committed/logged).
- The **backend** receives only the worker's **public key + key id** and remains **seal‑only**
  (`backend_has_private_keys()` false; refuses to operate if private keys appear).
- The **Session‑0 Beta Agent is removed from the validation credential path** — it no longer holds the
  envelope private key and no longer runs the probe. (Cleanup of the abandoned Session‑0 validation config is
  part of rollback/decommission, §16.)
- **Rotation** supported via key ids; an old key retained only for an explicitly bounded overlap if ever
  needed.

---

## 10. Local IPC / HMAC scope
- The backend reaches the worker over **Tailscale**, using the existing **HMAC‑signed** request contract with
  a **separate keyring scope** (distinct from `BETA_AGENT_KEYRING` and from the envelope keys).
- Guarantees: single‑use nonce + replay rejection; operation/payload/correlation binding; bounded
  timestamp/expiry; strict request and response allowlists; no secret logging; **one request at a time**;
  deterministic `unavailable`/`busy` responses; endpoint reachable only from the backend (Tailscale ACL +
  firewall).

---

## 11. No‑trade boundary
- The worker exposes / invokes **only** `initialize`, `last_error`, `account_info`, `terminal_info`,
  `shutdown`. It must **not** import, expose or call `order_send`, `order_check`, `positions`, `orders`,
  symbol‑trading, strategy execution or Expert Advisors. The validation terminal contains **no EA**.
- **Structural tests** fail if any prohibited MT5 method appears in the worker surface.

---

## 12. Cleanup contract (after every request / synthetic test)
1. Normal MT5 `shutdown()`.
2. Detect any remaining validation‑terminal process by **exact path + validation identity**.
3. Terminate **only** that path‑and‑identity‑verified stray.
4. Capture non‑secret diagnostics.
5. Restore the terminal from the VM's certified clean Golden copy to **exact baseline parity**.
6. Confirm: no `accounts.dat`, no logs, no account cache, no profiles, no EAs, no credential artefact.
7. Emit `VALIDATION_TERMINAL_CLEAN_AFTER_PROBE`.

---

## 13. Monitoring
- Worker **heartbeat** + **session‑present** + **terminal‑clean** status exposed to the backend/ops (reason
  codes only, no secrets). Absence/timeout surfaces as `validation_session_unavailable` /
  `validation_worker_unavailable` (UNAVAILABLE, retryable) and, for a persistent gap, an ops alert.

---

## 14. Reboot / session recovery
- On VM reboot / Windows Update: auto‑logon re‑establishes the interactive session; the startup task restarts
  the worker; the terminal is verified against baseline on start. **No human reconnect required.**
- Session loss / lock: fails closed (`validation_session_unavailable`) until the session + worker recover.

---

## 15. Failure taxonomy (customer‑safe, reused)
`validation_session_unavailable` / `validation_worker_unavailable` / `validation_terminal_missing` /
`mt5_unavailable` (IPC) / `server_unavailable` / `login_timeout` / `cleanup_failure` → **UNAVAILABLE
(retryable)**; `invalid_login` / `invalid_password` / `account_disabled` / `classification_mismatch` →
**NEEDS_ATTENTION (credential)**; `demo_ok` / `live_detected` → **HEALTHY**.

---

## 16. Rollback / decommission
- **Rollback (any gate fail):** stop + remove the worker task; remove the backend↔worker keyring + worker
  pubkey config; deprovision the VM. No DB restore required; no Customer Zero / production / Golden impact.
- **Decommission of the abandoned Session‑0 path (once the VM path is proven):** remove
  `BROKER_CRED_ENC_PRIVKEYS` and `BETA_AGENT_VALIDATION_TERMINAL_DIR` from the prod‑host agent env, remove the
  prod‑host validation terminal + its Modify ACL, and remove the backend's old (agent‑targeted) pubkey — a
  bounded, separately‑confirmed cleanup so the prod host carries no validation remnants.

---

## 17. Cost estimate
- **Small always‑on Windows VM** (2 vCPU / 4 GB / 60 GB SSD, Windows license bundled): **~US$20–45 / month**
  depending on provider/region (OVH/Azure/AWS small Windows instance).
- **On‑demand variant** (spin up per validation): lower running cost, higher orchestration complexity — **not**
  recommended for the first proof.
- One VM is sufficient for Trusted‑Beta scale (single‑flight validations). No RDS CALs. No additional storage.

---

## 18. Safety invariants (must hold throughout)
1. Customer Zero is not restarted, re‑provisioned or logged into. 2. Production terminals/bridge are never
targeted. 3. The Golden authority is never used as the active terminal (only a restore copy on the VM).
4. The broker password stays envelope‑encrypted until the worker opens it. 5. The Session‑0 agent never
acquires plaintext. 6. Backend remains seal‑only. 7. No order/position/symbol/EA API. 8. Path‑verified
cleanup terminates only the validation terminal. 9. Baseline restored after every attempt. 10. Single‑flight.
11. Session absence fails closed. 12. No production session is disconnected. 13. No manual customer re‑entry
in the normal journey. 14. One canonical validation primitive can later serve create/edit/test/recovery.

---

## 19. Execution phases and STOP gates (this packet)
- **P0 — Provision (Sponsor + secrets):** create the VM; join Tailscale (Sponsor auth key); base OS + Python +
  `cryptography` + `MetaTrader5`; create `guvfx_validation` (password Sponsor‑inserted/on‑VM, never printed);
  configure auto‑logon; copy the clean Golden → VM (restore reference + active terminal); firewall/ACL the
  worker port. *(Secret‑insertion pause points as needed.)*
- **P1 — Credential‑free interactive‑IPC proof:** in the VM session, synthetic `mt5.initialize(path=…,
  portable=True)` + `terminal_info` + `shutdown` + baseline restore. **GATE: `DEDICATED_INTERACTIVE_MT5_IPC_PASS`.**
  If FAIL → STOP, request architecture continuation.
- **P2 — Worker + synthetic sealed‑payload + cleanup:** deploy the worker + backend↔worker HMAC channel +
  worker envelope key; **synthetic sealed non‑secret payload** round‑trip (backend seals a fake value → worker
  opens it — proves the crypto path with **no broker credential**); replay/tamper/expiry/worker‑unavailable/
  concurrent‑busy controls; no‑trade surface assertion; clean shutdown; **exact terminal‑baseline restoration**.
  **GATE: worker + relay + cleanup certified.**
- **P3 — Reboot/session‑recovery + monitoring proof:** reboot the VM; prove auto‑logon + worker restart +
  baseline verify + heartbeat. **GATE: `VALIDATION_ENV_HOST_CERTIFIED`.**
- **STOP.** No broker login. **First live broker validation = a separate future packet.**

**Engineering (in parallel, repo‑only, gated by review/CI/merge before host use):** the `validation_worker`
+ backend client + envelope re‑key + local HMAC scope + no‑trade structural tests + full test suite +
**ADR‑0027 amendment** (session architecture) + adversarial review + `make check` + PR + merge + branch
deletion + governance closure.

---

## 20. Required evidence (per gate)
Backup + rollback identifiers; VM spec/region/Tailscale node; identity + session id + startup task;
`DEDICATED_INTERACTIVE_MT5_IPC_PASS` (identity, session id, exe path, IPC result, `last_error`, shutdown,
cleanup); synthetic sealed‑payload round‑trip + negative controls (replay/tamper/expiry/wrong‑key/
worker‑unavailable/busy/no‑trade); reboot‑recovery; `VALIDATION_TERMINAL_CLEAN_AFTER_PROBE`;
`CUSTOMER_ZERO_UNCHANGED`; `PRODUCTION_UNAFFECTED`; CI/PR/governance status.

---

## 21. NOT authorised (this packet)
Use/decrypt/request/transmit the Customer Zero broker password; any broker login; invoke a **credentialed**
VALIDATE_LOGIN; attach to Nuno's production MT5 or the bridge; use Session 1/3/4 or `guvfx-rdp`; touch
Customer Zero, the Golden authority, slot allocation or the provisioner (arm / `PROVISIONING_REQUIRE_BROKER_LOGIN`
/ jobs); place an order/trade; move a private key off its host; deploy or merge unreviewed implementation.

---

## 22. Next authorisation (on success)
Request exactly: **Customer Zero – Single Live Interactive Broker Login Validation** (one credentialed
`VALIDATE_LOGIN`, no trade). On P1 failure: **Customer Zero – Broker Login Architecture Investigation
Continuation**. On later implementation/cert failure: **Customer Zero – Interactive Validation Worker Failure
Investigation**.
