# GUVFX_MT5_LIVE_MASTER_FOLLOWER_REPORT

> Packet `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` (P0). Subsystem completion report.
> Author: Claude (Principal Engineer). Date: 2026‑10‑20; final-head reconciliation 2026‑10‑21;
> **#528 foundation governed-squash-merged to `main` 2026‑10‑21.**
> **All work is DARK; no production deploy, no LIVE execution, no real‑money order, no DEMO bridge
> mutation.** States per §19 are not conflated.

## 0. State summary (§19 taxonomy)

| State | Status |
|---|---|
| `ENGINEERING_COMPLETE` | ◑ **scoped only** — Observer (A) + follower‑intent (B) + four PD corrections + Copy Trading UX (C) + B‑exec DEMO OPEN producer. **The workstream is NOT engineering-complete:** CLOSE/MODIFY lifecycle execution (needs follower-ticket tracking), config-write UI, and customer activation are not built (§20, §23). |
| `DARK_MERGED` | ◑ **#528 foundation only** — governed-squash-merged to `main` `1ca72fc3` (2026‑10‑21, flags default-OFF, no prod action). #535 UX + #537 B-exec rebased onto it; their final merge heads require exact-head CI re-certification + explicit authorization (auto‑merge OFF). |
| `DEMO_CERTIFIED` | ❌ none — synthetic/unit certified; a real‑MT5 DEMO rehearsal is operator‑gated |
| `LIVE_OBSERVATION_CERTIFIED` | ❌ none — read‑only firewall foundation exists; needs a real LIVE account rehearsal |
| `LIVE_EXECUTION_CERTIFIED` | ⛔ **blocked (RED)** — no bridge close/modify path; arming reserved to Sponsor + Security |
| `PRODUCTION_DEPLOYED` | ❌ none |
| `CUSTOMER_ACTIVATED` | ❌ none |

## 1. Canonical main & PR SHAs (post #528-merge, 2026‑10‑21)

