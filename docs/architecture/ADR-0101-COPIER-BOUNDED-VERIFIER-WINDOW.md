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

---

## Addendum — key-lifecycle correction (GFX-164, 2026-11-19, repository-only, migration-free)

**Status:** Accepted (repository-only correction; preserves — does not change — the ADR-0101 contract above).
No deploy, no migration, no host contact, no secret handled, no flag enablement, no window opened, no broker
order is part of this correction. Merge is a separate Sponsor gate.

**Defect (confirmed in the pkt-162 production acceptance).** The operator command wrote the key to a **fixed
default tmpfs path** *before* calling `open_window`, and `open_window` reaped expired windows as its first act.
When an expired prior window (e.g. the pkt-159 window) referenced that **same** default path, reaping it ran
`close_window → _shred_file(<default path>)` and **shredded the key that had just been written for the new
window** — producing an OPEN but **keyless** window (`window_status.key_file_present == false`). The verifier
then had a live window but no resolvable key. (Separately, arming a flag that needs a backend recreate wipes
`/dev/shm`; that is a distinct operational ordering constraint, recorded below, not this defect.)

**Correction (smallest fail-closed change; per-window ownership + generation-safe ordering).**

1. **Ordering.** `open_window` now performs stale cleanup (`reap_expired`) and the single-flight check
   **before** any new key material exists, and writes the key **itself**, inside the same atomic block, only
   **after** both. Because the write happens strictly after the reap and after confirming no window is already
   open, stale-window cleanup can never shred a freshly opened window's key.
2. **Per-window ownership.** When the operator supplies no explicit `--key-path`, the key is written to a
   **per-window-unique** tmpfs path (`_unique_key_path`: configured dir + a random stem token). Two windows
   therefore never share a key file, so closing/reaping/exhausting one window can never touch another's key.
   An explicit `--key-path` is still honoured verbatim; the ordering guarantee alone makes it safe under
   single-flight.
3. **Fail-closed cleanup, no cross-deletion.** `open_window` owns the key file it writes and shreds it on any
   failure *after* the write (create error, rollback) — but it **never** shreds a path that an already-open
   window (or the winner of a concurrent open) references. A second open is rejected **before** it writes any
   key (`window_already_open`), so a rejected open leaves no orphan and never deletes the live window's key.
   The operator command no longer pre-writes or blindly shreds a fixed path (which, on `window_already_open`,
   would have deleted the *existing* window's live key).

**Unchanged (not weakened).** Fail-closed resolution, single-flight (`state=OPEN` partial-unique), TTL bounds
(30–900 s), shred-on-close/reap/exhaustion, purpose separation, nonce/`max_uses` replay protection, the 0600 /
ownership / symlink / placeholder key-file hardening, and the window-only (no-env) resolver all stand exactly
as above. The key still never enters the DB, argv, env, logs or output.

**Operational ordering constraint (restated, not changed by this correction).** Any flag whose change requires
a **backend container recreate** (e.g. the `COPIER_*` gates resolved via `os.getenv`) still wipes the RAM-backed
`/dev/shm`. The tmpfs-backed verifier window must therefore be opened **after** such a recreate, never before —
otherwise the recreate erases the window's key. This correction does not change that contract; it only ensures
that, once opened correctly, the window's key is not destroyed by the reaping of an unrelated expired window.

**Evidence.** Defect-first tests in `backend/trade_copier/tests_verifier_window.py`
(`VerifierWindowKeyLifecycleTests`) reproduce the keyless-window fault through the real operator command on the
default path (proven to FAIL on the pre-correction source: `key_file_present == false`) and cover: stale reap →
fresh open, explicit close, expiry cleanup, a second open rejected before writing, failed-open rollback with no
orphan, distinct purposes never sharing a path, and container/tmpfs loss reported truthfully. Migration-free.
