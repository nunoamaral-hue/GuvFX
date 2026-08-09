# Rollback Runbook — GuvFX (ADR-0035)

How to roll GuvFX back safely. The **first and preferred lever is always a feature-flag disable** — every
arming flag supports instant DARK rollback with no data loss. Destructive database rollback is a last
resort and is never used in the DARK→armed direction. This runbook is repository guidance; the
`rollback_plan` command *shows* the plan, it never *executes* it.

## Step 0 — Read the current plan (read-only)

```bash
python manage.py rollback_plan          # human-readable
python manage.py rollback_plan --json    # machine-readable
```

It reads the live flag posture and prints, in safe order, exactly which flags to unset and what each
disable returns the system to. If the posture is `FULLY_DARK`, no flag rollback is required.

## Step 1 — Flag-disable rollback (preferred; instant; no data loss)

Unset the flag(s) named by `rollback_plan`, outermost capability first (execution → gate → delivery →
onboarding → polling → health → events → monitoring → master). Each is reversible and non-destructive:

| Flag | Disable effect | Returns toward |
|------|----------------|----------------|
| `HOSTED_MT5_EXECUTION_ENABLED` | hosted execution disarmed | `customer_journey_only` |
| `BROKER_CONNECTIVITY_EXECUTION_GATE` | creation-time gate transparent | `health_enabled_no_gate` |
| `HOSTED_MT5_REMOTEAPP_ENABLED` | no new delivery descriptors | `customer_journey_only` |
| `HOSTED_WORKSPACE_ONBOARDING_ENABLED` | onboarding endpoints 404 | `operator_observability_only` |
| `HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED` | attach polling stops | `operator_observability_only` |
| `BROKER_CONNECTIVITY_HEALTH_ENABLED` | WP3 engine freezes existing rows | `operator_observability_only` |
| `OPERATIONS_EVENTS_ENABLED` | event API 404s; recorders no-op | `dark_deployed` |
| `VALIDATION_AGENT_MONITORING_ENABLED` | monitor runner inert | `dark_deployed` |
| `HOSTED_PERSISTENT_MT5_ENABLED` | **master off** — entire subsystem dark | `dark_deployed` |
| `BROKER_CONNECTIVITY_ENABLED` | broker-connectivity master off | `dark_deployed` |

Flags are read live (settings-first-then-env), so a disable takes effect without a restart. Confirm with:

```bash
python manage.py operational_health          # affected subsystems return to AWAITING_SPONSOR
python manage.py hosted_workspace_preflight   # verdict returns to BLOCKED_ON_SPONSOR / dark
```

The full per-state matrix (customer impact, trading impact, new-exposure risk, safest action) lives in
`docs/operations/broker-connectivity/rollback-matrix.md` (14 partial-arming states). This runbook is the
operational front end to it.

## Step 2 — Deploy-image rollback (manual, Sponsor-approved; only if a flag disable is insufficient)

Reach for this **only** on a Golden STOP-check drift, an unexpected order/position, or a customer runtime
reaching `RUNNING` without expected state — i.e. a code/data problem a flag cannot contain.

- Image tag: `rollback-preADR0021`.
- Reverse migrations (in order): `migrate terminal_provisioning 0008`, then `migrate trading 0012`.
- Full procedure + pre-conditions: `docs/ADR-0021-DEPLOY-ROLLBACK-PLAN.md`.

## What is NOT a rollback (do not invent one)

- There is **no** destructive database rollback in the DARK→armed direction for any flag — do not delete
  rows to "undo" an enable; disable the flag instead.
- Do not stop a production service from an interactive SSH session (RULE 1) — use the supported mechanism.

## Evidence

Capture the rollback as evidence:

```bash
python manage.py collect_operational_evidence --packet-id ROLLBACK-<ref> \
    --out evidence/manifests/rollback-<ref>.json
```
