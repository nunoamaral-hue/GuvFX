# WP6-B — Isolation Certification

Prove that one tenant cannot see, affect, or leak into another across every plane. Matrix cases:
`ISO-1..8`. Safety-critical gate: `GATE-B` (zero cross-tenant access tolerated). Each case is exercised
across the dimensions **single user · multiple users · multiple accounts per user · cross-account attempts**.

> Isolation is a **safety-critical** area. Any confirmed cross-tenant read/effect is a **NO-GO** and a SEV-1
> ([incident-response.md](incident-response.md)).

## Isolation planes

| Case | Plane | What is proven | PASS criteria | Repo evidence |
|------|-------|----------------|---------------|---------------|
| ISO-1 | Account | Operational-event API + account views are owner-scoped (IDOR-safe): non-owner → 404, owner sees only own | Zero cross-owner reads; 404 (not 403) for non-owner | `operational_events/tests_operational_events.py`, `trading/tests_broker_connectivity.py` |
| ISO-2 | Runtime | Each disposable runtime has a distinct low-priv slot identity, directory, least-priv task ACL | Disjoint identities/dirs; R+X-only agent ACL; zero triggers | `terminal_provisioning/tests_win_slot_ops.py`, `tests_beta_activation.py` |
| ISO-3 | Validation | The dedicated validation terminal is disjoint from every slot/golden/accounts root; traversal + bare-drive rejected | `IsolationError` on any non-isolated path; probe fail-closed | `terminal_provisioning/tests_validate_login_agent.py`, `tests_validation_image.py` |
| ISO-4 | Event | Operator-only events never appear in a non-staff owner's **timeline OR summary aggregates** | No operator-only content in a customer view | `operational_events/tests_operational_events.py`, `tests_broker_projection.py` |
| ISO-5 | Execution | One account's ineligibility does not affect another's gate decision | Per-account gate decisions independent | `execution/tests_broker_gate.py`, `tests_dispatch_gate.py` |
| ISO-6 | Health | Per-account `BrokerAccountHealth`; one account's state/version does not influence another | Independent state + `state_version` per account | `reliability/tests_broker_health.py` |
| ISO-7 | Credential | Per-account Fernet; decrypt only at point of use with a `CREDENTIAL_ACCESSED` audit; never cross-read | One audit per decrypt; no credential in logs/responses | `trading/tests_credential_audit.py`, `terminal_provisioning/tests_cred_access_audit.py` |
| ISO-8 | Operator | Staff see all, non-staff see own; the operator UI is admin-gated + read-only | Non-operator cannot read the operator surface; staff bypass owner-scope only | `operational_events/tests_operational_events.py`, `reliability/tests_alert_scoping.py` |

## Method (disposable environment)

Provision ≥2 disposable users, each with ≥1 demo account, plus a user with ≥2 accounts. For each plane, run
the tenant's own operations and then attempt the neighbour's `account_id` / slot / event / credential /
gate / health. Record the owner-scoped response and the denied cross-tenant attempt.

## Evidence + PASS

Per-plane transcripts across all four dimensions; the summary must be visibility-scoped (ISO-4 covers the
subtle case where the *timeline* hides an operator-only event but the *summary aggregate* could leak it —
this was a real WP5.1 finding, fixed). **PASS = zero cross-tenant access on any plane, in any dimension.**
