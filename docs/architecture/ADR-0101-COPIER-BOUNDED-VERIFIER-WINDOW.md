# ADR-0101 — Copy Trading bounded verifier window for the backend ingest HMAC key

- **Status:** Accepted (repository-only / DARK, default-OFF). Merge, deployment, the real secret provisioning,
  the bounded-verifier enablement and the real one-shot verification are **separate Sponsor gates**. No deploy,
  no migration applied, no host contact, no flag enablement, no secret handled, no authorization, no broker order
  is part of this packet.
- **Packet:** GFX-COPIER-SECURE-INGEST-CONSUMER-RESUME-083 (the corrective identified by packet 080).
- **Date:** 2026-11-12.
- **ADR number:** 0101 — the next genuinely free number on `main` (0097 ARM/DISARM, 0099 broker fingerprint,
  0100 dashboard coverage are all merged; 0098 is an unused gap).
- **Builds on / realises:** the GFX-COPIER-EMITTER-SECRET-AND-INGESTION-ONLY-080 finding `SECURE_CONSUMER_PR_REQUIRED`:
  the backend's two gated verifier endpoints — ADR-0074 signed event ingestion and ADR-0077 host-emitter
  attestation verify — both resolved their dedicated secret `COPIER_INGEST_HMAC_KEY` with
  `core.credentials.resolve_secret`, which is **environment-only**. The production backend is not keyed, and
  packet 080 established that keying it via the container environment is unacceptable.

## Context — why an environment key is the wrong source

`resolve_secret` reads `os.environ`. To let the backend verify an emitter signature the operator would have to put
`COPIER_INGEST_HMAC_KEY` into the backend container's environment. On Linux/Docker that value would then be:

- visible via `docker inspect` (container `Config.Env`) and `/proc/<pid>/environ` to anyone with host/daemon access;
- persisted in a normal env file (`docker-compose.yml` / an `--env-file`), i.e. **at rest**, in a file that is also
  the backend's deploy artefact;
- resident for the **whole container lifetime** — a standing, un-bounded verifier key, not a one-shot — and
  inherited by every child process;
- trivially left enabled by accident (the gate is a flag, but the key would simply stay present).

