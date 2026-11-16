# Copy Trading — account-46 one-shot identity attestation runbook

**Packet:** GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021 (ADR-0077). **Status:** prepared, NOT executed. Repository
tooling only. This runbook is executed later under a separate Sponsor GO (the attestation packet), never as part of
building the wrapper.

The one-shot attestation is split into two roles so **no agent ever handles a secret** and the **production verifier
is ON only for the single request**. The emitter scheduled task `GuvFX_CopierEmitter_46` stays DISABLED and never run
throughout.

> **Backend key source (ADR-0101).** The backend is **NOT keyed at rest** — `COPIER_INGEST_HMAC_KEY` is never in the
> backend container environment, env file or image. For this one request the matching key is provisioned into a
> **bounded verifier window**: an ephemeral tmpfs file (`/dev/shm/…`, 0600) made readable only while the window is
> OPEN, then shredded. See `trade_copier.verifier_window` / the `copier_verifier_window` command. Earlier revisions of
> this runbook wrongly assumed the backend already held a key "matching the backend's"; it does not, and must not.

## Preconditions

- The attestation verifier endpoint is present and returns `503 {"status":"disabled"}` with
  `COPIER_ATTESTATION_VERIFY_ENABLED` OFF, and **no verifier window is open** (`copier_verifier_window --status`
  reports `open:false`), so the backend holds no key.
- Account-46 emitter bundle installed DISABLED at `C:\GuvFX\copier-emitter\46` (packet-017), hashes verified.
- The operator can RDP to the Windows host locally and can SSH to the backend host to run the operator wrapper.
- The human operator has the **emitter's signing key** (`COPIER_INGEST_HMAC_KEY`) and `GUVFX_AGENT_TOKEN` to enter at
  **hidden prompts**. The same signing-key value is provisioned into the backend's bounded verifier window for the
  single request; nothing is stored on either host by this procedure — the window file is tmpfs and is shredded on
  close. The agent never receives, enters, or sees any of these values.

## Required gates — all THREE must be ON (and are restored OFF at teardown)

A successful attestation requires three **independent** gates. Enabling fewer produces a fail-closed rejection, never
an unsafe state. They are, with the exact reason code each one's absence yields:

1. **Backend master gate `COPIER_ENABLED`** — ANDed into every copier backend capability, including the one-shot
   trigger. Missing ⇒ the signed trigger `request_copier_emitter_oneshot_attest` refuses with
   `copier_emitter_oneshot_disabled` (host NOT contacted).
2. **Backend attest-verify gate `COPIER_ATTESTATION_VERIFY_ENABLED`** — arms the `/attest-verify/` endpoint. OFF ⇒
   `503 {"status":"disabled"}`; ON with no open window ⇒ `503 {"status":"unavailable"}` (still no key).
3. **Host emitter config gate `attestation_allowed`** in `C:\GuvFX\copier-emitter\46\config.json` — checked by
   `EmitterApp.attest()` (`trade_copier/emitter/app.py`) **before** the key is read, the identity is probed, or any
   POST is made. It is HOST-side and **invisible to the backend**, so a backend-only enable can never satisfy it.
   `false` ⇒ the host returns `attestation_gate_off` (host contacted, **no POST**). Toggle it with the reviewed,
   reversible, rollback-first `operator/Set-GuvfxCopierAttestationGate.ps1 -Enable` / `-Disable` / `-Status`
   (`observation_allowed` and `delivery_allowed` MUST stay `false`; it refuses otherwise and on an incomplete
   identity, and never handles a secret). Per-trigger, the operator wrapper additionally injects
   `COPIER_EMITTER_ONESHOT_ENABLED=1` into the single `docker exec` trigger — this is NOT a persistent flag.

Enable gates 1 and 2 by adding `COPIER_ENABLED=1` and `COPIER_ATTESTATION_VERIFY_ENABLED=1` to the **persistent,
auto-loaded** compose override (not a throwaway `-f` overlay: a backend recreate on the default files — e.g. a
routine redeploy — drops an overlay, leaving the gate silently OFF when the human runs the wrapper). Verify all three
are ON before requesting the key, and restore all three OFF at teardown.

## Role A — Claude Copy Trading operator (non-secret only)

1. Pre-checks (read-only): endpoint returns 503; `copier_verifier_window --status` reports `open:false` (backend
   holds no key); account-46 task DISABLED / never-run; no emitter process; spool empty; `LiveExecutionAuthorization`=0,
   `CopierLiveAuthorization`=0, kill switch 0, zero QUEUED/RUNNING copier jobs; record pre-counts (`CopierIngestNonce`,
   `MasterEvent`, `CopierPositionLifecycle`, `CopierReplication`, `ExecutionJob`).
