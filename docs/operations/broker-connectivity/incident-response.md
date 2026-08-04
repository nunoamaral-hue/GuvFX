# Incident Response — Broker Connectivity Trusted Beta (WP5.4 Workstream F)

The operational incident model for the broker-connectivity Trusted Beta. **No external notification
automation is authorised by this package** — every alert delivery sink is manual/operator-driven until
separately approved (`NOTIFICATION_DISPATCH_ENABLED` is OFF; see [monitoring-spec.md](monitoring-spec.md)).

**Ownership (single-operator programme).** Unless the Sponsor names others, the roles below resolve to:
**Decision authority = Sponsor (Nuno)**; **Recovery owner = Operator (Nuno)** with Engineering (Claude) for
repo-verifiable recovery; **Communications owner = Sponsor (Nuno)**. Record the named owners in
[readiness-checklist.json](readiness-checklist.json) `OPS-2` before arming stage 3.

**First action for any suspected incident:** identify the current partial-arming state
([rollback-matrix.md](rollback-matrix.md)) and prefer **disarming the relevant flag** over any destructive
action. Flag-off is the primary containment lever.

---

## SEV-1 — Critical (safety / integrity / exposure)

Any of:
- **execution permitted when an account is ineligible** (the gate let an order through);
- **credential or secret exposure** (any hint a secret left its boundary — logs, responses, evidence);
- **cross-account event or data leakage** (owner-scoping breach in the operational-event API or UI);
- **an order placed against the wrong account**;
- **rollback cannot restore a safe state.**

| Field | Response |
|-------|----------|
| **Detection source** | Execution-refusal audit anomaly (`EXECUTION_GATE_REFUSED`/`_DISPATCH_REFUSED` absent where expected); `CREDENTIAL_ACCESSED` audit anomaly; owner-scoping test / customer report; operational-event cross-account row; reconciliation (`operations-summary` `execution_jobs.orphaned_running`, `signal_dispositions.silent_loss_total`). |
| **Immediate containment** | 1) Disarm the relevant flag: `BROKER_CONNECTIVITY_EXECUTION_GATE` OFF (stops new exposure, transparent instantly) and/or `OPERATIONS_EVENTS_ENABLED` OFF (stops event serving) and/or `BROKER_CONNECTIVITY_ENABLED` OFF (customer surface 404). 2) If secret exposure is suspected: **stop, do not commit, do not delete** — follow `SECURITY` rule "Stop and report on suspected exposure"; rotate from `docs/SECRET_INVENTORY.md`. 3) Pause Trusted-Beta invitations. |
| **Flag actions** | `EXECUTION_GATE` OFF for ineligible-execution / wrong-account; `OPERATIONS_EVENTS_ENABLED` OFF + frontend DARK redeploy for data leakage; `BROKER_CONNECTIVITY_ENABLED` OFF for credential exposure in the customer flow. |
| **Evidence capture** | Preserve **before** any mutation: relevant `AuditEvent` rows (redacted), the order/job records, the operational-event rows, the flag state, timestamps, correlation ids. Redact per `SECURITY` — record file/path/category of any secret finding, never the secret. |
| **Communications owner** | Sponsor (Nuno). Manual only (no automated external notification authorised). |
| **Recovery owner** | Operator (Nuno) + Engineering. Reconcile any wrongly-opened exposure via the sanctioned reconciler (`reconcile_orphaned_place_orders`) — **never re-run** a job. |
| **Decision authority** | Sponsor (Nuno). Only the Sponsor may authorise re-arming after a SEV-1. |
| **Post-incident review** | **Mandatory.** Produce a PIR in the style of `docs/POST_INCIDENT_REVIEW_*` / `docs/PIR_*`; treat any coupling surfaced as an **architectural finding** (SECURITY RULE 2). |

## SEV-2 — Major (availability / correctness, no exposure/leak)

Any of:
- valid users unable to validate or execute;
- widespread **false** execution refusal for eligible accounts;
- broker-health pause or resume malfunction;
- validation-image failure;
- operational-event **visibility** breach that is customer-safe but wrong (e.g. operator-only content withheld
  from operators, or over-broad within-scope);
