# ADR-0077 — Copy Trading host emitter: read-only MT5 adapter, identity attestation, service bundle

- **Status:** Accepted (repository-only / DARK, default-OFF). Host installation, credential provisioning,
  attestation on a live host, and any enablement are separate Sponsor gates.
- **Packet:** GFX-COPIER-HOST-EMITTER-BUILD-006.
- **Date:** 2026-11-05.
- **Builds on:** ADR-0074 (hybrid completion — the signing library `bridge_emitter.py`, the signed ingestion
  envelope + nonce contract, the lifecycle/projection it feeds). **Composes:** ADR-0075 (per-host monitoring
  gateway + readiness — used as an optional readiness gate, not the snapshot source) and ADR-0072 (LIVE
  read-only positions monitoring gate). Nothing in those ADRs is modified. **ADR number:** 0077 — the next free
  number after ADR-0075 (`docs/architecture/`) and ADR-0076 (`docs/ADRs/0076-guvfx-tim-v0_3-c3-...`, TIM C3,
  landed by PR #614); this packet originally claimed 0076 and was renumbered to 0077 on reconciliation to avoid
  colliding with that TIM ADR, which is left unchanged.

## Context — the proven gap

The install preflight (GFX-COPIER-HOST-EMITTER-DARK-005) established that the merged production commit
`fb4ee402` contains only the signing **library** `trade_copier.bridge_emitter` — a class that is handed a
position snapshot and emits a signed revision — with **no** installable application: no MT5 read, no Windows
service/task, no attestation mode, no bundle. Installing an emitter and running an identity attestation were
therefore impossible without new code. This packet builds exactly that missing application, repository-only and
DARK.

## Decision

Add a Django-free, host-runnable emitter application under `trade_copier/emitter/`, a backend non-lifecycle
attestation verifier, and an account-scoped Windows bundle under `deploy/copier-emitter/`. Every capability is
behind its own default-OFF gate and none is on the execution path.

### 1. Read-only MT5 adapter (`emitter/mt5_adapter.py`)

A narrow `PositionSnapshotAdapter` exposing exactly two reads — `read_positions()` and `read_identity()` — and
**no** order/modify/close/cancel/send method (asserted by the tests). The production `BridgeHttpAdapter` GETs the
host's existing supported read-only endpoints (`/mt5/positions`, `/mt5/snapshots/account`) presenting the bridge
agent token, mirroring the audited `execution.snapshot_transport` contract. There is **no MetaTrader/broker
credential** in the emitter. Missing SL/TP are preserved as `None` (the `0.0 == no level` rule), never invented
as zeros; an unavailable/failed read returns a structured reason code, never a fabricated snapshot. The
position's broker **`open_time`** is read from the per-position `time` field — the honest age reference for
no-late-OPEN (the backend `diff.ObservedPosition` drops it; the emitter reads it directly). A fully synthetic
`FakeSnapshotAdapter` serves the tests; the production adapter is never invoked by a test.

**Read-path choice (snapshot_transport, not the #613 gateway).** ADR-0075's gateway is the right *readiness /
health / identity* gate and the emitter composes with it optionally, but it is DARK/undeployed and its client
returns classified readiness counts, not position rows. The live, identity-firewalled `/mt5/positions` primitive
is the snapshot source; migration behind ADR-0075's own routing switch is a documented follow-up.

### 2. One-shot identity attestation (`emitter/attestation.py` + `attestation_verify.py` + `attestation_api.py`)

The emitter proves — before observing anything — that it is the right build, on the right host, bound to the
right account/broker/terminal/bridge. `build_attestation` signs a canonical envelope (protocol/version, build
SHA, host id, account id, broker-login **fingerprint** (never raw), broker server, terminal fingerprint, bridge
id, config digest, observed-login fingerprint, nonce, issued-at, expiry) with HMAC-SHA256 over the emitter's OWN
dedicated key (`COPIER_INGEST_HMAC_KEY`) — never a broker/agent/DB/Tailscale secret — mirroring the audited
`event_ingestion` idiom (bounded skew + expiry, single-use nonce burned only after the signature verifies). The
one-shot `attest` mode reads **only** `/mt5/snapshots/account` identity, **never positions**. The backend
`verify_attestation_request` DB-cross-checks the authoritative identity — account, broker login, broker server,
and the observed live login (the terminal the emitter actually reached, when probed), plus the bridge/terminal
route when a `HostedExecutionEndpoint` exists — and burns the nonce in the shared `CopierIngestNonce` store; it
creates **no** lifecycle state, MasterEvent, ExecutionJob, alert or broker contact. The signed `host_id` /
`build_sha` are tamper-bound by the signature and recorded but are **not** DB-cross-checked here (the backend
holds no authoritative value for them; a build allow-list / host registry is a documented follow-up), and the
emitter's `attest` mode itself fails closed when the observed live identity does not match its bound account, so
a wrong-terminal envelope is never produced. `POST /api/copy-trading/attest-verify/` (gate
`COPIER_ATTESTATION_VERIFY_ENABLED`, default OFF → 503) is the surface. The pure verifier's negative controls —
wrong host/account/server/terminal/bridge/build, stale/future timestamp, replay, tamper, bad signature,
unsupported protocol — all fail closed (the host/build/observed cross-checks fire when the caller supplies the
corresponding expected value).

### 3. The runner (`emitter/app.py`) and gates (`emitter/config.py`)

`EmitterApp` wraps `BridgeEmitter` with the adapter, a durable spool and the attestation into three
separately-gated modes: `attest` (gate `attestation_allowed`), `observe-once` (gate `observation_allowed`;
delivery additionally gated by `delivery_allowed`) and `run` (the bounded fast/reconcile loop, inert unless
`observation_allowed` AND a positive `fast_interval_seconds`). Config holds the immutable identity and the
**env-var NAMES** for secrets, never a secret value; the service refuses to start without a complete identity
(`require_complete_identity`, reserved ids `<= 1` refused, loopback bridge required). Every gate defaults OFF and
a missing/malformed value evaluates OFF. These gates are deliberately **separate** from — and never conflated
with — lifecycle projection, dispatch, alert transport, demo execution or live execution.

Hybrid detection reuses the ADR-0074 pure `diff` + monotonic per-ticket revisions: OPEN / SL add·change·remove /
TP add·change·remove / volume increase / partial close / full close / unchanged (emits nothing). A retry
re-signs the transport envelope but **never** rewrites a revision's true `open_time`/`broker_event_time`, so a
late re-send can never reset an entry deadline; the backend's open_time admissibility rules a stale OPEN
`ENTRY_FAILED_FINAL` (proven end-to-end). The spool is bounded (overflow dead-lettered, recorded), restart-safe
(per-ticket revision + nonce persisted), and corrupt state fails closed with an operator-visible diagnostic.

### 4. Windows bundle (`deploy/copier-emitter/`)

An account-scoped bundle mirroring the repo's existing host conventions: a launcher, install/uninstall/status/
attest PowerShell (ASCII-only, `ParseFile`-validated per security RULE 9/11, one compact JSON final line,
`-Apply` dry-run default, reserved-id refusal, no broad recursive delete), a canonical **DISABLED** scheduled-task
template (Settings.Enabled=false, no `<Triggers>`, no RestartOnFailure — never scheduled, never auto-restarts
into active emission), a secret-free account-scoped config template, and a hash manifest + pinned source subset
to vendor. The emitter runs on Python 3.11+ stdlib only (no third-party dependency). It never edits the golden
MT5 template and never installs an Expert Advisor. It is a standalone operator bundle — **not** wired into the
signed hosted-executor daemon CONTRACT (so no drift coupling) — and is **not installed** by this packet.

## Capacity (Phase 11)

Synthetic per-stage + end-to-end emitter latency is recorded as evidence (one account). Production cadence is
NOT claimed from synthetic numbers; the fast loop defaults OFF, and a **host measurement** (on the real Windows
box, at the current and the certified-maximum account count) is required before selecting any sub-second
interval.

## Consequences

The copier now has a reviewed, installable host emitter with a read-only MT5 adapter (no broker credential, no
order surface), a one-shot identity attestation with a gated backend verifier, restart-safe bounded delivery,
and a disabled-by-default Windows service bundle — all DARK, additive, behind default-OFF gates, with execution
still gated solely by the unchanged execute/arm walls.

## Adversarial review (Phase 12)

An independent review across the 14 attack vectors found **no CRITICAL/HIGH**; the execution-safety invariants
hold (no order surface on the adapter; verify creates no lifecycle/job/broker and 503s before any work; gates
default OFF; attest reads no positions; `open_time` carried immutably with no re-stamp on retry; nonce burned
only after the signature verifies; operation/protocol binding prevents cross-protocol replay on the shared nonce
store). **Fixed within scope, with regression tests:**
- *M1* — the backend verify now cross-checks the **observed live login** against the account's login (a wrong
  terminal fails closed on the real path, not only in a unit test), and the advertised claims are corrected to
  exactly what is DB-cross-checked (account/login/server/observed-login, plus bridge/terminal when an endpoint
  exists); `host_id`/`build_sha` are signature-bound + recorded, not DB-cross-checked.
- *M4* — the emitter's `attest` fails closed on an observed-identity mismatch, so a wrong-terminal envelope is
  never produced.
- *M2* — a corrupt durable spool is **quarantined** and fails closed (never a silent reset of the monotone
  revision ledger) and surfaces `spool_corruption` in diagnostics.
- *M3* — the installer restores the prior task on a failed `-Apply` and verifies DISABLED via the task XML
  (locale-independent), closing the dead-rollback + locale-parse gaps.
- *L2* — only the `key_id` (never a secret-derived fingerprint) is surfaced; *L3* — installer XML interpolation
  is escaped.

**Accepted (recorded, not fixed):** *L1* — the verify endpoint can reveal account existence and does a pre-auth
DB lookup (throttled 60/min/IP + gated OFF; a signature-first reorder is a follow-up); and the redundant
`account_id` equality check (harmless — the login/server values are derived from the DB for the claimed
`account_id`, so a forged account binding fails on the login/server cross-check).

## Separate future Sponsor gates (NOT in this packet)

Host installation + identity attestation on the live host; secure provisioning of the emitter's authentication
material; scheduler/continuous-emission enablement; ingestion/projection/dispatch/alert flag enablement; a real
alert transport; read-only/shadow production observation; and any live one-order acceptance. Each is a distinct
authorization.
