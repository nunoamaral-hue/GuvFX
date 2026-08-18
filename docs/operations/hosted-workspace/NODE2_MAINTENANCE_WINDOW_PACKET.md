# Node-2 Dispatch Worker — Maintenance-Window Packet (for Nuno's approval)

**Status: AWAITING MAINTENANCE-WINDOW APPROVAL.** No production file has been touched. This packet is
the deliverable of Boundary B0 "preparation only": it defines the ordered, rollback-safe sequence to
(1) rotate the exposed shared agent token, (2) deploy the durable Node-2 dispatch worker, and (3)
prove heartbeat + node-only claimability with **zero orders**, then close claim authority and STOP.

No token or password value appears in this document.

## 0. Why a maintenance window is required

The shared inbound agent token gates **Customer Zero's live `:8788` bridge**. Rotating it requires a
controlled restart of CZ's bridge (SACRED, live-trading) plus every VPS consumer. `BRIDGE_TOKEN_ROTATION_PLAN.md`
(rev 2) mandates **"Nuno's maintenance-window approval, obtained before any file is touched."** That
approval is what this packet requests. This is the exact operation whose earlier failure produced
permanent RULES 1–11 (`POST_INCIDENT_REVIEW_BRIDGE_TOKEN.md`).

## 1. Secret-exposure record (this session, 2026-08-18)

Read-only production investigation inadvertently surfaced the following into the session transcript
(redaction regexes missed them). None was written to a file I created or committed to Git.

| Secret | Prior exposure | Action |
|---|---|---|
| Shared agent token (`GUVFX_AGENT_TOKEN` / `GUVFX_WINDOWS_AGENT_TOKEN`) | **Already public in Git history `67de147`** (2026-07-22 leak); rotation was already pending | Rotate per `BRIDGE_TOKEN_ROTATION_PLAN.md` (this packet) |
| `DB_PASSWORD` / `GUAC_ADMIN_PASS` / `GUAC_MT5_PASS` | In the VPS `docker-compose.yml` (trusted host) | Consider rotating in the same window; out of scope for the node-2 worker but recorded here |

The agent-token exposure is a **re-surfacing of an already-public, already-remediation-pending leak**,
not a new emergency. It should still be rotated — removing literals never revokes a leaked token.

## 2. Complete token consumer map (from `docs/SECRET_INVENTORY.md`)

The leaked credential is one wire-level value under two env names that MUST stay equal:

- **`GUVFX_AGENT_TOKEN`** (bridge-side name — the value the bridge *validates* on inbound):
  - Windows: `C:\GuvFX\secrets\bridge.tokens.bat` (SYSTEM+Administrators only)
  - Consumers: CZ `:8788` bridge (validates); `bridge_watchdog.ps1`; launchers
    `start_signal_bridge.bat`, `guvfx_autostart.bat`, `guvfx_autostart_bridge_only.bat`,
    `start_signal_bridge_is6.bat`; **node-2 `:8789` bridge + `node2_bridge_watchdog.ps1`**
    (shared-token exception, via `C:\GuvFX\node2\start_node2_bridge.bat`)
- **`GUVFX_WINDOWS_AGENT_TOKEN`** (alias `WINDOWS_AGENT_TOKEN`) (client-side — the value clients *send*):
  - VPS: `/home/ubuntu/guvfx-prod/bridge-agent.env` (mode 600)
  - Consumers (5): `guvfx-backend`, `guvfx-mt5-trade-ingest-worker`, `guvfx-mt5-validate-worker`,
    `guvfx-mt5-shadow-worker`, `guvfx-wayond-listener`
  - **+ the NEW `guvfx-mt5-node2-order-worker`** once deployed (this packet)

Separately, `MT5_WORKER_TOKEN` (worker→backend `X-Worker-Token`, legacy path) is a *different* token
and was **not** exposed; do not rotate it in this window unless separately decided.

## 3. Ordered execution sequence (single bounded window)

Follow `BRIDGE_TOKEN_ROTATION_PLAN.md` rev 2 §9 exactly. Summary:

1. **Approval + backups.** Verified `pg_dump` (sha256); backend image rollback tag; back up
   `bridge-agent.env` and `C:\GuvFX\secrets\bridge.tokens.bat` (secured, off-repo).
2. **Launcher Gate (RULE 8).** On the Windows host, prove **every** `.bat`/`.ps1` launcher and both
   watchdogs (`:8788` and `:8789`) parse and reference the token file correctly, BEFORE any restart
   (a tokenless `.bat` → `exit 1` → watchdog restart-loop is how CZ went down before).
3. **Pause intake.** Stop `guvfx-wayond-listener` (and/or pause the bound assignment) so no signal
   flows during the window.
