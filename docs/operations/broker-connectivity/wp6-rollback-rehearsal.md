# WP6-J — Rollback Rehearsal

**Rehearsal only. No production rollback.** Prove every rollback path restores a safe state, rehearsed in the
disposable environment. Matrix cases: `RBK-1..6`. Safety-critical gate: `GATE-J`. Consumes the WP5.4
[rollback-matrix.md](rollback-matrix.md) (preferred rollback = flag-OFF / DARK redeploy; no destructive DB
rollback in the DARK→armed direction).

| Case | Rollback path | Rehearsal | PASS criteria | Reference |
|------|---------------|-----------|---------------|-----------|
| RBK-1 | Flag rollback | Set each flag OFF in the disposable env; confirm DARK | each flag rollback restores DARK behaviour | `feature-flags.json`, `readiness-checklist.json` |
| RBK-2 | Image rollback (frontend DARK) | Redeploy the DARK frontend image (both `NEXT_PUBLIC_*` unset) | routes 404, no nav; parity guard green | `verify-frontend-parity.mjs` |
| RBK-3 | Backend rollback (prior image tag) | Rehearse redeploying the prior backend tag | additive migrations safe to leave; prior behaviour restored | ADR-0021 deploy/rollback plan |
| RBK-4 | Agent rollback (re-stage bundle) | Re-stage the agent bundle (fail-closed protocol) | `assert_compatible` passes with the correct bundle | `tests_mgmt_channel.py` |
| RBK-5 | Validation image rollback (5833→6073) | Revert the active validation terminal to the 6073 baseline (directory/config swap; probe side-effect-free) | 6073 baseline restorable; no estate/DB impact | `tests_validation_image.py` |
| RBK-6 | **Database restore rehearsal (disposable only)** | Take a backup + restore in a **disposable** DB; verify integrity | restore succeeds in the disposable env; **production untouched** | ADR-0021, `docs/OPERATIONS_DASHBOARD.md` §6 |

## Method + PASS

Each rollback is rehearsed against the disposable environment and its evidence captured (DARK-after-disable
transcript, parity output, `assert_compatible` result, image-swap transcript, backup checksum + restore
verification). **The database restore is rehearsed only in a disposable database — never against
production.** **PASS = every rollback path is rehearsed and demonstrably restores a safe state.** A rollback
that cannot restore a safe state is a **NO-GO** (packet: "rollback cannot restore safe state" is SEV-1).

> **Preference reminder.** Prefer disabling a flag over any destructive DB rollback wherever the merged
> design supports it (all six flags do for DARK→armed). Reversing an additive migration is a defect-cleanup
> tool, not a disarm mechanism. Do not invent unverified rollback capability.
