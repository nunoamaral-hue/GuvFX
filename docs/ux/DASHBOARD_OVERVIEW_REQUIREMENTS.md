Version: v1.0  
Approval Date: 2026-01-20  
Approved By: GuvFX Conductor  
Status: Approved — UX Requirements Only

# GuvFX — Post-Login Dashboard / Overview
## UX Requirements

Document Type: UX Requirements (Evidence-Based)  
Status: Draft — Pending Governance Approval  
Owner: Nova — UX & UI Authority  
Source: UX Audit & Readiness Assessment  
Canonical Index: /docs/CANONICAL_INDEX.md

---

## 1. Purpose

The Post-Login Dashboard / Overview exists to provide immediate
orientation and situational awareness to traders upon authentication.

It is not a task execution surface.

---

## 2. Orientation Requirement

### D-01 — Orientation First

The Overview MUST function as an orientation surface before any task execution.

**Rationale**
- Traders expect immediate context after login.
- Task-first landing increases cognitive load and erodes trust.

**Acceptance Criteria**
- Within ~3 seconds, a user can answer:
  - Is anything running?
  - Is anything wrong?
  - What changed since last session?

---

## 3. State Aggregation

### D-02 — High-Level State Aggregation

The Overview MUST aggregate high-level state across core domains.

**Domains (summary only):**
- Strategies
- Accounts / execution context
- Performance / activity
- System health

**Acceptance Criteria**
- No navigation is required to confirm basic system state.
- Detailed views remain in their respective sections.

---

## 4. Information Architecture

### D-03 — Non-Duplicative Content

The Overview MUST NOT duplicate full task pages.

**Acceptance Criteria**
- Content summarizes and links.
- No full lists or dense datasets appear.

---

## 5. Navigation Integrity

### D-04 — Navigation Contract

If "Dashboard / Overview" exists in navigation, it MUST be stable and safe.

**Acceptance Criteria**
- The page always renders.
- No logout, error, or undefined state occurs.

---

### D-05 — Flow Progression

The Overview MUST support the flow:

Overview → Detail → Action

**Acceptance Criteria**
- Clear paths to Strategies, Performance, and Accounts exist.
- Navigation feels intentional and predictable.

---

## 6. Cognitive Load

### D-06 — Glanceability

Information MUST be glanceable, not dense.

**Acceptance Criteria**
- No paragraph-length explanations required.
- Visual emphasis reflects priority, not quantity.

---

### D-07 — Priority Signaling

The Overview MUST differentiate normal vs attention-required states.

**Acceptance Criteria**
- Attention states are rare and meaningful.
- Normal state is clearly indicated.

---

## 7. Trust & Credibility

### D-08 — Institutional Grade Presence

The Overview MUST reinforce stability and competence.

**Acceptance Criteria**
- Layout stability
- Alignment precision
- No placeholder-looking final content

---

### D-09 — Empty / First-Time States

The Overview MUST handle empty and first-time states gracefully.

**Acceptance Criteria**
- New users understand what GuvFX is and what to do next.
- No dead ends or ambiguity.

---

## 8. Explicit Non-Goals

The Overview MUST NOT:
- Execute trades
- Toggle strategies
- Configure accounts
- Replace analytics pages
- Introduce new features

---

## 9. Governance Notes

- This document defines UX requirements only.
- It does not specify layout, components, or visuals.
- Any implementation must:
  - Reference this document
  - Pass Nova UX review
  - Pass Quinn readiness verification
  - Be approved by the GuvFX Conductor