4. **Generate + place the new token** (never argv/echo/log): update `C:\GuvFX\secrets\bridge.tokens.bat`
   (both `:8788` and `:8789` read it) and `/home/ubuntu/guvfx-prod/bridge-agent.env`. Run the §6
   configuration-validation gate (both sides' digests must match; abort on `ABSENT`/short/mismatch).
5. **One controlled restart** of the token consumers: CZ `:8788` bridge + node-2 `:8789` bridge
   (via their scheduled tasks — RULE 1), then the **4 non-intake** VPS consumers (`docker compose up -d`
   recreate `guvfx-backend`, `guvfx-mt5-trade-ingest-worker`, `guvfx-mt5-validate-worker`,
   `guvfx-mt5-shadow-worker`). **DO NOT restart `guvfx-wayond-listener` here** — it stays STOPPED (from
   step 3) so intake remains paused into §4. Its env-file already carries the new token (step 4); it is
   started with that token only at step 12 when intake resumes. Starting it now would re-open the
   live-order window §4 depends on being closed. The §7 rollback never reinstates the leaked token.
6. **Post-rotation proof (§10):** every consumer authenticates on the new token; old token now 401s;
   no secret in Git/logs/argv; CZ execution restored.
7. **DO NOT resume intake yet.** Keep `guvfx-wayond-listener` (and the bound assignment) **paused**
   straight into §4. Resuming here would let a fresh signal enqueue a node-2 PLACE_ORDER *after* the
   §4 reconcile-to-0, which the just-deployed ACTIVE worker would claim and `order_send` — a live
   order during a NO-ORDER window. Intake stays paused until the B0 STOP (step 12).

## 4. Node-2 worker deployment + B0 no-order commissioning (intake STILL paused throughout)

> **Invariant for all of §4: signal intake remains PAUSED.** The B0 no-order guarantee depends on the
> node-2 queue staying empty while the worker is claim-capable; a point-in-time reconcile is not enough
> if new signals can still arrive. Do not re-enable the listener or the assignment until step 12.

8. **Reconcile stale node-2 queue** (hard ordering): with **no** node-2 worker live AND intake paused,
   dry-run then apply `reconcile_stale_preactivation_orders --account-id <acct25> --apply` until node-2
   PENDING/RUNNING PLACE_ORDER = **0**. (The hardened reconciler refuses if a node-2 worker is
   registered; if so, temporarily `REVOKED` the identity, reconcile, then re-commission — see §5.)
9. **Deploy the worker** per `deploy/node2-dispatch-worker/README.md` (supported orchestration).
10. **Certify heartbeat/claimability with an EMPTY queue** — `last_seen` fresh, `authorized_nodes ==
    ['guvfx-beta-node-1']`, `node_execution_operational(node2, require_live=True)` TRUE,
    `execution_path_state(acct25)` ready, health scan clears `NODE_WORKER_STALE` /
    `NODE_PENDING_NO_CLAIMANT`. **Prove 0 claimed, 0 order_send, 0 trades** (queue empty + intake paused).
11. **Close claim authority** (stop the worker or REVOKE the identity) so no silent live-order window
    is left open.
12. **Only now resume intake** — re-enable the watchdog + `guvfx-wayond-listener` (+ the assignment).
    With claim authority already closed, any new node-2 signal simply queues (no live claimant), exactly
    the safe pre-Boundary-B posture. **STOP.**

## 5. Reconcile-vs-commission ordering note (design consequence of the ADR-0048 hardening)

The stale reconciler now refuses (`node_worker_registered`) once a node-2 worker identity is
registered — this is the intended reconcile-before-commission guard. Because Boundary A already
commissioned `mt5-node2-order-1`, cleaning a *fresh* stale queue requires temporarily setting the
identity `REVOKED`, reconciling, then re-commissioning (idempotent). This is safe and reversible; it
is called out here so the operator expects the refusal rather than treating it as an error.

## 6. Sacred invariants (must hold throughout)

- Customer Zero (acct 1) and account 18: **no config/authority change**; only the token-driven bridge
  restart touches CZ, and it returns to the identical Golden fingerprint.
- No customer authorization, no arm, no signal replay/manufacture, no manual ExecutionJob/Trade,
  and **no `order_send`** during B0. Live claiming is Boundary B (separate approval).
- `HOSTED_EXECUTION_PATH_GATE_ENABLED` stays DARK.

## 7. Boundary B follow-on (separate approval)

Re-enable node-2 claiming → wait for the next fresh real TI signal (<120s at dispatch) → trace
claim → hosted-auth → `:8789` bridge → `order_send` → MT5 → GuvFX Trade → certify first fill →
prove a second fresh signal executes without operator repair.
