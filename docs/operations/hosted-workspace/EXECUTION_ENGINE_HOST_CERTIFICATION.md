# Hosted Workspace Execution Engine — Host Certification Runbook (PREPARED, NOT RUN)

> **SUPERSEDED (2026-08-09) — use `EXECUTION_ENGINE_CAPSTONE.md` §4 as the authoritative runbook.** This
> document is the *pre-capstone* Provider-B version. Its "Hard gates before this runbook may run" are now
> **stale**: Decision C (isolation topology) is resolved = **shared bridge in HOSTED mode + a node-aware
> `WorkerIdentity`**, and the G2 observation runner + G5 provisioning seam are **merged DARK** (PR #315/#317).
> The current disposable-demo procedure — DARK setup via `manage.py provision_hosted_execution`, the bridge's
> own `GUVFX_WORKER_ID`/`GUVFX_WORKER_SECRET`, and Nuno's human-gated PLACE+CLOSE — lives in
> `EXECUTION_ENGINE_CAPSTONE.md` §4 (setup 4a · before 4b · order 4c · verify 4d · rollback §5 · blast radius
> §6). The safety rules below (Claude never trades even demo; credential-free; only the disposable env is
> touched; production PIDs/ports/service untouched) remain in force verbatim. Retained here as the historical
> pre-capstone artefact (RULE 5).

- Status: **PREPARED — NOT AUTHORISED TO RUN.** Repository engineering for the DARK Provider-B Execution
  Engine is complete (ADR-0034 G1/G2/G3/G4/G5/G6/G9/G10 + Decisions C/D). This runbook is written for a
  *future* certification and must not be executed until a disposable DEMO workspace + Nuno's manual broker
  login are available. Decisions B/C/D are resolved (demo-only / owner-bound single route / explicit layered
  arming). The harness now also proves: explicit arm (G5 `arm_hosted_workspace_execution`), bridge startup
  assertions (G6 `MT5_HOSTED_EXECUTION`), claim-seam entitlement (G4), account-switch pause + safe resume
  (G9), and duplicate/ambiguous-result reconciliation (G10 `classify_ambiguous_result`) — all before the one
  tiny demo PLACE, which is performed manually by Nuno (Claude never places/closes/modifies an order).
- Scope of the eventual certification: prove, on a **disposable DEMO** workspace only, that a Hosted Workspace
  (Provider B) account can execute safely through the *already-certified* order-safety spine, with zero
  production blast radius. **Demo only. No live money. No production account.**

## Hard gates BEFORE this runbook may run

This runbook stays inert until ALL of the following are explicitly authorised by Nuno (see
`docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §8.3):

1. **Decision B** — demo-only is confirmed for the certification (real/live accounts are a separate RED
   authorisation; this runbook is demo-only regardless).
2. **Decision C** — the isolation topology (one bridge per workspace vs shared worker + entitlement) is
   chosen, because it determines routing and which bridge/host the disposable workspace uses.
3. The remaining repository work it exercises (G2 observation runner, G5 provisioning seam) is implemented
   and merged DARK.
4. A disposable, broker-connected DEMO MT5 workspace exists under `C:\GuvFX\cert\` that **Nuno has logged
   into manually** (Claude never enters broker credentials).

## Standing safety rules (unchanged, non-negotiable)

- **Claude never places, closes, or modifies an order** — even a demo order. Every GATE below that opens or
  closes a position is performed **by Nuno manually**; Claude only prepares state and *observes* the result.
- Claude never sees, requests, types, or stores broker credentials or secrets.
- Only `C:\GuvFX\cert\` is touched. Production is never modified: not `C:\Program Files\IS6 Technologies MT5
  Terminal`, not `C:\GuvFX\beta\*`, not `C:\GuvFX\accounts`, not `C:\GuvFX\secrets`; not the production bridge
  PIDs or ports (`:8788`/`:8791`); the production `GuvFXBetaAgent` service is never restarted.
- Flags stay scoped to the isolated cert environment. No production flag is flipped.

## Environment (reuse the retained M3b-2 cert infrastructure)

- `C:\GuvFX\cert\repo` (staged byte-identical to the certified commit) + `C:\GuvFX\cert\venv`
  (credential-free), as retained from `M3B2_HOST_CERTIFIED`.
- A cert-scoped bridge started from the supported service/task mechanism (never `Start-Process`/`nohup` over
  SSH — RULE 1), pinned to the disposable workspace's `attach_path`, with the DARK flags asserted at startup:
  `MT5_GUARDED_ATTACH=1`, `MT5_REQUIRE_IDENTITY_PIN=1` (and `MT5_ALLOW_LIVE` unset). Validate the launch path
  before running (RULE 8) and parse every PowerShell artefact first (RULE 9).

## Gates (to run later, in order)

| Gate | Who | What it proves |
|---|---|---|
| 0 | Claude | Cert env staged; DARK flags asserted at bridge startup; production blast-radius baseline (PIDs/ports/service untouched) captured BEFORE. |
| 1 | Nuno | Manual broker login to the disposable DEMO workspace (HARD STOP — Claude waits for "logged in"). |
| 2 | Claude | Provider-B account row + `HostedMt5Workspace` created in the cert DB; `readiness_provider=persistent_workspace`; observation→persist run advances `canonical_state=EXECUTION_READY` + fresh `last_decision_at`. Readiness reports eligible. |
| 3 | Claude | A PLACE job built for the account carries the server-derived per-job pin (`require_identity_pin`, `expected_login`, `expected_server`, `is_demo=true`) — assert the payload, do NOT send. |
| 4 | Nuno | Manually trigger the single tiny DEMO PLACE via the sanctioned worker path. Claude observes the broker result + the bridge's pre-send `verify_execution_binding` log. |
| 5 | Nuno | Manually CLOSE the position. Claude observes `verify_mutation_identity` re-verified identity before the close. |
| 6 | Claude | **Wrong-account rejection**: point the pin at a different login and confirm the bridge REFUSES (no mutation) — the wrong-account fail-closed proof. |
| 7 | Claude | **No duplicate execution**: re-deliver the same job / retry and confirm idempotency (one order, no second). |
| 8 | Claude | Production blast radius = ZERO (PIDs `4336`/`8748`/`316`, `:8788`/`:8791`, `GuvFXBetaAgent` unchanged AFTER). Cleanup: disposable workspace + accounts.dat removed; `cert/repo`+`cert/venv` retained credential-free. |

## Return

A 25-item evidence record (mirroring the M3b-2 cert), each item PASS only when the criterion actually ran
(evidence.md), with the exact commands and raw results, plus the blast-radius before/during/after and the
list of what was NOT covered. Success marker (proposed): `EE_HOST_CERTIFIED — HOSTED_DEMO_EXECUTION_PROVEN`.
Arming real orders remains a separate RED decision (B/D) after this demo certification.
