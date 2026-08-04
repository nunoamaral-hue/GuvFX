# WP6-L — Release Recommendation

The decision matrix that turns certification evidence into a Trusted-Beta arming recommendation. **No
recommendation is made here** — the recommendation is `null` until certification **execution** completes, and
it is the **Sponsor's** decision. Machine-readable: [`wp6-release-gate.json`](wp6-release-gate.json). Gate
item: `GATE-*` (every area) + `GATE-CZ`, `GATE-REV`, `GATE-SPON`.

## Outcomes

| Outcome | Meaning |
|---------|---------|
| **GO** | Every gate item PASS with captured evidence; **zero open HIGH/MEDIUM**; Customer-Zero no-drift + production no-trade proven; rollback rehearsed; capacity baselines measured; **Sponsor arming approval** given. |
| **GO WITH CONDITIONS** | All **safety-critical** gate items PASS (isolation, execution safety, health, failure injection, recovery, rollback, CZ-no-drift), but one or more **non-safety** items are PARTIAL (e.g. a capacity baseline still refining) with an explicit, **time-bounded, Sponsor-accepted** condition and a named owner. |
| **NO GO** | Any **safety-critical** gate item FAIL/PARTIAL (isolation breach, ineligible execution permitted, cross-account leakage, rollback cannot restore safe state, health not converging), **or** any open HIGH/MEDIUM, **or** Customer-Zero/production contamination, **or** missing Sponsor approval. |

## Safety-critical areas (a FAIL/PARTIAL here forces NO GO)

**B** Isolation · **D** Execution safety · **E** Health · **H** Failure injection · **I** Recovery ·
**J** Rollback · plus **GATE-CZ** (CZ no-drift + production no-trade), **GATE-REV** (no open HIGH/MEDIUM),
**GATE-SPON** (Sponsor approval). Non-safety-critical (may be a *condition*, not a blocker, at the Sponsor's
discretion): **A** Environment, **C** Concurrency, **F** Operational events, **G** Operator workflow,
**K** Capacity.

## Decision procedure

1. Every certification case in [`wp6-test-matrix.json`](wp6-test-matrix.json) is executed and its evidence
   captured (an evidence manifest per area, per [`wp6-evidence.json`](wp6-evidence.json)).
2. Each `gate_item` in [`wp6-release-gate.json`](wp6-release-gate.json) is set PASS/PARTIAL/FAIL from that
   evidence — **PASS only when the criteria actually ran** (no inferred PASS).
3. The adversarial review of the certification results resolves every HIGH/MEDIUM (`GATE-REV`).
4. Customer-Zero no-drift + production no-trade are confirmed (`GATE-CZ`).
5. The Sponsor records the arming decision + cohort + capacity limit + stop conditions (`GATE-SPON`).
6. The recommendation is computed by the matrix above and recorded in `wp6-release-gate.json.recommendation`.

## Anti-bias guardrails

- The recommendation **must remain `null`** until every gate item has real evidence (enforced by
  `tests_wp6_certification.py`: recommendation null, no gate item PASS, WP6 execution not marked complete).
- A **NO GO on any safety-critical item cannot be overridden** by strength elsewhere — safety-critical
  failures are absolute.
- **GO WITH CONDITIONS** requires each condition to be explicit, time-bounded, owned, and **Sponsor-accepted**
  — it is not a way to pass an unmeasured or unproven item silently.
- The recommendation is the **Sponsor's** decision; certification produces the evidence and the matrix, not
  the verdict.

## Current state

`recommendation = null`. WP6 planning is complete; **WP6 execution has not run** (requires the disposable
environment + Sponsor-gated runs). No arming, no invitation, all flags OFF.
