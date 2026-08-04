# Trusted-Beta Readiness — Pre-Arming Checklist, Entry/Exit & Capacity (WP5.4 WS C + I + J)

Machine-readable copy: [`readiness-checklist.json`](readiness-checklist.json). **Nothing here is marked
complete** — every item is `PENDING` until an authorised owner records evidence. Host items are
**HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL** and cannot be closed from the repository.

**Ownership (single-operator programme):** Engineering (Claude) owns repo-verifiable items; Operator (Nuno)
owns host items; Sponsor (Nuno) owns approvals and lifecycle status. The PM/Sponsor owns advancing any
status — do not self-certify.

---

## Part 1 — Pre-arming checklist (Workstream C)

Each item: **Owner · Evidence required · PASS/FAIL rule · Blocks WP6? · Blocks arming?** Statuses live in
`readiness-checklist.json`.

### Repository
| ID | Item | Owner | Evidence | PASS rule | WP6 | Arming |
|----|------|-------|----------|-----------|-----|--------|
| REPO-1 | `main` at expected commit; WP1A/1B/2/3/4/5.1/5.2/5.3 merged | Engineering | git log; PR list (#265,#268,#274,#275,#276,#277, WP4.x) | All merged; CI green | ✔ | ✔ |
| REPO-2 | Full `make check` green | Engineering | `make check` exit 0 | Exit 0 | ✔ | ✔ |
| REPO-3 | Migration consistency (trading/operational_events/reliability) | Engineering | `makemigrations --check --dry-run` | No changes detected | ✔ | ✔ |
| REPO-4 | All six flags default OFF in code | Engineering | `feature-flags.json` + readiness test PASS | Every default OFF | ✔ | ✔ |
| REPO-5 | Frontend parity guard green | Engineering | `npm run verify:parity` | Parity OK | ✔ | ✔ |
| REPO-6 | Agent bundle manifest integrity | Engineering | `manifest.py` digest + supported_operations drift test | No drift; ops set matches | ✔ | ✔ |
| REPO-7 | No unresolved HIGH/MEDIUM findings | Engineering | Per-packet review records | Zero open | ✔ | ✔ |

### Backend
| ID | Item | Owner | Evidence | PASS rule | WP6 | Arming |
|----|------|-------|----------|-----------|-----|--------|
| BE-1 | Timeout contract holds (175>165>120+30) | Engineering | import-time asserts | Both asserts pass | ✔ | ✔ |
| BE-2 | Backend seal-only: `BROKER_CRED_ENC_PRIVKEYS` not set on backend | Operator | `backend_has_private_keys()` false | Backend holds no private key | ✔ | ✔ |
| BE-3 | Signing keyring provisioned at arming (for live customer-flow validation) | Operator | HOST-VERIFIED | Signing channel available (else UNAVAILABLE) | — | ✔ |
| BE-4 | Owner/staff access controls on operational-event API (IDOR-safe) | Engineering | view tests; non-staff own-only → 404 | No cross-owner read | ✔ | ✔ |

### Windows host (all HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL)
| ID | Item | Owner | Evidence | PASS rule | WP6 | Arming |
|----|------|-------|----------|-----------|-----|--------|
| HOST-1 | Governed build-5833 validation image staged + verified | Operator | `verify_image` PASS; source hashes pinned | ≥100 run-in .ex5; hashes match | ✔ | ✔ |
| HOST-2 | Rollback validation image 6073 present | Operator | HOST-VERIFIED | 6073 baseline present, disjoint from 5833 | — | ✔ |
| HOST-3 | No stray/foreign validation terminal; isolation disjoint; no reparse points | Operator | isolation + forbidden-artefact scan | IsolationError-free; scan clean | ✔ | ✔ |
| HOST-4 | Eight beta tasks ENABLED-but-TRIGGERLESS, least-priv ACLs (ADR-0017) | Operator | precheck read-backs; zero triggers | Enabled, zero triggers, R+X-only | ✔ | ✔ |
| HOST-5 | Provisioner protocol compatible (NEGOTIATE full set) | Operator | `assert_compatible`; bundle re-staged before/with backend | protocol 1; PROVISIONING subset ⊆ supported | ✔ | ✔ |
| HOST-6 | Golden MATERIALISE pin matches promoted host golden | Operator | `install_pool.ps1 -ValidateGoldenOnly` PASS | Pin matches FileVersion; validate PASS | ✔ | — |

> **Handoff/diagnostics ACL note (HOST-4/HOST-3).** The validation handoff + diagnostics directories carry
> the least-privilege ACL described in ADR-0017 / the validation-runner design; the agent has read+execute
> only, no task modify/re-trigger right. Confirm ACE read-backs on the host — repo describes the contract,
> the host enforces it.

### Frontend
| ID | Item | Owner | Evidence | PASS rule | WP6 | Arming |
|----|------|-------|----------|-----------|-----|--------|
| FE-1 | DARK image: both build-time flags unset; customer+operator routes 404, no nav | Engineering | deployed env; 404 checks; flag-gate tests | Routes 404, no nav | — | ✔ |
| FE-2 | Repository is the single source of truth (ADR-0031 parity) | Engineering | `verify:parity` | Parity OK | — | ✔ |

### Data and operations
| ID | Item | Owner | Evidence | PASS rule | WP6 | Arming |
|----|------|-------|----------|-----------|-----|--------|
| OPS-1 | Disposable demo accounts available for WP6 (never CZ, never live) | Operator | HOST-VERIFIED | ≥1 demo account; demo classification confirmed | ✔ | — |
| OPS-2 | Support owner, incident contact, rollback owner assigned | Sponsor | named in support/incident docs | All three named | ✔ | ✔ |
| OPS-3 | Evidence-retention location + redaction rules defined | Engineering | `evidence-pack.md` | Location + retention + redaction recorded | ✔ | — |
| OPS-4 | Arming window approved; stop conditions agreed | Sponsor | Sponsor approval record | Explicit approval + window + stops | — | ✔ |
| OPS-5 | Customer Zero excluded from destructive/concurrent testing | Operator | CZ no-drift evidence | CZ untouched; no order vs CZ | ✔ | ✔ |
| OPS-6 | Rollback rehearsed/verified per stage (disable-flag proven) | Operator | rollback dry-run notes | Each stage's disable rollback rehearsed | ✔ | ✔ |

---

## Part 2 — WP6 and Trusted-Beta entry/exit criteria (Workstream I)

### WP6 entry criteria (all required)
- WP1–WP5 repository work merged (`REPO-1`).
- DARK deployment successful (arming stage 1 complete; production behaviour unchanged).
- Provisioner rebuilt + protocol-compatible (`HOST-5`); validation image ready (`HOST-1`).
- **All six flags OFF** (`REPO-4`).
- Disposable demo accounts available (`OPS-1`).
- Operators/owners assigned (`OPS-2`).
- Rollback rehearsed or verified (`OPS-6`).
- Runbooks approved (this package).
- No unresolved HIGH/MEDIUM findings (`REPO-7`).
- Customer Zero excluded from destructive/concurrent testing (`OPS-5`).

> **WP6 status: NOT AUTHORISED, NOT STARTED.** WP6 multi-tenant certification is a separate, Sponsor-gated
> increment. This package does not begin it, create WP6 test accounts, or run multi-tenant tests.

### Trusted-Beta entry criteria (all required)
- **WP6 PASS.**
- Multi-tenant isolation proven.
- Concurrency / race tests passed.
- Support coverage ready ([support-playbook.md](support-playbook.md); `OPS-2`).
- Monitoring available ([monitoring-spec.md](monitoring-spec.md); note: signals defined, delivery manual).
- Rollback validated ([rollback-matrix.md](rollback-matrix.md); `OPS-6`).
- Sponsor arming approval (`OPS-4`).
- Explicit beta cohort **and** capacity limit (Part 3).
- Stop conditions agreed.

### Trusted-Beta exit / expansion criteria
- **Continue:** no SEV-1; SEV-2/3 within capacity; monitoring signals within `TO BE BASELINED` bounds once
  set; support/incident load sustainable; daily review clean.
- **Pause invitations:** capacity/support/incident limit reached; a persistent SEV-2; a monitoring signal
  breaches a WP6-set critical threshold. (Existing users unaffected — see arming stage 7 rollback.)
- **Roll back:** any SEV-1; execution-refusal spike for eligible accounts; rollback cannot restore a safe
  state → disarm the relevant flag(s) ([rollback-matrix.md](rollback-matrix.md)).
- **Expand cohort:** stable operation across the full daily-review cadence for an agreed period; capacity
  headroom confirmed; no open HIGH/MEDIUM; Sponsor approval.
- **Progress toward broader beta:** sustained stability at the expanded cohort + a fresh Sponsor decision +
  updated capacity baselines from accumulated evidence.

---

## Part 3 — Capacity and cohort control (Workstream J)

**No final numbers.** Every operating limit is a decision-framework placeholder to be set from WP6 evidence
(`WP6-BASELINED`). **No uncontrolled beta invitation is permitted** — a cohort and capacity limit must be
explicit before stage 7.

| Dimension | Decision framework | Initial value |
|-----------|--------------------|---------------|
| Number of users | Bound by support + incident capacity per day; start at the smallest cohort that exercises multi-tenant isolation | `WP6-BASELINED` |
| Accounts per user | Bound by validation throughput + per-account health cadence | `WP6-BASELINED` |
| Concurrent validations | Bound by the single isolated validation terminal (validation uses a global single-flight lock) and the timeout contract | `WP6-BASELINED` |
| Validation request rate | Bound by agent/runner capacity + UNAVAILABLE backoff; never hot-loop credential faults (not retryable) | `WP6-BASELINED` |
| Health-check cadence | Framework default base 300s (`BROKER_HEALTH_BASE_INTERVAL_S`) — **not armed** (scheduler inert); health converges on customer validation, not a poller | `WP6-BASELINED` (framework default 300s) |
| Exposure-opening throughput | Bound by the execution gate + existing execution pipeline limits; only relevant after stage 6 | `WP6-BASELINED` |
| Support capacity | Per-day ticket volume the assigned support owner can handle | `WP6-BASELINED` |
| Incident capacity | Concurrent incidents the operator can run without degrading response | `WP6-BASELINED` |
| Daily review cadence | At least daily during Trusted Beta; each review checks the monitoring signals + open incidents | `WP6-BASELINED` (≥ daily) |

**Capacity stop rule.** If any capacity dimension is reached, **pause invitations** (stage 7 rollback) —
existing users are unaffected. Do not expand a dimension without WP6 evidence and Sponsor approval.
