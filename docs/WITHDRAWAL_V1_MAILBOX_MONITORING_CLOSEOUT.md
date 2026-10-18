# Withdrawal V1 — Mailbox Monitoring Closeout (§11)

**Status:** ENGINEERING_COMPLETE for the read-model + alert-capable CLI (DARK / no scheduler wired).
Forward-looking signals are DESIGNED and PD-gated. **No new production timer is introduced by this packet.**
**Date:** 2026-10-18 · **Packet:** GUVFX_WITHDRAWAL_V1_WEEKEND_COMPLETION_R2

This closeout records how a failing or silent mailbox is made *identifiable* without decrypting a credential or
calling Gmail, what is shipped versus designed, and the exact gate each forward item needs. It distinguishes
architectural **intent**, current **implementation**, and **PD-gated** proposals (security RULE 5).

---

## 1. What exists today (shipped, read-only, secret-free)

| Capability | Where | Notes |
|---|---|---|
| Per-mailbox health classification | `broker_intelligence/health.py` → `mailbox_health()` | SUCCESSFUL / STALE / FAILED / RECOVERY_REQUIRED from stored metadata only; masked email; never a token/cursor/body. |
| Service aggregate | `health.py` → `service_health()` | `any_degraded`, `any_recovery_required`, and an explicit `service_running_note` that a clean unit exit does **not** imply mailbox health. |
| Customer-safe status | `health.py` → `customer_mailbox_status()` | Member-facing conceptual state; RECONNECT_REQUIRED only on explicit OAuth evidence (§4). |
| Operator CLI (alert-capable) | `management/commands/broker_mailbox_health.py` | `--json`, `--all-statuses`, `--exit-nonzero-on-recovery`, `--exit-nonzero-on-degraded`. Report **always prints first**; a flagged condition raises a non-zero exit **after** printing. Default invocation exits 0 (behaviour preserved). |
| Unmigrated-safe read | `health.py` → `r1_columns_present()` + `.only(...)` | At production schema 0010 (migration-0011 health columns absent) the read-model selects only pre-existing columns and classifies from staleness alone — so the diagnostic works in the exact incident state it reveals. |

### Alert conditions the shipped surface already covers

- **Staleness** — `last_successful_sync` older than `BROKER_INTELLIGENCE_STALE_SECONDS` (default 15 min) → STALE;
  older than `..._RECOVERY_SECONDS` (default 60 min) → RECOVERY_REQUIRED. Works on schema 0010.
- **Repeated failure** — `consecutive_failures` (schema 0011) surfaced in the health dict.
- **OAuth failure** — a blocking re-auth code (`OAUTH_PERMISSION_DENIED` / `GMAIL_HTTP_401` / `GMAIL_HTTP_403`,
  recovery `SPONSOR_REAUTH`) → RECOVERY_REQUIRED (operator) / RECONNECT_REQUIRED (member).
- **Ingestion-running-but-all-failing** — this is the exact 2026-10-14 blind spot: the systemd unit exited 0 every
  pass while the only mailbox hard-failed. `service_health` makes the per-mailbox state authoritative over the unit
  exit, and `--exit-nonzero-on-degraded` turns "unit green, mailbox dead" into a non-zero signal.

### Properties (owner-safe / operator-safe)

- **Secret-free:** every field is a masked email, a stable diagnostic code, a timestamp, or a count. No token,
  credential ref, raw cursor, email body, address, URL or Google error body is ever emitted (R1 §4 contract).
- **Dedup / no alert storm:** classification is a pure function of current stored state, not an event stream — one
  run yields one verdict per mailbox, so re-running never multiplies alerts. A scheduler that keys on the process
  exit code (0/!=0) inherits this idempotence.
- **Owner-private at the member boundary:** the customer status API is strictly `user=request.user` scoped; the
  operator CLI/`service_health` are operator tools and are never exposed on the member API surface.

---

## 2. Delivery model (no new production timer)

The alert capability is a **CLI exit code**, deliberately **not** wired to any scheduler in this packet. Wiring it to
cron / a systemd timer / an external monitor is a production-infrastructure change that must go through the
established GO gates (security RULE 1: never start a long-running production service from an interactive SSH session;
RULE 8: validate any out-of-Git launch path before each production change). Recommended wiring, when armed:

```
# Operator host, every 5 min (NOT started from an interactive SSH session — a scheduled task/systemd timer only):
python manage.py broker_mailbox_health --json --exit-nonzero-on-degraded
```

A non-zero exit is the alert; the JSON report is the evidence. No GuvFX code initiates, confirms or modifies a
withdrawal, and nothing here contacts a broker or decrypts a credential.

---

## 3. Forward-looking signals — DESIGNED, PD-gated, NOT applied

Detailed in `docs/WITHDRAWAL_V1_OAUTH_SUSTAINABILITY_RECOMMENDATION.md`. Each is additive and reversible; none is
applied here.

1. **Refresh-credential-age proximity signal.** Warn *before* a refresh token expires (the inferred External/Testing-
   mode 7-day expiry — one observed expiry, 2026-10-09 consent → ~2026-10-16 failure). Needs a nullable
   `credential_obtained_at` column (**migration 0012** — additive,
   reversible, unmigrated-safe read). **Gate:** PD approval to add migration 0012 + the standard DARK→arm sequence.
   **Not applied** (no migration in this packet).
2. **`ConnectedMailbox.Status = ERROR` transition on a blocking re-auth.** Lets a dead mailbox leave the
   CONNECTED-only poll set and drive the member RECONNECT_REQUIRED state deterministically. **Amber:** it changes the
   worker's poll-set semantics. **Gate:** explicit decision (ADR/Notion) before the worker writes ERROR.
   (The read-model already *maps* `status == ERROR` → RECONNECT_REQUIRED, so arming the writer needs no read change.)

---

## 4. Explicit limitations / not covered

- No sustained alert *delivery* is deployed (no cron/timer/pager wiring). The CLI is alert-*capable* only.
- Repeated-failure and last-error diagnostics require migration 0011; on production schema 0010 only
  staleness-based classification is available (by design — it still identifies a dead mailbox).
- The proximity signal cannot exist until migration 0012 is approved and applied.
- This is observation only. It never re-authenticates, never anchors a cursor, never contacts a broker.

**Marker:** `MAILBOX_MONITORING_ENGINEERING_COMPLETE` = **PASS (read-model + alert-capable CLI, DARK)**;
scheduler wiring + proximity signal + Status.ERROR writer = **PD-gated, not applied**.