- **Canonical `main`: `1ca72fc3`** — after the governed squash-merge of PR #528. WV1 history is preserved as
  ancestors throughout (`e6117aab`, `0802e423`, and `d5e4f22e`/#539); no WV1 commit was reverted, rebased away,
  or overwritten. Reconciliation lineage: #528 reconciled onto `0802e423`, then re-reconciled onto `d5e4f22e`
  (docs-only WV1 #539; collision none — #528 never touches `docs/WV1_BOARD_DEMO_RUNBOOK.md`), pre-merge head
  `aaf8e850`.
- **PR #528** — backend foundation (observer A, follower‑intent B, corrections B1–B4, B2 single‑flight).
  **MERGED** 2026‑10‑21 as governed squash `1ca72fc3` (single-parent; 35 files, +3321/-1; flags default-OFF;
  no prod action). Pre-merge reconciled head `aaf8e850`, CI run `39568592209`: **5/5 PASS** (backend 10m42s,
  frontend 2m6s, governance, market-data, research).
- **PR #535** — Copy Trading UX + read‑only API (branch `feat/mt5-copier-ux`). **Rebased `--onto 1ca72fc3`**
  → head `505cbb7c` (old head `0beb5999` retained for recovery). Diff vs `main` = 14 files/+829, the UX +
  read-only API increment only; **no duplicate #528 commits**; `AccountWithdrawalMonitoring.tsx` (WV1)
  untouched — canonical main version preserved. The `window is not defined` defect (§16 F0) stays resolved
  because main now carries the canonical WV1 unmount guard. Exact-head CI re-certification pending on `505cbb7c`.
- **B‑exec (PR #537)** — DEMO ExecutionJob producer (branch `feat/mt5-copier-exec`). **Rebased `--onto 1ca72fc3`**
  → code (old head `d19bff52` retained for recovery). Diff vs `main` = 9 files/+930/-5, B-exec + governance
  entries + this completion report only; **no duplicate #528 commits**. This report commits on top of the code,
  so the branch head advances per doc update — the authoritative exact-head result is always the latest run in
  the PR's own checks. Local full regression (pre-rebase, code unchanged by rebase): **6296 tests OK**.
  Exact-head CI re-certification pending on the rebased head.
- #528 merged; #535/#537 rebased and **not** merged. Their merges remain gated on exact-head CI re-cert +
  ADR‑0051 approval path + explicit human authorization; auto‑merge OFF.

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

## 9a. Complete trade‑lifecycle matrix (code‑backed)

Observed = diff engine + ledger; Intent = `replication.py`; Execution = `execution_producer.py` (DARK,
SHADOW/armed‑DEMO); Reconciled = broker‑result reconciliation; DEMO cert = real‑MT5 DEMO rehearsal.

| Master operation | Observed | Intent | Execution | Reconciled | DEMO cert |
|---|---|---|---|---|---|
| Open | ✅ `diff.OPEN` | ✅ OPEN intent | ✅ producer OPEN (P0‑hardened) | ❌ gated | ❌ synthetic only |
| Increase (scale‑in) | ✅ `VOLUME_INCREASE` | ✅ SCALE_IN intent | ❌ producer OPEN‑only | ❌ | ❌ |
| Partial close | ✅ `PARTIAL_CLOSE` | ✅ intent | ❌ needs follower ticket | ❌ | ❌ |
| Full close | ✅ `FULL_CLOSE` | ✅ intent | ❌ needs follower ticket (+bridge demo close) | ❌ | ❌ |
| Set/change/remove SL | ✅ | ✅ complete same‑poll state | ❌ bridge modify = breakeven‑only | ❌ | ❌ |
| Set/change/remove TP | ✅ | ✅ complete same‑poll state | ❌ bridge modify = breakeven‑only | ❌ | ❌ |
| Disable policy A (keep managing) | n/a | ✅ | ✅ intent‑level | ❌ | ❌ |
| Disable policy B (detach) | n/a | ✅ | ✅ intent‑level | ❌ | ❌ |
| Disable policy C (close) | n/a | ✅ | ❌ needs close execution | ❌ | ❌ |
| Restart/recovery | ✅ single‑flight + stale‑discard + freshness | ✅ | ✅ producer rejects stale/superseded | ❌ | ❌ |
| Duplicate / lost‑ACK | ✅ idempotent ledger | ✅ | ✅ one job per intent (row‑locked) | ❌ | ❌ |
| Pending orders | ❌ not observed (deals only) | ❌ | ❌ | ❌ | **deferred** |

Evidence: `trade_copier/diff.py` + `tests_diff.py`; `replication.py` + `tests_replication.py`;
`execution_producer.py` + `tests_execution_producer.py`; `observer.py` + `tests_observer.py`. No cell is
`DEMO cert` ✅ — no real‑MT5 DEMO run has occurred.

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

**Local (supplementary):** full backend regression green on an isolated DB (`test_copier_ci`) at each step
(latest #537-head run `3bb2e609`: full suite in progress at report time; prior green at 6200+ tests OK, 1
skipped). Frontend build + parity + eslint + vitest green. The six `window.localStorage.clear is not a
function` failures seen locally are pre-existing jsdom flakes present on `main`, **not** introduced here and
**green in CI** (confirmed by #535 `frontend` PASS below); they are not the `window is not defined` defect.

**GitHub CI — exact-head, authoritative merge gate — ALL FIVE JOBS GREEN ON EACH HEAD (2026‑10‑21):**

| PR | Head | Run | backend | frontend | governance | market-data | research |
|---|---|---|---|---|---|---|---|
| #528 | `fd6c4006` | `39563423956` | ✅ 10m46s | ✅ 2m19s | ✅ | ✅ | ✅ |
| #535 | `0beb5999` | `39563423762` | ✅ 12m46s | ✅ 2m5s | ✅ | ✅ | ✅ |
| #537 | `e57d7704` | `39564585714` | ✅ 8m52s | ✅ 2m8s | ✅ | ✅ | ✅ |

15/15 jobs passed. The #537 row's head `e57d7704` is the first all-green post-correction doc commit (atop
code head `3bb2e609`); because this report commits on top of #537, its head advances by one doc commit per
update, and each push re-runs CI with no code/test delta — the authoritative exact-head result for the
current head is always the latest run in the PR's own checks.

The decisive result: **#535 `frontend` PASS at `0beb5999`** proves the `window is not defined` unmount defect
(§16 F0) is resolved at the exact head, with the full frontend suite, not only copier tests. Green CI is a
gate, **not** an authorization to merge — merge remains gated on ADR‑0051 ratification + explicit human
authorization; auto‑merge OFF.

## 16. Adversarial findings & resolutions

PD review rounds fixed, with negative tests: **B1** false FULL_CLOSE on a failed/null positions read
(bridge None→error; observer rejects non‑list/dup); **B2** out‑of‑order polls (bounded single‑flight lease
before the read + persist‑lock ownership discard); **B3** incomplete SL/TP (complete desired state from the
same‑poll snapshot); **B4** guessed constraints / unknown opcode (unknown event rejected; structured
`constraints_authoritative`/`sltp_translation_ok` flags). PR C UX: SHADOW ≠ success relabel, unmount guard,
hydration fix.

**F0 — #535 `window is not defined` CI failure (final-head reconciliation).** The `feat/mt5-copier-ux`
branch predated canonical `main` `0802e423` and carried a stale `AccountWithdrawalMonitoring.tsx` without the
WV1 mounted/unmount guard, so the full frontend suite threw `window is not defined` during async unmount in
CI. **Resolution:** reconciled the stack forward onto `0802e423`, which brings main's canonical WV1 unmount
fix (mountedRef, lines ~98–127) — **the WV1 guard was adopted, not weakened or replaced**. New head
`0beb5999`; CI `frontend` now PASS (2m5s), full suite (§15). No WV1 report/runbook/evidence or commit
(`e6117aab`, `0802e423`) was reverted or overwritten.

**#537 B‑exec independent review (2026‑10‑21), reviewed against the actual diff — findings fixed with
negative tests:** **F1** follower-disable race — `produce_one` now `select_for_update(of=("self",))` AND
freshly re-reads the follower row inside the lock before re-running admission, so a follower disabled between
intent and produce is rejected (not executed on a stale in-memory row). **F2** cross-broker symbol map not
re-verified at produce time — admission re-resolves the follower symbol via `resolve_follower_symbol`; an
unverified/absent map is rejected at produce, not only at intent. **F3** instrument readiness — produce
re-checks `BrokerInstrument` enabled state; a disabled/absent instrument is rejected. **F4** job
serialization contract — added `test_job_serialization_contract` pinning the emitted payload shape. All 22
producer tests + 119 trade_copier/governance tests green locally; entrypoint drift guard + WP6 matrix updated
for `trade_copier/execution_producer.py::produce_one`.

**#537 B‑exec producer (PD Friday review), with negative tests:** **P0‑A** DARK flag bypass — `produce_one`
now enforces `copier_execute_enabled()` at entry AND under the lock; mode captured once (flags‑OFF direct‑call
proves no job/mutation). **P0‑B** stale‑intent/config TOCTOU — ALL admission re‑run inside the atomic
boundary on the locked, re‑read replication; an OPEN superseded by a later CLOSE or outside the freshness
window is rejected. **P0‑C** payload — numeric positive `lots` sized against fresh constraints, `max_lot` cap
+ `CPY<id>` provenance; a clamp (silent shrink) or non‑positive volume is rejected. **P0‑D** protection —
complete, directionally‑valid SL+TP from the authoritative snapshot + fresh authoritative constraints
required. **P1** SHADOW semantics — a suppressed job sets `SHADOW_JOB_RECORDED` (not `EXECUTION_REQUESTED`);
UI renders "SHADOW JOB RECORDED — NO ORDER SENT", never green/success.

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

## 23. CLOSE/MODIFY & follower‑ticket — gated design (STOP boundary)

The master/follower lifecycle is not fully executable until follower CLOSE/MODIFY are safe. What remains:

- **Durable follower‑ticket tracking** (master position identity → the exact follower position ticket),
  recorded only AFTER broker‑result reconciliation proves the follower position was opened by THIS copier.
  This is **buildable DARK as data + reconciliation** (no new bridge mutation) BUT can only be populated by
  a real DEMO execution → it waits on the operator‑gated DEMO rehearsal. Design: extend `CopierReplication`
  (or a `CopierFollowerPosition` row) with the reconciled follower ticket + broker fill evidence; a
  reconciliation pass reads the follower's own identity‑firewalled snapshot and binds the ticket by the
  `CPY<id>` provenance comment; owner/account/node/binding re‑validated; orphaned/ambiguous/manually‑changed
  positions rejected; a master ticket is never used against a follower account.
- **Full CLOSE (DEMO):** once a verified follower ticket exists, a `CLOSE_TRADE` job targets the follower
  ticket only (the bridge's demo `close_position` already exists). Partial close validates the close volume.
- **General SL/TP MODIFY:** **blocked at a bridge‑authority boundary.** The existing bridge
  `modify_position` is **risk‑reducing / breakeven‑only** — it refuses SL widening and does not support TP
  removal or general SL/TP replication. Implementing general copier SL/TP replication requires **broadening
  the bridge's authorized mutation capability**, which I will NOT do autonomously.

**Gated increment for general MODIFY (Sponsor + Security decision required):**
- *Exact missing capability:* a demo‑only, identity‑pinned bridge `modify_sltp` that can set/change/remove
  SL and TP on a verified copier‑owned follower ticket, within a pre‑authorized protection policy.
- *Proposed safety contract:* demo‑only wall preserved for LIVE; identity pin at send time; reject any
  change that would weaken an approved stop beyond policy; verified follower ticket + owner/account/node;
  idempotency key; re‑read current broker state before mutate; audited.
- *Tests:* SL‑widen rejected; TP‑remove per policy; wrong/absent ticket rejected; lost‑ACK/duplicate; demo
  wall holds for LIVE; send‑time re‑validation.
- *Threat analysis:* a broadened mutation path is a new LIVE‑adjacent surface — must stay demo‑locked and
  fail‑closed; risk of weakening protection, cross‑account mutation, replay.
- *Migration/rollback:* additive bridge handler behind a default‑OFF flag; rollback = disable flag + revert
  handler; no schema change on the critical path.
- *Decision required:* Sponsor + Security GO before building or deploying the broadened bridge mutation.