2. **Enable all three gates (no secret):** add `COPIER_ENABLED=1` and `COPIER_ATTESTATION_VERIFY_ENABLED=1` to the
   persistent compose override and recreate the backend; run `operator/Set-GuvfxCopierAttestationGate.ps1 -Enable` on
   the host to set `attestation_allowed=true` (obs/delivery stay OFF). Verify health, route resolves, no migration ran,
   **execution/mutation/live controls still OFF**, and confirm all three gates ON (backend `copier_enabled()` +
   `copier_attestation_verify_enabled()` true and host `-Status` reports `attestation_allowed:true`) BEFORE requesting
   the key. With gate 2 on and no window open, the endpoint returns `503 {"status":"unavailable"}` (still **no key**).
3. Tell the human operator: "gate is ON — run the operator wrapper now." The wrapper **provisions the bounded
   verifier window** (hidden prompt → stdin → `copier_verifier_window --open --purpose attest --max-uses 1`; the key
   never touches argv/env/logs), then runs the account-46 emitter one-shot, then **guarantees** `--close`. Start the
   abort timer (see below). The agent provisions **no key** and runs no wrapper.
4. Receive ONLY the wrapper's redacted result (http status, verified, reason code, timestamp, correlation id).
5. Verify cleanup and restore OFF: confirm `copier_verifier_window --status` reports `open:false` and the tmpfs key
   file is gone (the wrapper's guaranteed `--close`; if in doubt, run `copier_verifier_window --close` yourself — it is
   idempotent and handles no secret). Then restore **all three gates OFF**: `operator/Set-GuvfxCopierAttestationGate.ps1
   -Disable` on the host (`attestation_allowed` back to `false`), and remove `COPIER_ENABLED` + `COPIER_ATTESTATION_VERIFY_ENABLED`
   from the persistent override and recreate; verify the endpoint returns `503 {"status":"disabled"}` again and the host
   `-Status` reports `attestation_allowed:false`.
6. Verify exactly one new `CopierIngestNonce` (iff success), no new `MasterEvent`/lifecycle/replication/`ExecutionJob`,
   task still DISABLED/never-run, zero broker orders. Record evidence (redacted).

The agent never sees, enters, provisions or infers a secret, never runs the emitter or the key-provisioning wrapper,
and never reads the envelope/signature. The one-action `copier_verifier_window --close` and `--status` the agent may
run handle **no** key value.

## Role B — Human operator (secret entry, local RDP only)

1. Only after Claude confirms the verifier is ON, run, locally on the host, in a normal (non-transcribed) console:

   ```
   .\Invoke-GuvfxCopierOneShotAttestation.ps1 -AccountId 46 \
       -ConfigPath C:\GuvFX\copier-emitter\46\config.json \
       -VerifierUrl https://api.guvfx.com/api/copy-trading/attest-verify/
   ```

2. Type the confirmation phrase, then enter the two secrets at the **hidden** prompts. Never paste a secret into
   chat, a ticket, a log, or anywhere else.
3. Report ONLY the wrapper's one-line redacted JSON result to Claude. Do not copy the envelope or any prompt value.
4. The wrapper submits exactly one request and self-cleans; it does not retry. If it reports a failure, do NOT re-run
   without a fresh Sponsor authorization (a consumed nonce cannot be reused).

## Role B2 — Headless variant (account 46, MT5-only workspace; ADR-0104 + packet-112)

Account 46's workspace is MT5-only, so there is no interactive PowerShell desktop in which to run the Role-B wrapper.
The headless path instead uses the pre-installed DISABLED task `GuvFX_CopierEmitterOneShot_46` (run_oneshot_attest.py),
which reads the ingest secret from a **host-local ephemeral key file** and shreds it after one use. The operator
provisions that file securely, triggers the task once, then removes any residue. The human enters the **same** key
here as into the backend verifier window; the agent never receives it.

1. Only after Role A confirms the verifier window is OPEN (ATTEST, `max_uses 1`) and the gate is armed, the human
   operator, locally over RDP as an administrator in a normal (non-transcribed) console, provisions the host-local key:

   ```
   .\Provision-GuvfxCopierOneShotKey.ps1
   ```

   Enter the confirmation phrase, then the **same** ingest key at the hidden `Read-Host -AsSecureString` prompt. The
   wrapper validates the installed task contract (account-46-pinned, DISABLED, triggerless, correct action/principal),
   refuses a reparse-point path, writes `C:\GuvFX\copier-emitter\oneshot\ingest.key` atomically with inheritance broken
   and access restricted to `guvfx_u_46` + SYSTEM + Administrators, and prints only a redacted JSON result. Report that
   one line to Role A (never the key). Optionally run `.\Provision-GuvfxCopierOneShotKey.ps1 -Verify` to confirm
   `ok:true` without reading the key.
2. Role A triggers the task exactly once through the signed op (`request_copier_emitter_oneshot_attest --apply`, which
   enables -> triggers -> host re-disables the task). `run_oneshot_attest.py` reads the key into a process-local env,
   builds the identity attestation, POSTs it once to the ATTEST verifier, writes a sanitised result, and **shreds the
   key file** (one-use).
