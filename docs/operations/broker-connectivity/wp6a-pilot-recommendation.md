# WP6A — Internal Pilot Recommendation (Workstream J)

## Recommendation: **GO WITH CONDITIONS**

For a **tightly-controlled Internal Pilot** — flags OFF/manual, demo accounts only, manual operator
oversight — the **engineered capability is certified correct** by the WP6A evidence (433 executed tests all
passing; full `make check` green; see [wp6a-certification.md](wp6a-certification.md)). It is **NOT a GO**
without conditions because several shared-environment facts are **HOST-VERIFIED / OUTSIDE REPOSITORY
CONTROL** and one is a known unresolved gap. **This does NOT authorise Trusted Beta, production arming, or
customer invitations** — those remain Sponsor-gated and are separately blocked by WP6B.

The decision is based **only on evidence collected during WP6A** (executed tests + code/governance
inspection). No stress, isolation, concurrency, load, or failure evidence was collected — that is WP6B.

## Conditions (must all hold before any Internal Pilot activity)

1. **Broker-login host certification (primary condition).** A *live* demo `VALIDATE_LOGIN` has **not** yet
   succeeded on the host: ADR-0027 records Phase 2 (build-5833) as not host-certified, and the first live
   demo attempt failed at `isolation_check_failed` (the validation-terminal ACL did not grant the agent
   service account read+execute). Before any broker-login-dependent pilot step: close that ACL gap and prove
   **one successful demo `VALIDATE_LOGIN`** on the host, captured as evidence. Until then, only the
   broker-login-**independent** surfaces (below) are pilot-eligible.
2. **Operator confirmation of the HOST-VERIFIED environment items** (WP6A WS-A/H): build-5833 is the active
   validation image; `verify_image` PASS on host; the promoted golden matches its pin; the deployed commit
   is `b3e0bba`.
3. **A verified database backup exists before the pilot** — `OPERATIONS_DASHBOARD.md` §6 records no
   automated backup is deployed; take and verify one (rehearsed restore in a disposable DB).
4. **All INTERNAL PILOT LIMITS** below are honoured.
5. **Customer Zero excluded** from all pilot activity except already-approved read-only validation; no live
   accounts.

## Internal Pilot limits (recommended; no numbers invented beyond these)

- **5–10 internal users maximum.**
- **Demo accounts only** (no live-money accounts).
- **Manual operator oversight; manual support; manual rollback.**
- **No automatic arming; no unattended validation.**
- **Execution gate MAY remain OFF** until the Sponsor chooses otherwise (an Internal Pilot does not require
  execution).
- Each flag that a pilot needs is armed only under an explicit, separate Sponsor authorisation and via the
  WP5.4 [arming-runbook.md](arming-runbook.md), stage-by-stage, never "enable all".

## What does NOT block an Internal Pilot

- The **engineered capability** — lifecycle, execution-safety gates, health engine, operational events, the
  read-only Operations UI, and the 17 support workflows — is code-certified (433 tests green).
- The **broker-login-independent surfaces**: the internal Operations & Support UI (operator-only, read-only)
  and operational-event observability, which need no live broker login.
- The **execution gate staying OFF** — an Internal Pilot need not open exposure.

## What blocks Trusted Beta (not just the pilot)

- **WP6B in its entirety** (see below): multi-tenant isolation under stress, concurrency, load, failure
  injection, agent/bridge/worker failures, database recovery, throughput, capacity baselines.
- **Host broker-login certification** (condition 1) for any customer-facing validation at scale.
- **Sponsor arming approval** with a named cohort + capacity limits + stop conditions.

## Remaining risks

- **Broker-login not proven on the host** (condition 1) — the single largest open item.
- **No automated DB backup** in the shared estate (condition 3).
- **Single-VPS SPOFs + 2/11 container healthchecks + no alert-delivery sink** (`OPERATIONS_DASHBOARD.md`
  risk register) — operational-maturity gaps that bound pilot size to manual oversight.
- **HOST-VERIFIED items unconfirmed** from the repository (condition 2).

## Remaining engineering

- Close the validation-terminal ACL gap + prove a demo `VALIDATE_LOGIN` on the host (ADR-0027 Phase 2B).
- Add the **event-lag** and **operator-API-error-rate** monitoring signals (do not exist yet).
- WP6B stress/isolation/failure/capacity certification (a disposable environment first).

## Remaining operational work

- Deploy + verify an automated DB backup; rehearse restore.
- Stand up a disposable certification environment (demo accounts + disposable agent/bridge/host) for WP6B.
- Assign named support + incident + rollback owners for the pilot window (WP5.4 `OPS-2`).

---

## Deferred WP6B scope (explicitly NOT claimed complete)

The following are **outstanding** and **must not be claimed complete** by WP6A:

- multi-user isolation
- concurrency
- load
- capacity (baselines — `CAPACITY BASELINES DEFERRED TO WP6B`)
- failure injection
- agent failures
- bridge failures
- worker failures
- database recovery
- stress
- throughput

WP6B requires a **disposable certification environment** (never the shared production estate) and is
**Sponsor-gated**. Until WP6B PASSes, **no Trusted Beta invitation and no production arming** may proceed.