- repeated credential-access anomaly (elevated but not confirmed exposure).

| Field | Response |
|-------|----------|
| **Detection source** | Validation `UNAVAILABLE` rate (`BrokerAccountValidationAttempt`); refusal-rate spike (audit + `operations-summary`); `validation_baseline_dirty` / `isolation_check_failed`; health contract anomalies (`get_contract`); recorder-failure logs. |
| **Immediate containment** | Disarm the offending flag: `EXECUTION_GATE` OFF for a false-refusal spike; `HEALTH_ENABLED` OFF for pause/resume malfunction; revert to the **6073** validation baseline for an image failure; `BROKER_CONNECTIVITY_ENABLED` OFF if validation is broadly UNAVAILABLE. |
| **Flag actions** | As above; prefer the narrowest disarm that restores service. |
| **Evidence capture** | Affected account eligibility/health snapshots, refusal reason-code distribution, validation-attempt sample (masked), image `verify_image` output, recorder-failure log lines. |
| **Communications owner** | Sponsor (Nuno), manual. |
| **Recovery owner** | Operator + Engineering. |
| **Decision authority** | Sponsor for re-arming; Operator may disarm to contain without prior approval. |
| **Post-incident review** | Required if customer-visible or recurring; a lightweight note otherwise. |

## SEV-3 — Minor (isolated / cosmetic / degraded-in-place)

Any of:
- single-account validation failure;
- stale health for an account;
- delayed operational events (lag);
- operator UI unavailable;
- isolated timeline inconsistency (single dedup/missing-event).

| Field | Response |
|-------|----------|
| **Detection source** | Single-account validation-attempt row; per-account `get_contract` `STALE`; operational-event `created_at` lag vs the authoritative moment; operator report; a single dedup collision / missing projection. |
| **Immediate containment** | Usually none platform-wide. Retry the single validation; for the operator UI, redeploy the DARK/armed frontend; for a projection gap, **rebuild the projection** (cache) — authoritative state is unaffected. |
| **Flag actions** | None required in most cases. |
| **Evidence capture** | The single affected row(s) + correlation id; recorder-failure log line if a projection failed. |
| **Communications owner** | Operator (informational). |
| **Recovery owner** | Operator / Engineering. |
| **Decision authority** | Operator. |
| **Post-incident review** | Optional; batch recurring SEV-3s into a trend review. |

---

## Cross-cutting rules

- **Flag-off is the primary containment lever.** Backend flags disarm instantly (read live); frontend flags
  require a DARK redeploy. The operational-event projection is a rebuildable cache; the validation probe is
  side-effect-free.
- **Never** infer "no incident" from the absence of a caller-side error: the operational-event recorder and
  the log-metric plane are **fail-open** and **silent to callers**. Confirm health by checking
  `guvfx.operational_events` logs and durable rows, not by the absence of an exception.
- **Customer Zero** must never be used for destructive/concurrent incident testing (`OPS-5`).
- **Evidence before mutation.** Capture redacted evidence first; deleting/overwriting before capture destroys
  the record.
- **Suspected secret exposure ⇒ stop and report** (SECURITY rule) — do not commit, do not delete; rotate from
  `docs/SECRET_INVENTORY.md`; record redacted detail only.
- **No automated external notification** is authorised here. Alert *detection* signals exist (see
  [monitoring-spec.md](monitoring-spec.md)); delivery is manual until `NOTIFICATION_DISPATCH_ENABLED` and a
  confirmed sink are separately approved.

## Incident record template (mirror `docs/OPERATIONAL_EXCEPTIONS.md` OE-n shape)

```
INC-<n>
Severity: SEV-1 / SEV-2 / SEV-3
Detected: <UTC> via <source>
Partial-arming state at detection: <state id from rollback-matrix>
Containment: <flags disarmed / actions>  (UTC)
Customer impact: <...>   Trading/exposure impact: <...>
Evidence: <redacted references / paths — never secrets>
Recovery owner: <name>   Decision authority: <name>
Root cause: <...>   Architectural finding? <yes/no + reference>
PIR: <required/optional + link>   Re-arm approved by: <Sponsor + UTC>
```
