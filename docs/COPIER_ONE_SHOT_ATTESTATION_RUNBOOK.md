# Copy Trading — account-46 one-shot identity attestation runbook

**Packet:** GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021 (ADR-0077). **Status:** prepared, NOT executed. Repository
tooling only. This runbook is executed later under a separate Sponsor GO (the attestation packet), never as part of
building the wrapper.

The one-shot attestation is split into two roles so **no agent ever handles a secret** and the **production verifier
is ON only for the single request**. The emitter scheduled task `GuvFX_CopierEmitter_46` stays DISABLED and never run
throughout.

## Preconditions

- The attestation verifier is deployed (temporary release `20f5491b`), endpoint present and returning `503
  {"status":"disabled"}` with `COPIER_ATTESTATION_VERIFY_ENABLED` OFF.
- Account-46 emitter bundle installed DISABLED at `C:\GuvFX\copier-emitter\46` (packet-017), hashes verified.
- The operator can RDP to the Windows host locally.
- The emitter's `COPIER_INGEST_HMAC_KEY` (value matching the backend's) and `GUVFX_AGENT_TOKEN` are available to be
  entered interactively by the operator. They are NOT stored on the host by this procedure.

## Role A — Claude Copy Trading operator (non-secret only)

1. Pre-checks (read-only): confirm verifier revision `20f5491b`; endpoint returns 503; account-46 task DISABLED /
   never-run; no emitter process; spool empty; `LiveExecutionAuthorization`=0, `CopierLiveAuthorization`=0, kill
   switch 0, zero QUEUED/RUNNING copier jobs; record pre-counts (`CopierIngestNonce`, `MasterEvent`,
   `CopierPositionLifecycle`, `CopierReplication`, `ExecutionJob`).
2. Open the temporary verifier window: set **only** `COPIER_ATTESTATION_VERIFY_ENABLED` ON and apply the minimum
   backend recreate. Verify health, route resolves, no migration ran, execution/delivery controls still OFF.
3. Tell the human operator: "verifier is ON — run the wrapper now." Start the abort timer (see below).
4. Receive ONLY the wrapper's redacted result (http status, verified, reason code, timestamp, correlation id).
5. Immediately restore `COPIER_ATTESTATION_VERIFY_ENABLED` OFF and recreate; verify the endpoint returns 503 again.
6. Verify exactly one new `CopierIngestNonce` (iff success), no new `MasterEvent`/lifecycle/replication/`ExecutionJob`,
   task still DISABLED/never-run, zero broker orders. Record evidence (redacted).

The agent never sees or enters a secret, never runs the emitter, and never reads the envelope/signature.

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

## Abort path

If the human does not complete the invocation within the agreed short window (e.g., 10 minutes), or anything looks
wrong, **Role A restores `COPIER_ATTESTATION_VERIFY_ENABLED` OFF immediately** (minimum recreate), verifies the 503
response, and records an aborted-window note. The verifier must never be left ON while reports are written.

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