That is precisely what packet 080 forbade ("do not claim secure ephemeral handling if the key would remain visible
through Docker inspection, container metadata, `/proc`, logs, crash output or a normal env file"). The verifier
needs the key for **one bounded verification session**, not forever.

## Decision — a bounded, single-flight, auto-expiring verifier window with an ephemeral tmpfs key source

Resolve the key from a **bounded verifier window** instead of the environment. A new `trade_copier.verifier_window`
module and a durable `CopierVerifierWindow` row (additive migration `0015`) govern the key's availability:

1. **Ephemeral, non-env key source.** The key lives only in a tmpfs file (default `/dev/shm/…`, RAM-backed,
   0600, owned by the app user, never followed through a symlink). It is never an environment variable, never a
   command argument, never a persisted file, never a Docker image layer, never in `docker inspect` metadata. A
   container restart erases it. The window row stores only the tmpfs **path**, never the key value, and no broker
   credential or token.
2. **Operator provisioning via STDIN only.** The `copier_verifier_window --open` management command reads the key
   from **STDIN** (the operator wrapper supplies it through a hidden, non-echoing prompt + pipe), writes it 0600 to
   the tmpfs path, and opens one window. The key is never an argument, never echoed, never logged, never returned.
3. **Single-flight.** A DB partial-unique constraint on `state=OPEN` permits **at most one** open window — a
   concurrent second open is rejected (`window_already_open`).
4. **Bounded lifetime + fail-closed.** A window has a short, capped TTL (≤ 900 s). No window / expired / wrong
   purpose / exhausted / missing-or-insecure key file ⇒ the key cannot be resolved and the gated endpoint returns
   **503**. Normal backend operation (no window) therefore holds **no key** — `build_keyring()` in both
   `event_ingestion` and `attestation_verify` now sources exclusively from the window, never from `os.environ`.
5. **Replay / exhaustion.** Envelope replay stays rejected by the existing single-use `CopierIngestNonce` burn. An
   optional `max_uses` cap (the one-shot runbook sets `--max-uses 1`) hard-limits verifications; reaching the cap
   auto-closes the window and shreds the key file.
6. **Guaranteed, idempotent cleanup.** Closing shreds the tmpfs file (random overwrite + unlink) and marks the row
   CLOSED; an expired window is reaped (and its file shredded) on the next open/claim; `--close` is idempotent and
   safe to call in a guaranteed-cleanup `finally` on every path (success / verifier failure / emitter failure /
   timeout / interruption); and the tmpfs file dies on a container restart regardless.
7. **Purpose separation.** A window is opened for `INGEST`, `ATTEST` or `BOTH`, so an "ingestion-only" verification
   cannot be used to drive the attestation path and vice-versa.

### Why this cannot enable trading

The window governs only **when** two identity-proof endpoints can resolve a verification key. Neither endpoint
creates a `MasterEvent`, `ExecutionJob`, `CopierLiveArm`, `CopierLiveAuthorization`, `CopierReplication`, lifecycle
row or broker contact — this is asserted by tests. The window grants no execution/arm/admission authority, provisions
no bridge, starts no worker, and stores no secret in the database. It never substitutes a different credential
(security.md RULE 3) and never reads the key from the environment. The scheduled emitter task stays DISABLED and
triggerless throughout.

## Trust boundary, lifecycle and exposure analysis

| Surface | Exposure | Mitigation |
|---|---|---|
| Command argument / process list | — | Key is read from STDIN; never argv. `--open` refuses an interactive TTY. |
| Environment / `docker inspect` / `/proc/environ` | — | Key is never an env var; resolver never reads `os.environ`. A key present in the environment does **not** resolve (regression-tested). |
| Persisted env file / image layer | — | Key lives only on tmpfs; no file on disk, no layer. |
| Shell history / PowerShell transcription | — | Operator wrapper rejects transcription/debug/verbose and pipes the hidden prompt; never writes the key. |
| Logs / exceptions / crash output | — | Reason codes only (`no_verifier_window`, `key_file_*`); the key is never logged or embedded in an error. |
| File permissions / ownership / symlink | denied | 0600 required; group/other access, wrong ownership and symlinks are refused (`key_file_permissions/ownership/symlink`). |
| Container/backend restart | — | tmpfs is RAM-backed → erased on restart; any OPEN window past expiry is reaped. |
| Concurrency / replay | rejected | Partial-unique `state=OPEN` (single-flight); nonce burn + optional `max_uses` (replay/exhaustion). |
| Cross-tenant / credential fallback | denied | Resolver returns only `COPIER_INGEST_HMAC_KEY`; never `GUVFX_AGENT_TOKEN` or any sibling secret. |
| Standing verifier left on | denied | Default-OFF gate + no window ⇒ 503; `max_uses=1` and guaranteed `--close` return it OFF. |

**Secret lifetime contract:** the key exists only from the operator `--open` (write to tmpfs) until the first of
`max_uses` reached / TTL expiry / `--close` / container restart, bounded by ≤ 900 s, and only in RAM. Rollback is
trivial and total: `--close` (or a restart) removes the mechanism's only secret surface; reverting the PR restores
the prior env-only resolver (which, with no env key, simply stays fail-closed).

## Rejected alternatives

- **Key in the container environment / env file** — rejected (the whole point; see Context).
- **Keep an env fallback in `build_keyring()`** — rejected: it would leave the forbidden env path alive and let a
  standing key bypass the window. The resolver is window-only; existing tests were migrated to provision the key
  through a window (synthetic keys), so no env path remains in production code.
- **Globally weakening `resolve_secret`** — rejected: it stays the strict, env-only, no-substitution resolver for
  every other credential. This mechanism is a **narrow, additional** source used only by the two copier verifier
  `build_keyring()` call sites.

## Consequences

- Additive migration `0015_copierverifierwindow` (one `CreateModel`, no data migration). DARK: inert until an
  operator opens a window.
- The two verifier endpoints are byte-identical when no window is open (503) — the DARK posture is unchanged until
  a Sponsor-gated verification session.
- `docs/COPIER_ONE_SHOT_ATTESTATION_RUNBOOK.md` is corrected: it no longer assumes the backend is already keyed;
  it now provisions the key into the bounded window and guarantees teardown.

## Not in this packet (separate Sponsor gates)

Merge · DARK deploy + apply migration `0015` · the real `COPIER_INGEST_HMAC_KEY` provisioning · enabling the
verifier gate · the real one-shot ingestion/attestation verification · the scheduled emitter remains DISABLED.
No deploy, no migration applied, no host contact, no secret handled, no flag enablement, no broker order here.
