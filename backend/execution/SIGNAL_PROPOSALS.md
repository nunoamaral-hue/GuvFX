# EXEC-E1a — approval → ProposedSignalOrder bridge (NO order is ever placed)

The execution-side bridge that turns an **APPROVED**
`signal_intake.PendingSignalApproval` into a **non-executable**
`ProposedSignalOrder` on a **demo** account. It is the next gated step after the
E0 shadow intake, and it still places **no order**.

```
Wayond Telegram ──▶ signal_intake ──▶ PendingSignalApproval ──(human approve)──▶
    execution.signal_proposals.propose_order_from_approval ──▶ ProposedSignalOrder
                                                               (candidate; NOT an ExecutionJob)
```

## Why "no order" is structural, not just policy

The MT5 worker claims work via
`ExecutionJob.objects.filter(status=PENDING)` (see `execution.views` →
`next_job`). A `ProposedSignalOrder` is a **different model** with **no PENDING
status and no worker-claim path**, so the worker never sees it. Creating a
proposal therefore cannot reach a broker — proven by tests asserting
`ExecutionJob.objects.count()` is unchanged and the claim queryset stays empty.

The bridge (`execution/signal_proposals.py`) **never**:

- creates an `ExecutionJob` (it does not import or reference the model at all),
- calls `create_open_trade_job` / `order_send` / any broker call,
- contacts MT5 or the Windows agent, uses broker credentials, or starts a
  Telegram listener.

A static AST guard (`tests_e1a_proposals.py::NoOrderStaticGuardTests`) enforces
this; the E0 ADR-009 guard continues to prove `signal_intake` / `wims` /
`intelligence` never import `execution` (the boundary is one-way: execution may
read signal_intake).

## Safety gates (all enforced before a proposal is created)

| Gate | Rule |
|------|------|
| Approved-only | approval must be `APPROVED` |
| Kill switch | `ExecutionControl.kill_switch_engaged` fails closed |
| Signal disable | `ExecutionControl.signal_proposals_enabled = False` blocks |
| Env kill switch | `GUVFX_EXECUTION_DISABLED` honoured (defence-in-depth) |
| Demo-only | `account.is_demo` required; live broker `environment` rejected |
| Symbol allowlist | `SIGNAL_ALLOWED_SYMBOLS` (`EURUSD/GBPUSD/XAUUSD`) |
| Lot cap | `≤ SIGNAL_MAX_LOT_SIZE` (0.02); default `DEMO_FIXED_LOT_SIZE` (0.01) |
| Daily cap | `< SIGNAL_MAX_TRADES_PER_DAY` (10) proposals/account+symbol/day |
| Concurrent cap | `< SIGNAL_MAX_CONCURRENT_POSITIONS` (1) live proposals |
| Duplicate | one proposal per approval (`OneToOne`, DB-enforced) |

Every outcome writes an append-only `ProposalAuditEvent`
(`PROPOSAL_CREATED` / `PROPOSAL_REJECTED`) linked back to the approval, extending
the `signal_intake.SignalAuditEvent` chain. Rejection audits persist even though
no proposal row is created.

## Functional kill switch (replaces the MVP 501 stub)

`POST /api/execution/kill-all/` (admin-only) now **engages**
`ExecutionControl.kill_switch_engaged` and returns `200`, blocking all
proposals. Releasing the switch is intentionally **not** exposed over the API —
it is admin/server-side only (`ExecutionControl` admin actions) so the web
surface can only fail safe.

## Models

- `ExecutionControl` — singleton control row (kill switch + signal disable).
- `ProposedSignalOrder` — non-executable candidate (`PROPOSED`/`REJECTED`/`SUPERSEDED`).
- `ProposalAuditEvent` — append-only proposal/kill-switch audit.

## Operator entry point (no listener, no automation)

```bash
cd backend
python manage.py propose_signal_order --approval <id> --account <demo_id> [--lot 0.01]
```

Proposals can also be reviewed in Django admin. There is **no** automatic
approval→proposal trigger and **no** web endpoint that creates proposals; the
admin `ProposedSignalOrder` add form is disabled so the safety-gated bridge is
the only writer.

## Test / verify

The execution app's tests require PostgreSQL (the trading apps carry
Postgres-only migrations). With a local DB available:

```bash
cd backend
python manage.py test execution.tests_e1a_proposals signal_intake
```

## Next (gated, NOT in this packet)

E2+: promote a reviewed `ProposedSignalOrder` to an executable job on a demo
account with the worker still suppressed (logs intended order, places none),
then demo paper, then live — each a separate, sponsor-gated escalation.
