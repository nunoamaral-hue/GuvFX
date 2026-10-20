# GUVFX_MT5_LIVE_MASTER_FOLLOWER_REPORT

> Packet `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` (P0). Subsystem completion report.
> Author: Claude (Principal Engineer). Date: 2026‑10‑20. **All work is DARK; no production deploy,
> no LIVE execution, no real‑money order, no DEMO bridge mutation.** States per §19 are not conflated.

## 0. State summary (§19 taxonomy)

| State | Status |
|---|---|
| `ENGINEERING_COMPLETE` | ✅ Observer (A) + follower‑intent (B) + four PD corrections + Copy Trading UX (C) + B‑exec DEMO producer |
| `DARK_MERGED` | ❌ none — PRs open, merge gated on ADR‑0051 ratification + final‑head CI; auto‑merge OFF |
| `DEMO_CERTIFIED` | ❌ none — synthetic/unit certified; a real‑MT5 DEMO rehearsal is operator‑gated |
| `LIVE_OBSERVATION_CERTIFIED` | ❌ none — read‑only firewall foundation exists; needs a real LIVE account rehearsal |
| `LIVE_EXECUTION_CERTIFIED` | ⛔ **blocked (RED)** — no bridge close/modify path; arming reserved to Sponsor + Security |
| `PRODUCTION_DEPLOYED` | ❌ none |
| `CUSTOMER_ACTIVATED` | ❌ none |

## 1. Canonical main & PR SHAs

