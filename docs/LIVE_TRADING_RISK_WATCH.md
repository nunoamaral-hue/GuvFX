# Live Trading Path — Standing Risk Watch

> A safety reference for the **live, money-moving** trading/execution path (the GREEN
> *Trading* capability). It records **how to stop trading immediately**, the known
> single points of failure, and where recovery steps live. Facts are grounded in the
> code (cited by `file` + symbol); anything that could not be confirmed in this
> repository is listed under "Not verified here". Documentation only — it changes no
> behaviour and is not a substitute for the governance decision path.
>
> Last reviewed: **2026-06-28** (Claude Code, acting PM), against `main`, from a
> read-only code investigation (not memory).

## Why this exists

The *Trading* domain is GREEN/production and a **real order path exists today** via
the Windows bridge — governed by the **legacy** programme and **not yet reconciled**
with the target execution architecture (Blueprint doc 06). See "Live Trading path
governance gap" in `docs/STATUS.md`. Until that reconciliation (a future ADR), this
page is the operator's quick reference for stopping and recovering the live path.

## The live order path (verified in code)

Strategy/signal → execution job → worker → Windows bridge → MT5 terminal → broker:

1. An open-trade request creates an `ExecutionJob` — `backend/execution/views.py`
   (`CreateOpenTradeJobView`); model + lifecycle (`PENDING → RUNNING → SUCCESS|FAILED`)
   in `backend/execution/models.py` (`ExecutionJob`).
2. The strategy poller enqueues jobs **only for active accounts** —
   `backend/execution/management/commands/poll_strategies.py` (filters
   `TradingAccount.is_active=True`).
3. The MT5 worker polls `/api/execution/jobs/next/`, claims a job and processes it —
   `mt5_worker/mt5_worker.py` (`main_loop`, `process_job`).
4. The worker reaches the broker through a **single** Windows agent / signal bridge
   endpoint, configured by `GUVFX_WINDOWS_AGENT_BASE_URL` (a single private-range
   address) with a `GUVFX_WINDOWS_AGENT_TOKEN` header —
   `backend/guvfx_backend/settings.py`.

> In *this* repository the worker's actual `order_send` is a stub/dummy
> (`mt5_worker/mt5_worker.py`); the real order placement runs in the **external**
> Windows bridge (separate private repo). The live path is therefore real in
> production even though its order-sending code is not in this repo.

## 🛑 How to STOP live trading now (fastest → most surgical)

| # | Action | Effect | Where |
|---|--------|--------|-------|
| 1 | **Invalidate the worker token** — unset/rotate `MT5_WORKER_TOKEN`, then restart the backend | The worker can claim **no** jobs at all — hardest global stop | `backend/execution/views.py` (`IsAuthenticatedOrWorkerToken`) |
| 2 | **Stop the `mt5_worker` process** | No jobs are claimed (queued jobs simply wait) | `mt5_worker/mt5_worker.py` |
| 3 | **Disable the account** — set `TradingAccount.is_active = False` (admin/API) | No new jobs are enqueued for that account | `backend/trading/models.py` |
| 4 | **Disable the strategy assignment** — `StrategyAssignment.is_active = False` | No OPEN_TRADE/SYNC jobs for that assignment | `backend/strategies/models.py` |

For an **immediate, total halt**, prefer **#1 (token) + #2 (stop worker)**. #3/#4 are
surgical per-account / per-strategy stops. **None of these close existing open
positions** — closing is a separate `CLOSE_TRADE` job or a manual action at the broker.

## Single points of failure (verified in code)

- **One MT5 instance per account** — `TradingAccount.mt5_instance` FK + the unique
  constraint `uniq_active_account_per_instance` (`backend/trading/models.py`). If the
  instance fails, its accounts cannot trade.
- **One Windows agent / bridge endpoint** — a single `GUVFX_WINDOWS_AGENT_BASE_URL`
  routes all order/backtest traffic (`backend/guvfx_backend/settings.py`); no failover.
- **Desktop-session dependency** — the MT5 terminal + bridge require the autologon/kiosk
  console session; a console logoff disrupts them until autologon restores
  (ADR-DATA-017; `docs/KNOWN_ISSUES.md`). **Treat a console logoff as a live-impacting
  action.**
- **Single worker auth vector** — all worker access is gated by one `MT5_WORKER_TOKEN`
  (`backend/execution/views.py`).
- **Single operational database** — all `ExecutionJob` state lives in one PostgreSQL;
  a DB outage halts the whole pipeline.

## Recovery

- **MT5 desktop / VPS production restart, health checks, handoff-mount sanity:**
  `docs/RUNBOOK.md` (MT5 Free Desktop + VPS Production sections).
- **Operator discipline / session procedures:** `docs/CLIVE_RUNBOOK.md`.
- **Known operational issues + workarounds** (Guacamole mouse, session dependency,
  stale Wine processes): `docs/KNOWN_ISSUES.md`.
- **Bridge recovery after a console logoff:** autologon + on-logon tasks restore the
  MT5 terminal and the signal bridge (verified to self-heal in the 006D-R0 health
  check); if not, restore the desktop session per `docs/RUNBOOK.md`.

## Governance status

The live path is **legacy-governed** and not yet under the new packet model. Blueprint
doc 06 requires reconciling the target execution architecture with this existing
implementation **before** any execution-layer packet — a near-term **Amber ADR**
(owner-reviewed). No LLM has live-order authority; promotion of any strategy to
limited-live/production remains a **Red**, sponsor-gated action.

## Not verified here

- The real `order_send` lives in the external Windows bridge (private repo), not this
  repo; the in-repo worker call is a stub.
- No explicit demo-vs-live routing was found in the execution worker based on
  `TradingAccount.is_demo`.
- Broker credential encryption/decryption (`backend/mt5/crypto.py`) was referenced but
  not read in this pass.
- There is **no dedicated, scripted "panic stop" command** — the stops above are the
  mechanisms that exist today. A one-command kill-switch is a candidate hardening item.
