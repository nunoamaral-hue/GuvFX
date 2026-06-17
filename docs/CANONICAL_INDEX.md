# GuvFX — Canonical Knowledge Index

## Purpose
This document defines the single authoritative index of knowledge for the GuvFX system.

Any information, decision, process, or rule not referenced here is considered:
- Non-authoritative
- Historical only
- Invalid for decision-making

This index is binding.

---

## Canonical Baseline

### SYSTEM BASELINE SNAPSHOT (READ-ONLY)
Status: Locked

Scope:
- Production state
- Repositories and authority
- Deployment method
- Constraints and hard rules
- Active risks and unstable areas

This snapshot is the immutable reference point for all future changes.

---

## Migrated & Frozen Knowledge Records

The following source chats have been migrated into authoritative records and are now frozen.

Legacy conversations must not be referenced.

### 1. Deployment & Operations
- Source: GuvFX Deployment Runbook
- Status: Migrated and Frozen
- Authority: Deployment process, tagging discipline, rollback method

### 2. MT5 Runtime Control
- Source: MT5 Login Switch Plan
- Status: Migrated and Frozen
- Authority: MT5 login switching guidance and operational risks

### 3. Windows MT5 Automation
- Source: Winston — Windows MT5 Automation Lead
- Status: Migrated and Frozen
- Authority: Windows-based MT5 automation facts and constraints

### 4. MT5 Strategy Integration
- Source: Iris — MT5 Strategy Integration
- Status: Migrated and Frozen
- Authority: Strategy integration workflow and deployment alignment

### 5. Security Review
- Source: Sid — The Security
- Status: Migrated and Frozen
- Authority: Security review scope and advisory boundaries

### 6. Architecture
- Source: Archie — The Architect
- Status: Migrated and Frozen
- Authority: Architectural documentation and design intent (non-enforced)

### 7. Development / Frontend
- Source: Clive — The Coder
- Status: Migrated and Frozen
- Authority: Frontend development practices and build workflows

### 8. Trading Strategy Guidance
- Source: Ted — The Trader
- Status: Migrated and Frozen
- Authority: Trading concepts and strategy guidance (non-enforced)

### 9. Program Management
- Source: Peter — The PM
- Status: Migrated and Frozen
- Authority: Documentation structure and coordination patterns

---

## Governance & Enforcement

### Conductor
- Role: Governance, flow control, enforcement
- Authority: Absolute over production changes
- Scope: All future changes must pass through Conductor review

### UX Authority
- Role: Nova — UX & UI Design Lead
- Scope: Look, feel, usability, and trader-grade presentation
- Constraint: Advisory + review only (no direct implementation)

---

## Change Rule (Hard)

Any future change must:
1. Reference this index
2. Declare affected canonical areas
3. Follow Conductor-controlled change flow

Failure to do so is a process violation.

---

## Status
Canonical Index: **Active**
Knowledge Migration Phase: **Closed**
System State: **Governed**

### UX Requirements
- /docs/ux/DASHBOARD_OVERVIEW_REQUIREMENTS.md

### UX Release Gates
- /docs/ux/DASHBOARD_OVERVIEW_READINESS_CHECKLIST.md (v1.0)

- prod-2026-01-20-01 — Phase 1 Dashboard / Overview
  - Commits: 49ec647, 083c320