- Canonical `main` at report: `48ecd20a` (reconciled into #528).
- **PR #528** — backend foundation (branch `feat/mt5-copier-observer`): observer A, follower‑intent B,
  blocker corrections B1–B4, B2 single‑flight. Head `235885b3` (merged latest main). **OPEN.**
- **PR #535** — Copy Trading UX + read‑only API (branch `feat/mt5-copier-ux`, stacked on #528).
  Head `530ce7ab`. **OPEN.**
- **B‑exec** — DEMO ExecutionJob producer (branch `feat/mt5-copier-exec`, stacked on #528).
  Head `85ba54eb`. **OPEN.**
- No PR merged; no squash; auto‑merge OFF. Merge is gated on final‑head GitHub CI + ADR‑0051 ratification.

## 2. Existing infrastructure & capacity findings

New Django app `trade_copier` built as an additional governed strategy source (no competing engine).
Reuses: `ExecutionJob.save()` (the single creation gate), the snapshot identity firewall
(`execution.snapshot_transport`), source‑scoped routing primitives, three‑layer sizing,
`BrokerInstrument` symbol metadata, the five‑layer LIVE wall (`execution.live_authz`), kill switch,
broker gate, identity pin. Infra reachability (read‑only, 2026‑10‑20): Tailnet up; VPS `100.119.23.29`
+ MT5 bridge `100.79.101.19:8788` reachable; agent `:8787` down. Real‑MT5 DEMO infra present but a
rehearsal is operator‑gated.

## 3. Master observation certification

Read‑only observer (`observer.py`) reusing the per‑tenant read bridge + `verify_snapshot_identity`
(login‑exact + server + DEMO/LIVE env). Durable store (`MasterObservationPoll` + `MasterPositionSnapshot`
incl. SL/TP) + append‑only `MasterEvent` diff ledger. Pure `diff.py`: OPEN / FULL_CLOSE / PARTIAL_CLOSE /
VOLUME_INCREASE / SL·TP set‑change‑remove, float‑noise suppressed. Additive read‑only bridge field
(`fetch_positions` now returns sl/tp/time/identifier). **Certified in tests (synthetic).** A real MT5
observation run is a rehearsal item.

## 4. Manual & EA trade handling

The observer diffs OBSERVED position state — it does not filter by origin, so a manually or EA‑opened
master position is observed and diffed like any other. Origin attribution (manual vs EA vs script) is a
known gap in the underlying ingest (`deal.reason`/`opened_by` not authoritative); the copier does not
depend on it. Cycle prevention: copier‑placed follower trades carry a `CPY<id>` comment and are excluded
from generating master events.

## 5. Follower activation & configuration

`CopierFollower` (sizing mode/params, caps, SL/TP translation policy, disable policy A/B/C, versioned +
revocable unprotected‑close authorization) + append‑only `CopierFollowerAudit`. Audited service
(`services.py`): a follower cannot activate itself; cross‑user and self‑follow rejected fail‑closed.
The UI (#535) displays opt‑in/sizing/policy/status read‑only (config **writes** are a deferred follow‑up —
no write endpoints yet).

## 6. Configurable sizing

Pure `sizing.py`: FIXED_LOT, MULTIPLIER, BALANCE_PROPORTIONAL, RISK_BASED; broker min/step/max + follower
caps; floors to lot step; **never silently rounds up**; rejects below‑min and missing‑SL risk sizing.
Constraints carry an `authoritative` flag; B‑exec re‑reads **fresh** constraints at promotion and refuses
non‑authoritative bounds. Certified in unit tests.

## 7. IS6FX / TradersWay / Pepperstone coverage

Symbol resolution (`symbols.py`): same‑broker identity; cross‑broker requires a **verified**
`CopierSymbolMap` row (fail‑closed — never by name resemblance). Broker‑specific empirical certification
(IS6FX / TradersWay / Pepperstone) is a rehearsal item; only brokers actually exercised will be claimed as
supported. Not yet empirically tested against live broker symbol metadata.

## 8–9. Open/close/SL‑TP, partial & pending

Observation: OPEN, full close, partial close, SL/TP set‑change‑remove are diffed. Intent engine
(`replication.py`) records per‑follower idempotent intents (sized volume, mapped symbol, **complete** SL+TP
from the same‑poll snapshot — an SL‑only change preserves the intact TP). B‑exec producer currently
produces **OPEN** only; CLOSE/MODIFY require a verified current copier‑owned follower ticket and are
fail‑closed (deferred). Pending orders are not observed (ingest reads executed deals only).

## 10. Disabled‑follower safety

Disable policy A (keep managing) / B (detach) / C (close) honored at the intent level: a disabled follower
takes **no new entries**; A continues managing existing, B detaches, C mirrors closes only. Unprotected‑
close authorization is account‑specific, versioned and revocable (config only; no auto‑close actuation in
this scope). No silent liquidation.

## 11. Ledger & replay protection

`CopierReplication` is idempotent per `(master_event, follower)`. Observer is single‑flight per master
(lease claimed before the read; stale/taken‑over reads discarded) so a network‑delayed older snapshot
cannot regress the baseline or fabricate closes. B‑exec is idempotent (one job per intent, row‑locked).

## 12. Actual MT5 execution test evidence

**None.** No order was placed, no DEMO bridge mutation performed, no LIVE action taken. All certification
is synthetic/unit + adversarial review. A real‑MT5 DEMO rehearsal is operator‑gated.

## 13. Real measured replication latency

**None measured** (no execution performed). Latency fields exist in the ledger for a future real run.

## 14. Frontend status & screenshots

Copy Trading page (`/accounts/[id]/copy-trading`, EN/JA) renders master, followers (opt‑in/sizing/policy/
sync + **LIVE‑execution‑blocked** badge), observed events, and replication status with truthful
**SHADOW — NO ORDER SENT** labels (never green/success) + an always‑visible execution‑limitations banner.
Live browser smoke on an isolated same‑origin preview harness with seeded DARK data — **screenshot
captured** (caught + fixed a hydration bug). 4 vitest tests, build + parity + eslint green.

## 15. CI & regression

Full backend regression green on an isolated DB (`test_copier_ci`) at each step (latest: 6200+ tests OK,
1 skipped). Frontend build + parity + eslint + 4 vitest green. GitHub CI runs on each PR head (final‑head
CI is the authoritative merge gate; local runs are supplementary).

## 16. Adversarial findings & resolutions

PD review rounds fixed, with negative tests: **B1** false FULL_CLOSE on a failed/null positions read
(bridge None→error; observer rejects non‑list/dup); **B2** out‑of‑order polls (bounded single‑flight lease
before the read + persist‑lock ownership discard); **B3** incomplete SL/TP (complete desired state from the
same‑poll snapshot); **B4** guessed constraints / unknown opcode (unknown event rejected; structured
`constraints_authoritative`/`sltp_translation_ok` flags). PR C UX: SHADOW ≠ success relabel, unmount guard,
hydration fix.

## 17. LIVE execution authorization requirements

LIVE follower execution is RED. It additionally requires: a LIVE close/modify bridge path (does not exist —
the bridge demo‑locks those handlers), a valid `LiveExecutionAuthorization`, `MT5_ALLOW_LIVE` on the
runtime, identity pin, and Sponsor + Security GO. None is granted; none is enabled by any merge.

## 18. Friday engineering readiness

Backend foundation + corrections + UX + B‑exec DEMO producer are engineered, DARK and test‑certified.
Outstanding for a certified DEMO demonstration: a real‑MT5 DEMO rehearsal (operator GO + chosen accounts).

## 19. Monday demonstration procedure

1. Show the Copy Trading dashboard (read‑only) — master + followers + limitations.
2. Show LIVE master **read‑only observation** (where a LIVE account + bridge are reachable).
3. Show DEMO/shadow follower behaviour, **clearly labelled** (SHADOW — NO ORDER SENT), with the LIVE
   follower gate shown blocked. A genuine LIVE‑money copy is **NO‑GO** and must not be implied.

## 20. Supported vs deferred

- **Supported (DARK, certified in tests):** master observation + ledger; follower config + intent engine
  (OPEN/close/partial/SL‑TP intents, sizing, verified symbol map); DEMO OPEN producer (SHADOW/armed‑DEMO).
- **Deferred:** CLOSE/MODIFY execution (needs follower‑ticket tracking); config‑write UI; broker‑specific
  empirical certification; real latency.

## 21. Production activation GO/NO‑GO

**NO‑GO.** DARK only; no deploy, no migration applied to prod, no arming.

## 22. Outstanding security & broker dependencies

LIVE close/modify bridge path (RED); real‑MT5 DEMO rehearsal (operator GO + accounts); broker metadata
sync for authoritative constraints per broker; ADR‑0051 ratification + final‑head CI before any merge.
