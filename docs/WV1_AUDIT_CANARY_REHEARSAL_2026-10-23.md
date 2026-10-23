# WV1 / ADR-0054 F-8 — Canary-sequence rehearsal evidence (disposable PG15, 2026-10-23)

**Scope.** Full dry-run of `WV1_AUDIT_CANARY_RUNBOOK_2026-10-23.md` on a **disposable PostgreSQL 15.19** container,
exercising the **actual committed** `ops/erasure/audit_evidence_controls.sql` (current canonical main `8dec81fd`)
and `ops/erasure/audit_evidence_controls_test.sql`. **No production contact.** Harness:
`scratchpad/audit/rehearsal_g_canary.sh` (session-local).

**Result: 21/21 rehearsal gates PASS**, with the committed test harness reporting **22/22 assertions PASS**
(A1–A8 catalog + B1–B7 negative controls + C1–C7 positive/tamper). No SQL defect found; this remained a docs-only
packet.

## Part A — control correctness (committed test harness, isolated DB)
All 22 assertions passed: audit schema/table/registry/functions/event-triggers bootstrap-owned and owner-only;
SECURITY DEFINER + fixed empty `search_path`; no EXECUTE to PUBLIC or any non-bootstrap role; no dynamic SQL;
append-only; **rename-then-drop captured** (C5), **DML-trigger drop captured** (C6), **lookalike-named object not
audited** (C7, no pollution); migration/app/erasure identities cannot drop/disable/forge/suppress (B1–B7).

## Part B — 15-stage canary sequence + scenarios
| Stage / scenario | Result |
|---|---|
| 1 Recovery point + checksum | PASS — artifact present, SHA-256 recomputed and matched |
| 2 Disk capacity + off-host dest | PASS — 15 GB >= 12 GB precondition; destination present |
| 3 Read-only identity/grant/schema/start-time capture | PASS |
| 4 No unapproved app writer sessions | PASS — 0 active |
| 5 Layer-A atomic install (committed SQL) | PASS — rc=0 |
| 6 Catalog verify (ownership/ACL/search_path/OID registry/event-triggers) | PASS — evt=2, OID-registry=6, secdef+empty-search_path funcs=4 |
| 7 Runtime/migration/erasure cannot tamper | PASS — migrate denied DROP EVENT TRIGGER; app denied audit read |
| 8–11 **Normal canary** (reload enable, synthetic ops, deadline reset) | PASS — enabled by reload; **no restart** (postmaster start-time unchanged); reset to `none` |
| — credential/verifier suppression in-window | PASS — 0 leaks |
| — expected synthetic events observed | PASS |
| **Scenario 2 disk-floor abort** | PASS — free < 10 GB floor -> abort + auto-reset to `none` |
| **Scenario 3 log-ceiling abort** | PASS — retained log > ceiling -> abort + auto-reset to `none` |
| **Scenario 4 Operator-failure -> Observer rollback** | PASS — Operator unavailable -> Observer rollback: logging off, controls removed, **evidence intact (100 rows preserved)** |
| 12 Synthetic canary objects removed | PASS — `canary` schema absent |
| 13 Off-host export of audit evidence | PASS — exported (header present; a clean canary destroys nothing so 0 destructive rows is the correct result) |
| 13b **Off-host export failure** | PASS — export to a bad path leaves the DB healthy |
| 14 Final verify (log=none, health, no credential leak) | PASS |
| 15 Accept/rollback decision gate | PASS — requires named Observer authority (not auto-decided in rehearsal) |
| cleanup / leak check | PASS — no disposable containers remain |

## Key proven properties (mapped to runbook stops)
- **Reload-only, no restart** — `pg_postmaster_start_time()` unchanged across enable+reset (stop #11).
- **Auto-reset always runs** — a watchdog `trap…RETURN` resets `log_statement='none'` even when a prior step fails
  (stops #12/#13/#16/#17).
- **Thresholds abort** — disk-floor and log-ceiling breaches both abort and reset (stops #3/#12/#13).
- **Operator-failure -> Observer rollback** — independent halt authority removes the controls and leaves real
  evidence intact (stop #19); this is why **Operator != Observer** is mandatory.
- **Export failure is non-fatal to the DB** (stop #2, post-change branch).
- **No destructive operation on real evidence** occurred in any scenario; evidence row count preserved throughout.
- **No leaked disposable resources.**

## Fidelity notes / limits
- Disposable 15.19 (Debian) vs production 15.15 (Alpine/musl): the control uses only core catalog + event-trigger
  features common to both; the manifest/ownership/identity checks (stages 3/6) must still be run against the
  **real** production catalog at execution time (they cannot be rehearsed with production data here).
- Disk floor / log ceiling were simulated via thresholds, not by filling the real disk; the production watchdog
  must read true free space and retained-log size.
- The recovery-point validation (stage 1) was rehearsed against a synthetic artifact; production uses the real
  certified `pg_dump|gpg` point on the NAS.
- The accept/rollback decision (stage 15) is a **human** decision under the named Observer; it is represented, not
  auto-decided.
