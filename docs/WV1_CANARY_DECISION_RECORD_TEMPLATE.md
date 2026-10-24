# WV1 / ADR-0054 F-8 — Canary decision / approval record (TEMPLATE)

**Use.** One record per canary attempt. Filled by the named humans at execution time. This is a **template** — it
grants nothing and records nothing until completed and signed. Pairs with
`WV1_AUDIT_CANARY_RUNBOOK_2026-10-23.md` and `WV1_SPONSOR_NOMINATION_FORM_2026-10-23.md`.

## 1. Identification
| Field | Value |
|---|---|
| Canary attempt id | __________ |
| Date / maintenance window (tz) | __________ |
| Canonical main SHA at execution | __________ |
| Audit-control SQL path + commit | `ops/erasure/audit_evidence_controls.sql` @ __________ |
| Recovery point artifact + SHA-256 | __________ |

## 2. Named roles (from the signed nomination form)
| Role | Name | Affiliation |
|---|---|---|
| Sponsor | Nuno Rodolfo Ferreira Do Amaral | Virtus Logic UK Ltd |
| Manifest Reviewer 1 | Nuno Rodolfo Ferreira Do Amaral | Virtus Logic UK Ltd |
| Manifest Reviewer 2 | __________ | __________ |
| Production Operator | __________ | __________ |
| Observer / rollback authority (!= Operator) | __________ | __________ |

Operator != Observer confirmed: [ ] yes

## 3. Approvals captured before execution
| GO | Granted by (Sponsor) | Date/time | Signature |
|---|---|---|---|
| GO#1 human roles resolved | __________ | __________ | __________ |
| GO#2 Layer-A install | __________ | __________ | __________ |
| GO#3 logging canary (reload-only) | __________ | __________ | __________ |

## 4. Preconditions at execution (tick when verified)
- [ ] Recovery point present + checksum verified
- [ ] Free space >= 12 GB
- [ ] Off-host (NAS) destination reachable
- [ ] Bounded log destination configured (rotation) — recorded: __________
- [ ] Watchdog armed (disk floor 10 GB, log ceiling __________ MB, deadline __________ min)
- [ ] Operator and Observer both present and reachable
- [ ] No unapproved active application writer sessions

## 5. Stage log (record result + evidence ref per runbook stage)
| Stage | Result (pass/stop/abort) | Evidence reference | Notes |
|---|---|---|---|
| 1 Recovery validate | | | |
| 2 Capacity + dest | | | |
| 3 Capture | | | |
| 4 Writer sessions | | | |
| 5 Layer-A install | | | |
| 6 Catalog verify | | | |
| 7 Tamper controls | | | |
| 8 Canary begin | | | |
| 9 Synthetic ops | | | |
| 10 Monitor | | | |
| 11 Auto-reset | | | |
| 12 Remove canary objs | | | |
| 13 Export off-host | | | |
| 14 Final verify | | | |

## 6. Hard-stop events (record any that fired)
| Hard-stop # | Fired? | Detail | Rollback performed |
|---|---|---|---|
| | | | |

## 7. Final state verification
| Check | Value |
|---|---|
| `log_statement` final | __________ (must be `none`) |
| Application health | __________ |
| Credential/secret hits in log | __________ (must be 0) |
| Evidence row counts unchanged | __________ |
| Layer-A disposition | [ ] retained (ACCEPT) / [ ] removed (ROLLBACK) |

## 8. Decision (Observer / rollback authority; Sponsor ratifies)
- [ ] **ACCEPT** — canary clean; Layer-A retained as the always-on control; logging reset to `none`.
- [ ] **ROLLBACK** — reason: __________________________ ; Layer-A removed (`DROP SCHEMA audit CASCADE`, event
      triggers first), after off-host archive of `audit.evidence_audit`.

Observer signature: __________  Date/time: __________
Sponsor ratification: __________  Date/time: __________

## 9. Post-canary notes / follow-ups
__________________________________________________________________

*This record does not authorise F-8 runtime-role production cutover, which remains a separate staged
authorisation. Production remains NO-GO until that path completes.*
