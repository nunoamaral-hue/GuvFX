# WV1 / ADR-0054 F-8 — Sponsor nomination + decision form (2026-10-23)

**Purpose.** Resolve the three open human-governance roles so the bounded audit-control canary
(`WV1_AUDIT_CANARY_RUNBOOK_2026-10-23.md`) can be scheduled. **This form collects names, affiliations, a window and
an emergency route only** — never passwords, credentials, identity documents, home addresses, or other personal
data. Nothing in this packet installs or enables anything; it is DARK and advisory until the Sponsor signs.

## 1. Roles (kept distinct)
| Role | Responsibility | Status |
|---|---|---|
| **Sponsor** | Authorises the canary / production enablement | **Nuno Rodolfo Ferreira Do Amaral, Virtus Logic UK Ltd** |
| **Manifest Reviewer 1** | Independent review + digest reproduction of the production object manifest | **Nuno Rodolfo Ferreira Do Amaral** |
| **Manifest Reviewer 2** | Second independent manifest reviewer (separate human) | **UNRESOLVED** |
| **Production Operator** | Named human who executes the approved procedure on the host | **UNRESOLVED** |
| **Observer / rollback authority** | Named human who monitors the stop conditions and is empowered to order rollback; **must be distinct from the Operator** | **UNRESOLVED** |

## 2. Role-separation recommendation (Reviewer 2 as Observer?)
**Question posed:** may Manifest Reviewer 2 also act as the Observer / rollback authority?

**Governance trade-off — stated, not assumed:**
- The **non-negotiable** separation is **Operator != Observer**: the human who executes must not also be the human
  empowered to halt and order rollback. Collapsing those two would remove the only independent live check on the
  executor and is **never acceptable**.
- Reviewer 2 and Observer are both *independent-scrutiny* (non-executor) roles, at different times (static
  pre-cutover manifest review vs. live canary oversight). Letting **one** person hold both is **permissible**
  against the hard rule (that person is still not the Operator), but it **concentrates independent oversight in a
  single human**: a blind spot, unavailability, or compromise of that one person removes *both* the static and the
  live independent checks together — reduced defence-in-depth.
- The **Sponsor should not be the Operator** (authorisation must stay independent of execution). The Sponsor
  serving as Observer is weaker than an independent Observer but does preserve Operator != Observer; prefer an
  Observer who is neither the Sponsor nor the Operator.

**Recommended minimum defensible arrangement:** **three distinct humans** —
1. **Nuno** (Sponsor + Manifest Reviewer 1), who does **not** execute and does **not** act as Observer;
2. a **Production Operator** (distinct human);
3. a **second independent human** who serves as **Manifest Reviewer 2 and Observer / rollback authority**.

This keeps the hard rule (Operator != Observer) and keeps all independent scrutiny off the executor, at the cost
of combining the static and live independent checks in one person.

**Preferred (stronger) arrangement:** **four distinct humans** — Sponsor (Nuno), Manifest Reviewer 2, Operator,
and a separate Observer — so live oversight is independent of the static manifest review as well.

The Sponsor explicitly accepts or rejects any role overlap in section 4.

## 3. Nominations (Sponsor to complete)
| Field | Entry |
|---|---|
| Manifest Reviewer 2 — full name | __________________________ |
| Manifest Reviewer 2 — affiliation | __________________________ |
| Production Operator — full name | __________________________ |
| Production Operator — affiliation | __________________________ |
| Observer / rollback authority — full name | __________________________ |
| Observer / rollback authority — affiliation | __________________________ |

## 4. Confirmations (Sponsor to tick / complete)
- [ ] The **Operator and the Observer are different people**.
- [ ] Each nominated person has **reviewed their responsibilities** in `WV1_AUDIT_CANARY_RUNBOOK_2026-10-23.md`.
- [ ] Allowed role overlap decision (choose one):
  - [ ] **No overlap** — Reviewer 2 and Observer are different people (preferred, four-human arrangement).
  - [ ] **Reviewer 2 also acts as Observer** (accepted minimum three-human arrangement; overlap explicitly accepted).
  - [ ] Other overlap, specified here: __________________________ (Operator != Observer must still hold).
- [ ] **Proposed maintenance window** (date + start/end, timezone): __________________________
- [ ] **Emergency contact route** (how the Observer reaches the Operator/Sponsor to halt — channel only, not
      personal data): __________________________

## 5. Decision
- [ ] **ACCEPT** — nominations above stand; proceed to schedule the canary under the named roles (still subject to
      the separate GOs in the runbook).
- [ ] **REJECT / REVISE** — reason: __________________________

**Signature (Sponsor):** __________________________  **Date:** __________

*No part of the canary is authorised by this form alone; the Layer-A install GO and the logging-canary GO remain
separate explicit approvals (see runbook §Approvals).*
