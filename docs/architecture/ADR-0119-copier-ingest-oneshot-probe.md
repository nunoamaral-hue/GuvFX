# ADR-0119 — Copier non-trading one-shot INGEST-signing probe

- **Status:** Proposed (repository-only; DARK). PM owns lifecycle status in Notion.
- **Packet:** GFX-COPIER-INGEST-ONESHOT-PROBE-160
- **Date:** 2026-11-19
- **Supersedes:** nothing. Additive to ADR-0074 (signed event ingestion), ADR-0077 (non-lifecycle emitter attestation), ADR-0101 (bounded verifier window), ADR-0104/0112 (emitter key / keyring), ADR-0114 (atomic lifecycle arming), ADR-0117 (copier production arming pipeline).

## Context

After the standing account-46 ingest key and a bounded INGEST verifier window were provisioned
(GFX-COPIER-INGEST-SINGLE-ENTRY-EXECUTION-159, ADR-0117 §3), the natural next gate — *verify one real signed
account-46 INGEST request end to end* — had **no reviewed execution path** that did not cross a hard safety
boundary. The only producer of a signed INGEST event in production is the standing emitter
(`trade_copier.emitter.app` `run` mode = `run_loop`, a **persistent worker** driven by a **real master MT5
action** on #46), and the only other way to craft a signed request is to **expose the ingest key** to an
operator or agent. All three are forbidden: no persistent worker, no real trade, no key exposure.

Consequently a Sponsor could not confirm that the whole ingest-authentication chain — host-key signing →
transport → backend HMAC verification → bounded-window key resolution → single-use nonce / replay protection →
teardown — actually works, **before** committing to the much higher-risk real OPEN/MODIFY/CLOSE demo. The
synthetic E2E test (`tests_synthetic_e2e_latency`, `tests_event_ingestion`) exercises the `/event-ingest/` HMAC
boundary with a **synthetic** key against a test fixture; it proves the code, not the live standing key against
the live window, and runs no production request.

## Decision

Add a dedicated, **structurally non-trading, one-shot INGEST-signing probe** that proves the live chain with
**zero trading or execution semantics**, gated behind the same master/ingestion verification gates and the
bounded INGEST window, default OFF.

1. **Pure envelope (`trade_copier.emitter.ingest_probe`, Django-free).** Mirrors the audited
   `event_ingestion`/`attestation` HMAC idiom (canonical-JSON signed field set, HMAC-SHA256,
   `hmac.compare_digest`, bounded skew + expiry, single-use nonce). The **complete** signed field set is
   `{protocol_version, operation, key_id, account_id, nonce, timestamp, expiry}` — a fixed protocol
   (`copier-ingest-probe-1`) and operation (`copier_ingest_probe`), the key id, the fixed account, a nonce and
   timestamps. It carries **no symbol, side, volume, price, ticket, order, lifecycle or position field**, so it
   is **structurally incapable** of being interpreted as — or mutated into — an OPEN/MODIFY/CLOSE event. The
   nonce is burned only after the signature verifies.

2. **Backend verification (`trade_copier.ingest_probe_verify` + `ingest_probe_api`).**
   `POST /api/copy-trading/ingest-probe/`, `AllowAny` + manual HMAC, triple-gated and fail-closed:
   `copier_ingest_probe_enabled()` (default OFF, which ANDs `copier_event_ingestion_enabled()` — itself ANDing
   the master `copier_enabled()`); AND the key is resolved **only** from the bounded ADR-0101 INGEST window via
   `ingest_keyring("INGEST")`, which fails closed on zero / ≥2 / expired windows (so *exactly one OPEN INGEST
   window* is enforced, not assumed); AND the signed `account_id` must equal the fixed `PROBE_ACCOUNT_ID = 46`.
   Verification creates **no** `MasterEvent`, `CopierPositionLifecycle`, `CopierLifecycleDispatch`,
   `ExecutionJob`, follower ticket, `CopierLiveAuthorization`, `CopierLiveArm` or `LiveExecutionAuthorization`,
   invokes no copier execution code, references account #48 nowhere, and contacts no broker. The single-use
   nonce is burned via the shared `CopierIngestNonce` store — the **same** replay protection as real ingestion
   and attestation, and the durable, metadata-safe evidence that the probe ran (**no migration**). The response
   is value-blind: `{status, verified, account_id, operation, protocol_version}` — never a secret, signature,
   nonce or trade datum.

3. **Host-side one-shot signer / operator command (`deploy/copier-emitter/run_ingest_probe.py`).** Run once, by
   hand, by an authorized operator — **not** a scheduled task and **not** a persistent worker. It pins account
   46, the fixed operation and the fixed endpoint (derived from the account config's own `ingest_url` origin +
   the fixed `/api/copy-trading/ingest-probe/` path; no arbitrary URL/account/operation argument). It reads the
   **existing standing** key from `<config dir>/ingest.key` — the same file the emitter auto-loads — **never**
   through argv/env/prompt/stdin/chat, and **never** prints, hashes, length-measures or otherwise exposes it;
   the standing key file is **retained** (never shredded/rotated/removed). It signs **exactly one** envelope,
   POSTs it **once** (no retry); `--replay` re-POSTs the **same** already-generated envelope once to prove the
   backend rejects a replay. It emits only a sanitised, value-blind result (verified / sanitised reason /
   correlation id / timestamp / attempt count) and scrubs the in-memory key and envelope in a `finally`.

Negative controls (wrong key, stale/future/expired timestamp, replay, tamper, bad signature, wrong account,
wrong/unsupported operation or protocol, oversize nonce, no window, gate off, key unavailable) all fail closed
with a stable sanitised reason.

## Why the probe is NOT a substitute for the real E2E demo

The probe proves the **ingest-authentication plane only**: that the standing key can sign a request the backend
authenticates through the bounded window, and that replay is rejected. It deliberately proves **nothing** about
trading. It exercises **no** MT5 state, **no** master action, **no** observer → `CopierIngestedRevision` →
`MasterEvent` projection, **no** replication, dispatch, admission, arm, follower (#48) order, or
OPEN/MODIFY/CLOSE lifecycle — by construction the probe operation cannot carry or become any of those. A green
probe therefore means "the key/window/transport/replay chain is sound," **not** "copy trading works." The real,
separately-governed OPEN→MODIFY→CLOSE demo (ADR-0114/0117 arm path) remains the only thing that can certify
end-to-end copy execution, and it is unaffected by this ADR.

## Consequences

- One more DARK endpoint and one host operator script; both inert until `COPIER_INGEST_PROBE_ENABLED` is armed
  **and** an operator opens a bounded INGEST window. No migration, no production change, no new persistent
  process, no scheduled task.
- The arming sequence gains a cheap, safe, repeatable pre-flight that de-risks the real demo without any trading
  surface.
- The probe shares the ingest key id (`copier-ingest`) and the `CopierIngestNonce` replay store with real
  ingestion/attestation, so a probe nonce and a real nonce cannot collide or be replayed across paths.
- **Governance coupling (intended, strictly more restrictive):** `copier_ingest_probe_enabled()` ANDs
  `copier_event_ingestion_enabled()`, and the probe resolves its key from the one shared `INGEST` verifier window.
  So the probe cannot run unless real signed `/event-ingest/` is also live (itself persist-only / non-trading
  unless `COPIER_IMMEDIATE_DISPATCH_ENABLED` is separately armed). A "non-trading probe window" is therefore
  simultaneously a live real-ingestion window sharing one key and one nonce space — the runbook mandates closing it
  immediately after the probe. The window is used-uncapped (TTL-bounded) because the verifier claims the key before
  the signature is checked, so each request burns one use.

## Status / rollout

Repository-only and DARK. Activation (flag arm + a bounded INGEST window + one operator probe + window close +
flags OFF, standing host key retained) is a **separate, Sponsor-gated** step, documented in
`docs/COPIER_46_48_DEMO_RUNBOOK.md`. PM owns the Notion lifecycle status.
