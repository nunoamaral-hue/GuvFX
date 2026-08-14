# Hosted-workspace observation/provisioning scheduler

The missing deployable wire for the hosted-workspace autonomous cycle.

## What it schedules

`python manage.py run_hosted_observations` — one ordered pass, every minute:

1. **Allocate** a `TerminalNode` to any workspace still at `PROVISIONING` → `WAITING_FOR_LOGIN`
   (`hosted_workspace.provisioning_runner`).
2. **Observe** — poll hosted observations through the certified single writer, advancing the
   canonical state machine `WAITING_FOR_LOGIN → CONNECTED → matched → EXECUTION_READY`
   (`hosted_workspace.observation_runner`).
3. **Auto-arm** any `EXECUTION_READY`-but-unarmed workspace (ADR-0044 Decision 2,
   `hosted_workspace.auto_arm_runner`).

## Why it exists

The command shipped with the Beta Readiness / ADR-0044 work but had **no deployable scheduler
artefact** — mirroring `deploy/monitor-scheduler` and `deploy/soak-report`, this directory
supplies it. Without it, a self-requested Hosted Workspace stays at `PROVISIONING` forever and
an engineer has to run the command by hand. That per-user engineer intervention is a **Beta
Blocker**: a brand-new beta user's journey cannot advance autonomously. This cron is the wire.

## Safety — DARK by default

Installing the cron changes **nothing** until an operator flips `HOSTED_OBSERVATION_SCHEDULER_ENABLED`:

- `run_hosted_observations` is a **dormant no-op** unless that flag is on (it self-gates at
  `handle()`), and even then each driver only does work while `HOSTED_PERSISTENT_MT5_ENABLED` is on.
- It **never** launches MT5, logs into a broker, places an order, or arms execution by itself.
- It is a **singleton** (Postgres advisory lock — a concurrent cycle simply skips) and every step
  is **idempotent**, so per-minute re-runs are harmless.

## Usage

```bash
deploy/hosted-observation-scheduler/install_hosted_observation_cron.sh            # install (idempotent)
deploy/hosted-observation-scheduler/install_hosted_observation_cron.sh --remove   # uninstall
COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx \
  deploy/hosted-observation-scheduler/install_hosted_observation_cron.sh          # overridable
```

Log: `/var/log/guvfx/hosted_observations.log` (writable-log + logrotate provisioned idempotently,
identical to the monitor-chain installer, so a host rebuild cannot silently re-break the redirect).
