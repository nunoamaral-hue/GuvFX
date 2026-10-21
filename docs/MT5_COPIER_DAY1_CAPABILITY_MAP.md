# MT5 Master/Follower Copy Trading — Day‑1 Capability Map, Collision Gate & Data Contract

> Packet: `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` (P0, 2026‑10‑20).
> This is the **Tuesday (day‑1)** deliverable required by §17: collision gate, repository
> assessment, master‑observer capability map, execution‑safety reuse, master/follower data
> contract, and the honest Friday/Monday feasibility verdict. **No behaviour‑changing code
> is described here.** Read‑only reconnaissance only; grounded in file:line evidence, not
> assumption (per `.claude/rules/` + CLAUDE.md governance overlay). See decision in
> `docs/ADRs/0051-mt5-master-follower-copier.md`.

Author: Claude (Principal Engineer). Date: 2026‑10‑20.
Worktree: `.claude/worktrees/mt5-copier-foundation`, branch `worktree-mt5-copier-foundation`.

---

## 0. Governance posture (read first)

- This packet is **not in `packets/ACTIVE.md`** (now registered additively, 2026‑10‑20) and has no
  in‑session Notion packet. A new execution‑adjacent subsystem is **Amber → RED‑adjacent** and
  required an explicit recorded decision before build — ADR‑0051 + Programme Director scope GO.
- The packet is explicit and correct: **engineering is DARK; real‑money is a separate Sponsor +
  Security GO.** Standing rules forbid LLM‑authored live orders, broker‑credential access,
  risk‑limit changes, and model promotion without Nuno's explicit gate.
- **Treatment:** the copier is an **additional governed strategy source** emitting **enqueue‑only,
  flag‑gated (default‑OFF)** commands through the *existing* authorized execution path. It never
  calls `order_send`, never removes a demo wall, never enables LIVE execution under a merge.

## 1. Baseline & collision gate (§4)