3. Guaranteed cleanup on every path: the human runs `.\Provision-GuvfxCopierOneShotKey.ps1 -Remove` (idempotent
   shred+delete of the fixed path; refuses a reparse target) even though the consumer already shreds on success, and
   Role A closes the verifier window and restores the gate OFF (see Abort path). Confirm the key file is absent and the
   task is DISABLED and triggerless.

The provisioner never enables, triggers or runs any task, never opens the verifier window, and never contacts the
backend or broker; it only places (or removes) the one ephemeral key file.

## Abort path

If the human does not complete the invocation within the agreed short window (e.g., 10 minutes), or anything looks
wrong, **Role A closes the verifier window and restores the gate OFF immediately**: run `copier_verifier_window
--close` (idempotent; shreds the tmpfs key, handles no secret), confirm `--status` reports `open:false`, restore **all
three gates OFF** (`Set-GuvfxCopierAttestationGate.ps1 -Disable` on the host, then remove `COPIER_ENABLED` +
`COPIER_ATTESTATION_VERIFY_ENABLED` from the persistent override and recreate), verify the `503 {"status":"disabled"}`
response, and record an aborted-window note. Neither the key window nor any gate may be left on while reports are written. The
bounded window also self-expires (≤ 900 s) and its tmpfs key dies on any container restart, so a missed close still
fails closed.

## Numeric-fidelity check (real run)

The wrapper parses the emitter's attest output and re-serializes the `envelope` for the POST. On the real run, confirm
the verifier returns `verified` (HTTP 200) — a `bad_signature` (401) would indicate a JSON round-trip altered a signed
value; if so, the fallback is to have the emitter write the envelope to a restricted-ACL temp file read verbatim (do
not print it). Validate this on the Windows runner with the Pester mock before the production run.

## Durable follow-up (do NOT implement in this PR)

- **Production-grade secret handling.** Continuous/automated operation must NOT rely on interactive entry. Use an
  approved secret store (Windows Credential Manager / DPAPI, or a governed service-identity mechanism) so the emitter
  reads its `COPIER_INGEST_HMAC_KEY` / `GUVFX_AGENT_TOKEN` from a protected store rather than an env var. This is a
  separate, governed change.
- **`GUVFX_AGENT_TOKEN` rotation.** This token remains scheduled for project-closure rotation; it must never be
  displayed or copied during this work.

## Transport reconciliation (packet 139) - `--drive-attestation`

The host emitter sends its signed envelope to the attest-verify endpoint EXACTLY ONCE. A transient
proxy/network failure (connection dropped / HTTP 502 / no response) is AMBIGUOUS: the verifier may have
processed the request even though the HTTP response was lost. A blind retry is FORBIDDEN - it could
double-process and it wastes the one-use key.

With `--drive-attestation` the single-entry operator wrapper (`provision_and_open_oneshot.py`) drives the
attestation itself, holding the human-entered key only in process memory for the bounded operation:

1. Provision host key + open the verifier window (as before).
2. Trigger the one-shot attestation once (`request_copier_emitter_oneshot_attest --apply`).
3. Classify the result:
   * `verified` -> VERIFIED (done);
   * a DEFINITIVE backend verdict (`transport_ambiguous=false`: rejected/replayed/disabled/unavailable/...)
     -> REJECTED. A verifier HTTP verdict is NEVER retried.
   * an AMBIGUOUS transport outcome -> reconcile against AUTHORITATIVE state (`copier_attest_reconcile
     --nonce <n> [--correlation-id <c>]`), which reads only existing durable records:
       - `CopierVerifierWindow.uses` (incremented when the key is CLAIMED, before the verdict) and
       - `CopierIngestNonce` (the envelope nonce is burned only after a FULL verify),
     and returns PROCESSED_VERIFIED / PROCESSED_REJECTED / NOT_PROCESSED / AMBIGUOUS.
       * PROCESSED_* -> report, NEVER retry;
       * NOT_PROCESSED (window still OPEN, uses==0, nonce not burned) -> reprovision the host key from the
         SAME in-memory bytes (no second human prompt) and retry EXACTLY ONCE;
       * AMBIGUOUS (or NOT_PROCESSED again after the single retry) -> report UNKNOWN, NEVER retry.
4. GUARANTEED teardown on every terminal path: drop the in-memory key reference, shred the host key
   (provisioner `-Remove`), close the verifier window (`copier_verifier_window --close`). The one-shot TASK
   is re-disabled host-side by the signed trigger itself. Backend flags and the host emitter config gate
   remain the agent's separate enable/teardown.

At most ONE retry. Exactly-once is preserved: the only state that permits a retry is a PROVEN NOT_PROCESSED
(the request never reached verification), and the retry reuses the still-OPEN window's unchanged backend key.
`copier_attest_reconcile` is strictly read-only (no key, no mutation, no host/broker contact) and NEVER
contacts account #48. The attestation nonce used for reconciliation is a non-secret single-use value.
