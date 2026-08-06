# Validation-Agent Monitoring Runner — Deployment Package

**Status:** repository-ready, **NOT deployed**. This packet is engineering only. Deployment, flag-arming, and
selecting a live Telegram/email destination are SEPARATE, Sponsor-gated steps. Nothing here touches the
Windows host, the `GuvFXBetaAgent` service, port `8788`, live MT5, customer account #12, or live account #1.

This package completes the missing backend operations layer that the earlier "Operational Deployment" and
"Option A" packets STOPPED on: the merged monitoring capabilities (`agent_health_probe`, `agent_monitoring`,
`agent_alert_sink`) were **inert** — no runner, no scheduler, no external alert delivery. This packet adds
exactly those three, DARK by default.

---

## 1. What ships (all repository-only)

| Area | Artefact | Default posture |
|------|----------|-----------------|
| Durable state (WS-C) | `terminal_provisioning.AgentMonitorState` (migration `0011`) | singleton row, empty |
| Runner (WS-B/F) | `agent_monitor_runner.run_once` + policy | pure; inert unless enabled |
| Probe command (WS-B) | `manage.py run_agent_readiness_probe` | disabled → exit 0, no-op |
| Synthetic test (WS-G) | `manage.py test_agent_alert_delivery` | null sink → sends nothing |
| Ops evidence (WS-I) | `manage.py agent_monitor_status` | read-only, no secrets |
| External sinks (WS-E/H) | `TelegramAlertSink`, `EmailAlertSink` + factory | not built unless configured |
| Scheduler (WS-D) | `deploy/validation-agent-monitor/` cron installer | not installed |
| Config (WS-J) | `settings.py` block + `monitoring-runner-contract.json` | all OFF/NULL |

Repository default is fully DARK: `VALIDATION_AGENT_MONITORING_ENABLED=false`, `AGENT_ALERT_SINK=null`, no
Telegram/email destination configured. **No secret is committed.**

## 2. Safety invariants (proven by tests)

- The runner's only outbound action is the **signed-NEGOTIATE readiness probe** and, on a fired alert, a
  message to the configured sink. It performs **no** broker validation, reads **no** broker credential,
  creates **no** validation attempt, starts **no** MT5, contacts **no** broker, and reads/writes **no**
  customer account, trade, or plan. The only row it writes is the `AgentMonitorState` singleton.
- Hysteresis + per-alert cooldown are **durable** — a backend restart cannot re-page a still-open outage or
  reset a recovery streak.
- The ops Telegram sink can **never** hit the customer channel (the factory refuses when the ops `chat_id`
  equals the customer `TELEGRAM_CHAT_ID`) and **never** borrows the customer bot token (it requires its own).
- No sink ever logs a token, chat id, keyring, or credential; a delivery failure carries only an HTTP status
  code / API error_code / exception type.
- A real delivery failure is **surfaced** (exit 40, `last_delivery=failed`, ops evidence) — RR-11: an alert
  that pages nobody is the outage.
- The runner is not on any request path; disabled monitoring is inert (exit 0).

## 3. Arming order (deploy-time, Sponsor-gated — NOT part of this packet)

1. Deploy the merged backend (migrate `0011`, recreate `guvfx-backend` only, `--no-deps`).
2. Configure the DEDICATED ops destination on `guvfx-backend` env (its OWN bot token; an ops `chat_id`
   distinct from the customer channel):
   `AGENT_ALERT_SINK=telegram`, `AGENT_ALERT_OWNER=<named rota>`,
   `VALIDATION_AGENT_TELEGRAM_CHAT_ID=<ops chat>`, `VALIDATION_AGENT_TELEGRAM_BOT_TOKEN=<ops token>`.
3. **Pre-arm gate:** `docker compose exec -T guvfx-backend python manage.py test_agent_alert_delivery
   --correlation-id arming-<date>`. Confirm the synthetic alert arrives in the ops channel. This is the gate
   the Aug-5 outage lacked. (Doing this against a live Telegram destination is itself a Sponsor-gated action
   — it is NOT performed in this repository packet.)
4. Install the scheduler: `deploy/validation-agent-monitor/install_agent_monitor_cron.sh`.
5. Arm monitoring: set `VALIDATION_AGENT_MONITORING_ENABLED=true` and recreate `guvfx-backend`.
6. Verify: `deploy/validation-agent-monitor/verify_agent_monitor.sh` and `agent_monitor_status`.

Optional continuous-monitoring extra: `VALIDATION_AGENT_STALE_DETECTION_ENABLED=true` pages when a scheduled
run is missed. Leave OFF unless the cron runs continuously (a deliberately-paused scheduler would otherwise
page). **Note:** this in-runner stale check only catches a scheduler that stopped and *resumed* — it runs
inside a probe pass, so a fully-dead cron never triggers it. Detecting a completely dead prober is an
external watchdog's job: alert when `agent_monitor_status` → `last_probe_age_seconds` exceeds N intervals
(that value is the durable heartbeat). Wiring that external watchdog is a follow-up, out of this packet's
scope.

## 4. Rollback (fully reversible, non-destructive)

- Disarm delivery: `AGENT_ALERT_SINK=null` (recreate backend) — monitoring still evaluates, sends nothing.
- Disarm monitoring: `VALIDATION_AGENT_MONITORING_ENABLED=false` (recreate backend) — runs become inert.
- Remove the scheduler: `install_agent_monitor_cron.sh --remove`.
- The `AgentMonitorState` row is operational metadata only; it is safe to leave, and safe to truncate.
- No destructive DB step; migration `0011` reverses cleanly (`migrate terminal_provisioning 0010`).

## 5. STOP boundary

This packet **STOPS at the reviewed PR**. It does not deploy the backend, modify the Windows host, run the
supervised installer, start/stop/restart `GuvFXBetaAgent`, activate a production Telegram destination, send a
real Telegram alert, recreate any production container, modify production environment variables, run
`VALIDATE_LOGIN`, use broker credentials, create a broker-validation attempt, or touch account #12, account
#1, or port `8788`.
