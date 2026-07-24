# 0017 — Beta scheduled tasks are ENABLED but TRIGGERLESS (on-demand execution capabilities)

- Date: 2026-07-25
- Status: **Accepted** — Nuno, 2026-07-25 ("Adopt the following canonical Task Scheduler state: THE EIGHT
  APPROVED BETA TASKS SHALL BE ENABLED BUT TRIGGERLESS AT REST. They are on-demand execution capabilities, not
  scheduled jobs."). Implementation authorised through the governance pipeline.
- Related: [ADR 0015](0015-unprivileged-process-observation.md) and
  [ADR 0016](0016-present-attribution-architecture.md) (unprivileged observation / PRESENT attribution — the
  service that runs these tasks); the TSV task-scheduler-visibility remediation (#212/#213/#214) that made the
  service able to *discover* the tasks. This ADR resolves the follow-on blocker that discovery uncovered.

## Context — the blocker this ADR resolves

After the TSV remediation the least-privilege service `NT SERVICE\GuvFXBetaAgent` could discover and open its
eight per-slot beta tasks by exact name. The native signed lifecycle then failed one step later: the installer
had registered every task and immediately **`Disable-ScheduledTask`d** it ("install-only, no start"), while the
approved baseline (`approved_tasks.json`) pinned `enabled: true`, and **no runtime code path ever enables a
task**. A disabled task cannot be triggered:

- `win_slot_ops.run_task` refuses to `Run()` a disabled task (returns `False` → `*_trigger_rejected`);
- `occupancy.assert_task_matches_approved` rejects a disabled installed task unconditionally
  (`TaskDefinitionDrift("disabled")`), *before* any trigger.

So the entire task-trigger path — native `START` (launch task) and native `STOP` (terminate task) — was blocked.
Two resolutions were possible: (a) toggle each task Enabled immediately around every invocation, or (b) leave
the tasks enabled at rest. Option (a) requires granting the service (or the slot identity) a task **write/modify**
right merely to flip state, widening the blast radius of the one component that reaches the operator's live host.

## Decision

**The eight approved beta tasks are registered ENABLED but with ZERO triggers, and remain that way at rest.**
They are *on-demand execution capabilities*, not scheduled jobs. No temporary enable/disable is performed around
an invocation, and the service is **not** granted any task-modification right.

Enabled does **not** mean scheduled. The following seven statements are the model:

1. **Enabled ≠ scheduled.** A task's `Enabled` flag and its trigger set are independent. Enabled means "may be
   run"; scheduled means "starts itself". These tasks are the former, never the latter.
2. **Every task remains triggerless.** Zero triggers is a security invariant, checked at install and at runtime.
3. **No task can start automatically.** With zero triggers, nothing — no clock, logon, boot or event — starts
   a task. The *only* thing that ever runs one is a signed, authorised agent request that opens it by exact name
   and calls `Run()`.
4. **Only the beta service receives execute access.** The task-specific ACL grants
   `NT SERVICE\GuvFXBetaAgent` read+execute (`0x1200a9`) and nothing more (TSV, #212/#213).
5. **Slot identities receive no task-modification or task-security rights.** `guvfx_b_slot<n>` cannot alter,
   delete, re-trigger or re-permission its own task.
6. **The agent validates the complete definition before every execution.** `precheck_launch_task` /
   `precheck_terminate_task` re-inspect the task and assert it against the approved definition on every
   invocation — identity, executable, arguments (scope), logon type, run level, **enabled**, and **zero
   triggers**.
7. **A mismatch remains fail-closed.** Any drift — disabled, a trigger, a wrong field — refuses the operation.
   The agent never repairs, re-registers or re-enables a task at runtime.

**Rationale for enabled-at-rest over toggle-around-invocation:** keeping the tasks enabled avoids granting the
service (or the slot identity) any task write/modify right merely to flip `Enabled` before each call. The
security boundary is held instead by *zero triggers* — a task that cannot start itself is not made more
dangerous by being enabled, because a signed agent request is still the only caller.

## Security boundary (unchanged by this ADR except where stated)

The full boundary that keeps the terminate task's `Stop-Process -Force` off the operator's live terminal, and
keeps a beta task from becoming a scheduled job, is:

- zero triggers (this ADR — install-time and runtime invariant);
- exact approved definition (digest over identity fields + enabled + triggerless);
- fixed low-privilege task principal (`guvfx_b_slot<n>`, RunLevel Limited);
- task-specific ACL (service read+execute only; no folder ACE — TSV);
- signed agent request (HMAC per `mgmt_protocol`);
- exact slot/runtime binding (`(slot, generation)` occupancy);
- service read+execute only — **no** task registration, modification, deletion or security-descriptor control;
- the slot identity holds none of those rights either.

## What changes

- **Installer (`install_pool.ps1`).** The two `Disable-ScheduledTask` calls are removed
  (`New-ScheduledTaskSettingsSet` defaults `Enabled=$true`, so registration alone leaves each task enabled and
  triggerless). The `-Apply`/`-VerifyOnly` VERIFY block now asserts each task is **Enabled** (rejecting Disabled)
  and carries **zero triggers** (corroborated PS + COM), principal = matching slot identity, RunLevel Limited.
- **Credential-free delivery (`-EnableTasksOnly`).** A new standalone installer mode moves the eight
  already-registered tasks from install-only Disabled to Enabled, and exits. `Enable-ScheduledTask` needs no slot
  password and re-registers nothing; the mode refuses to enable anything that is not exactly a reviewed beta task
  (right principal, Limited run level, zero triggers) and read-back-verifies Enabled + zero triggers. It never
  creates, deletes, re-registers or edits a task, and never touches production tasks.
- **Runtime validation.** `win_slot_ops.query_task` now reads `Definition.Triggers.Count`;
  `win_primitives.inspect_task` carries `trigger_count` in its evidence and treats an unreadable count as
  incomplete (fail closed); `occupancy.assert_task_matches_approved` adds the zero-triggers invariant after the
  enabled check (`TaskDefinitionDrift("triggered")`), with `None` treated as a trigger.

## What does NOT change

Task actions, task principals, task logon type, task run level, the service ACL mask, the slot-process ACL
mechanism (ADR 0016), and every production task configuration are all untouched.

## Consequences

- The native lifecycle can complete: with the tasks enabled + triggerless, `START` and `STOP` reach and run
  their approved tasks under a signed request, and `TOMBSTONE`/`RELEASE` follow.
- The service is never granted a task write right; least privilege is preserved.
- The install-only guarantee is unchanged in substance — *nothing runs* during or after install, because a
  triggerless task cannot start and no signed request is issued.