| Item | Finding |
|---|---|
| Canonical `main` | **`5b209833`** (#525 "account‑level broker‑email contact API"; advanced from `43657246`/#524 during the session — verified via fetch, per the packet's "verify latest canonical main"). |
| Working branch | `worktree-mt5-copier-foundation`, isolated git worktree cut from latest `origin/main`. |
| Parallel Withdrawal/BI workstream | Actively merging to `main` (#517–#525) — **no copier collision** (disjoint apps/files). |
| Existing copier code | **None.** No `master`/`follower`/`copier` models, migrations or branches exist — genuinely new subsystem. |
| Migration collision risk | New app ⇒ own migration sequence (`0001`); no shared‑app migration edits for the core models. |
| Infra reachability (read‑only, 2026‑10‑20) | Tailnet up; **VPS `100.119.23.29:22` reachable; MT5 bridge `100.79.101.19:8788` reachable**; agent `:8787` not listening. Real‑MT5 DEMO infra is **present**; specific demo‑account/binding/readiness inventory deferred to the PR‑D rehearsal gate (read‑only prod query). |

**Collision verdict: CLEAR.** Dedicated worktree + new Django app isolate this from all in‑flight streams.

## 2. Execution‑path reuse map (§3) — what we inherit

The real order path is **signal‑driven** (`strategies/execution.py` is a placeholder raising
`ExecutionEngineNotImplementedError`). Every command funnels through one fail‑closed chain:

```
signal_acquired → auto_router.route_acquired_signal → PendingSignalApproval
  → approve() → signal_planning.plan_demo_execution()      # SignalExecutionPlan + ProposedOrderLeg ONLY (no job)
  → signal_promotion.promote_plan_to_shadow_jobs()          # PLACE_ORDER_SHADOW (suppressed, no order)
  → ExecutionJob.save()                                     # THE single creation gate
  → worker next_job → order_transport → mt5_signal_bridge.process_job
  → validate_job_safety → verify_execution_binding → mt5.order_send
```

Reusable gates inherited for free:

- **`ExecutionJob.save()`** (`backend/execution/models.py:275`) — kill switch, Stop‑Trading,
  broker gate, broker‑runtime‑pause, identity‑pin. `JobType` already has `PLACE_ORDER`,
  `CLOSE_TRADE`, `MODIFY_POSITION`, `PLACE_ORDER_SHADOW` (`models.py:167`). Kill switch &
  Stop‑Trading deliberately **exclude** close/modify (`models.py:378,425`) so risk can be flattened.
- **LIVE wall** `live_execution_permitted(account)` (`execution/live_authz.py:57`), 5 layers, default OFF.
- **`LiveExecutionAuthorization`** (`models.py:1486`) — per‑account, immutable, revocable, fail‑closed.
- **Source‑scoped fan‑out** `_resolve_targets(mode, source)` (`auto_router.py:222`) — master→N‑followers.
- **Risk controls** `evaluate_promotion_risk` (`risk_controls.py:367`) — exposure/symbol/open/
  drawdown/free‑margin/concurrent, fail‑closed.
- **Sizing** (3 layers): `calculate_lot_size` (`signal_engine.py:349`), `SignalSourceConfig` caps
  (`models.py:826`), `AssignmentLegSizing` + `validate_lot` (`strategies/models.py:614,644`).
- **Symbol resolve** `can_account_trade_symbol` (`broker_symbols.py:87`) + `BrokerInstrument`
  per‑account cache (`models.py:1246`), base‑symbol suffix normalization, fail‑closed.
- **DARK flag idiom** `_flag()` (settings→env, default OFF; `hosted_workspace/flags.py:18`). No `beta.env` in repo.
- **Ownership/env truth** — `TradingAccount.user` (`trading/models.py:99`); gate DEMO/LIVE only via
  `account_policy.is_demo_environment()/is_live_environment()` (`trading/account_policy.py:58,69`).

## 3. Master‑observation capability map (§5) — the crux, and the gap

Observation today is **poll‑only** on a history‑deals snapshot; **no MT5 event callbacks**.
Cadence ~1/min only for accounts holding an open GuvFX plan (`breakeven.sweep_breakeven →
_ensure_position_sync`); a purely manual/EA master with no GuvFX plan is **not polled on a schedule**.

| Master operation required (§5/§10) | Observable today? | Evidence / gap |
|---|---|---|
| Position OPEN (BUY/SELL) | ✅ | `position_ingest.py:121` |
| Full CLOSE | ✅ | `position_ingest.py:131` |
| Partial close | ❌ | Collapses to OPEN at full volume (`position_ingest.py:131`) |
| Partial fill (entry) | ❌ | Volume‑weighted into one open (`:116`) |
| SL set/change/remove | ❌ | No durable SL field; only in transient `/mt5/positions` read (`analytics/views_trade_history.py:194`) |
| TP set/change/remove | ❌ | Same as SL |
| Trailing‑stop movement | ❌ | Consequence of SL gap |
| Pending orders (create/modify/cancel/expire/reject) | ❌ | Ingest reads executed deals only |
| Broker‑triggered SL/TP/stop‑out close | ⚠️ | Close lands, but `deal.reason` never read — reason unknown |
| Event history / ordering | ❌ | No deal/event ledger; `Trade` is a mutable per‑position upsert |
| Durable open‑positions snapshot | ❌ | Only account‑level `AccountEquitySnapshot` (DARK) |
| Origin manual vs EA vs script | ❌ | Indistinguishable unless GuvFX magic (≥1e9)/`WAY…` comment; `opened_by` hard‑coded `"EA"` |
| Broker‑time correctness | ⚠️ | deal times stored as UTC but broker‑TZ (UTC+3); only `Trade.close_ingested_at` is server‑UTC |

**The bridge already CAN return SL/TP/live price** (transient read) and read‑only LIVE monitoring is
permitted by the snapshot identity firewall — **nothing persists, sequences or diffs it.** So the
master observer is a **substantial new read‑only build**; it is the foundation that gates everything.

## 4. Master/follower data contract (§9, §17)

Conceptual; finalized in PR A. Raw observation immutable (`.claude/rules/data.md`).

**Master (observation, read‑only — never mutates the master):**
- `MasterPositionSnapshot` — per poll: `{position_ticket, symbol, side, volume, open_price, sl, tp,
  current_price, profit, swap, commission, magic, comment, observed_at, observed_login,
  observed_server, poll_seq}` — the durable open‑position store that does not exist today.
- `MasterEvent` (append‑only, monotone per‑account `sequence`): `{event_uuid, account, sequence,
  event_type ∈ {OPEN, PARTIAL_CLOSE, FULL_CLOSE, SL_SET/CHANGE/REMOVE, TP_SET/CHANGE/REMOVE,
  PENDING_CREATE/MODIFY/CANCEL/EXPIRE, BROKER_CLOSE}, position_ticket, deal_id?, symbol, side,
  volume_delta, prev_sl, new_sl, prev_tp, new_tp, reason?, origin?, observed_at, from_snapshot}` —
  derived by diffing consecutive snapshots + the deals feed.

**Follower (configuration + replication):**
- `CopierFollower` — `{master (FK→TradingAccount), follower_account (FK→TradingAccount), owner
  (MUST equal master.user — cross‑user rejected), enabled, sizing_mode, fixed_lot, multiplier,
  max_volume, max_aggregate_exposure, max_concurrent_positions, max_spread, max_slippage,
  symbol_map_policy, sltp_translation_policy, disable_policy ∈ {A_KEEP_MANAGING, B_DETACH, C_CLOSE},
  unprotected_close_authorization (versioned, revocable, account‑specific), sync_status,
  last_outcome, audit}`.
- `CopierReplication` (ledger; idempotency key **unique per `(follower, master_event)`**) —
  `{master_event, follower, requested_op, sizing_result, execution_job?, request_state ∈
  {REQUESTED, ACCEPTED, EXECUTED, VERIFIED, FAILED}, broker_result, follower_ticket,
  residual_exposure, latency_ms, reconciliation_status}`.
- `CopierSymbolMap` — explicit **verified** cross‑broker equivalence `{master_broker, master_symbol,
  follower_broker, follower_symbol, verified_by, verified_at}` (no shared equivalence table today;
  §8 forbids name‑resemblance selection).

**Cycle prevention (§5):** copier‑origin follower trades (copier magic/comment) are excluded from
generating master events, so a copied trade can never be copied back.

## 5. Live/demo execution reality (§13) — what blocks LIVE

- The signal `PLACE_ORDER` path *can* place a LIVE order only when fully armed (`MT5_ALLOW_LIVE=1`
  + valid `LiveExecutionAuthorization` + identity pin + all DARK flags ON) — arming is **RED**,
  reserved to Nuno + Security; no live credentialed broker login has ever succeeded in prod.
- **`close_position`/`modify_position` on the bridge are unconditionally demo‑locked** — no
  `MT5_ALLOW_LIVE` path. Since SL/TP replication and master‑following closes are core, **LIVE
  follower close/SL/TP needs new bridge execution‑path work that does not exist** (RED‑adjacent, out of scope).
- Read‑only LIVE **monitoring** of a master *is* supported (firewall); a LIVE follower cannot be mutated.

## 6. DARK‑buildable by Friday vs blocked

Buildable now (DARK, read‑only or SHADOW/DEMO, flag‑gated default‑OFF): **PR A** master observer +
ledger; **PR B** follower config + replication engine + risk (enqueue‑only SHADOW→DEMO); **PR C**
Copy Trading UX; **PR D** cross‑broker cert + demo rehearsal.

Blocked: LIVE follower execution (RED arming); LIVE follower close/SL/TP (no bridge path); real‑MT5
cert depends on a reachable demo bridge + accounts (rehearsal‑gated) — else synthetic/unit only.

## 7. Honest Friday / Monday verdict (§2, §15)

- A genuine **LIVE‑money copy demo is NO‑GO by Friday** — the LIVE execution gate has never passed
  and LIVE close/SL/TP has no bridge path.
- Strongest certifiable Monday demo = the packet's own §15 fallback: **LIVE master read‑only
  observation + DEMO/shadow followers executing real DEMO orders, clearly labelled**, with the LIVE
  follower gate shown blocked. Never present shadow/DEMO as LIVE follower execution.

## 8. Certification state taxonomy (§19) — current

| State | Status |
|---|---|
| `ENGINEERING_COMPLETE` | In progress (PR A starting) |
| `DARK_MERGED` | None |
| `DEMO_CERTIFIED` | None (needs PR A+B + a demo bridge rehearsal) |
| `LIVE_OBSERVATION_CERTIFIED` | None (foundation exists: read‑only firewall) |
| `LIVE_EXECUTION_CERTIFIED` | **Blocked** (RED; no bridge close/modify path) |
| `PRODUCTION_DEPLOYED` | None |
| `CUSTOMER_ACTIVATED` | None |
