# ops/monitoring -- GuvFX mailbox alerting (DARK templates)

These are **repository artifacts prepared for activation, NOT installed** by any packet. They wire the existing,
read-only, secret-free health command
(`backend/broker_intelligence/management/commands/broker_mailbox_health.py`) to a scheduler so a stalled/degraded
mailbox surfaces as an alert instead of a silent stall.

| File | Purpose |
|------|---------|
| `guvfx-broker-mailbox-health.service` | systemd oneshot that runs the health check; non-zero exit on degraded/recovery = the alert signal |
| `guvfx-broker-mailbox-health.timer`   | 5-minute cadence |

**Do not enable these under the current packet.** Activation (install, `systemctl enable --now`, wire `OnFailure=`
to the estate notifier) is an operator step gated by the Programme Director — the full procedure, prerequisites and
GO/NO-GO gates are in `docs/WITHDRAWAL_V1_OPERATIONAL_READINESS_ACTIVATION.md`.

Properties: read-only (no broker contact, no credential decryption, no writes); secret-free output (masked email +
stable state only); de-duplicated by construction (reports current state, so reruns do not multiply alerts); honours
security RULE 1 (a production service is started via systemd, never an interactive SSH session).
