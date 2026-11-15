# Established monitoring-recovery — corrected eligibility contract + bounded positive acceptance

- **Packet:** GFX-DASHBOARD-ESTABLISHED-MONITORING-ELIGIBILITY-FIX-112 (2026-11-14).
- **Builds on:** ADR-0107 (established-account auto-recovery) + its ELIGIBILITY-FIX-112 addendum.
- **Status at merge:** repository-only; DARK (both recovery flags default-OFF); migration-free. No production/host
  mutation by this packet. The positive acceptance below is a **separate, Sponsor-gated** step after deploy.

## 1. The corrected eligibility contract (what makes an account recoverable on this path)

An established account's DOWN `terminal64` may be auto-relaunched by the **monitoring** recovery pass only when
**every** durable, account-scoped fact agrees (first failing gate names the reason code):

| # | Requirement | Reason code if it fails |
|---|---|---|
| 1 | Exists, owner-bound, **not** tombstoned. **`is_active` is NOT checked** — it is an execution control, not a monitoring one. | `no_account` / `account_disconnected` / `no_owner` |
| 2 | Not reserved / Customer Zero. | `reserved_identity` |
| 3 | A derivable account-specific slot (`derive_slot(account.id)`) — the exact relaunch target. | `slot_underivable` |
| 4 | The workspace is this account's (`ws.trading_account_id == account.id`). | `workspace_account_mismatch` |
| 3b | A **hosted, per-tenant monitoring subject** (`readiness_provider == PERSISTENT_WORKSPACE` **or** a `HostedMt5Workspace` row exists — the same disjunction the authoritative monitoring model uses, so a provider-field lag never excludes a monitored account) — not a legacy/global-agent account. | `not_monitored` |
| 4c | **Not** a Copy Trading master or follower (registries read only to exclude; indeterminate ⇒ fail closed). | `copier_participant` / `copier_check_unavailable` |
| 5 | A live (non-RETIRED) per-tenant endpoint owned by this account, agreeing with the workspace. | `no_endpoint` / `endpoint_workspace_mismatch` |
| 6 | A non-empty expected (login, server) identity pin. | `no_identity_pin` |
| 7 | **Prior verified evidence** — an immutable, identity-matched `account_read` snapshot under the **current** pin. | `no_prior_verified_read` / `identity_pin_drift` |
| 8 | A recoverable environment — DEMO always; LIVE **only** when `HOSTED_LIVE_RECOVERY_ENABLED` is on. | `live_recovery_disabled` / `environment_indeterminate` |

**Flags (both default-OFF, ANDed with the master + liveness-recovery gates):**
`HOSTED_ESTABLISHED_RECOVERY_ENABLED` turns the established candidate class on; a **LIVE** subject additionally
requires `HOSTED_LIVE_RECOVERY_ENABLED`. A DEMO subject needs only the established flag.

**Invariants this path never breaks:** it relaunches **only** that account's own terminal; it never arms, logs in,
supplies a credential, enables Copy Trading, places an order, writes/fabricates a snapshot, sets `is_active`, or
touches a sibling. Re-establishing monitoring is re-proven solely by the normal identity-firewalled collector on
the next cycle.

## 2. Bounded positive acceptance procedure (Sponsor-gated; after deploy)

Choose exactly **one** subject **S**: a real, established, monitoring-only account that is eligibility-but-for-the-flags
(reason `live_recovery_disabled` for a LIVE S, or eligible-but-for the established flag for a DEMO S), **flat**
(no open positions, no pending orders — human-confirmed in Guacamole), identity-matched, with a current
`account_read` snapshot and `liveness_recovery_count = 0`. **Never infer "flat" from missing data.**

1. **Baseline (read-only):** confirm both flags OFF; S's eligibility reason is the expected "but-for-the-flags"
   code; S flat (human visual); every other monitored account fresh; record recovery counters.
2. **Enable the minimum flags** by editing `beta.env` (byte-exact backup first; add no other variable): a **DEMO**
   S → `HOSTED_ESTABLISHED_RECOVERY_ENABLED=1` only; a **LIVE** S → that **and** `HOSTED_LIVE_RECOVERY_ENABLED=1`.
3. **Recreate only `guvfx-backend`** (`docker compose up -d --no-deps guvfx-backend`; **never** `--remove-orphans`);
   verify revision, health 200, flags-as-read, workers preserved.
4. **Human** deliberately stops **only S's** `terminal64` via the account-specific mechanism (Guacamole into S's
   slot). Never the bridge/host/Guacamole/a sibling tenant.
5. Observe the next 2–3 `run_hosted_observations` cycles (the `established:` log section).
6. **Require, exactly:** `established: … candidates=1 … down=1 relaunched=1` for **S only**; no sibling enumerated;
   S's `liveness_recovery_count` +1 exactly; one established alert opens then resolves; a **new legitimate**
   `account_read` snapshot arrives (never injected); monitoring returns to full, 0 stale.
7. Do **not** insert any manual/synthetic snapshot; an UNKNOWN/ambiguous observe is fail-closed (no relaunch).
8. **Return both flags OFF** (byte-exact `beta.env` restore; recreate backend); verify S healthy and every sibling
   unchanged.

**Abort / rollback (immediate, on any anomaly** — wrong account, an open position/pending order appears, a sibling
goes stale, >1 candidate, any cross-account host action, identity mismatch, relaunch UNKNOWN, duplicate relaunch,
cooldown failure, or any Copy Trading / execution-control change**):** restore both flags OFF and recreate backend
(stops all further recovery); if automatic recovery did not conclusively succeed, restore **only S's** terminal via
the existing human/operator path — **no late/improvised recovery, never a fabricated snapshot, never a broker
action.** Image-only backend rollback is available (no code change is exercised by the test itself).

## 3. Prohibitions (standing)

No fabricated/manual snapshots; no broker orders or execution authority; no credential handling by the agent; no
`is_active` change; no Copy Trading change; no Tailscale/firewall/DNS change; never start a long-running production
service from an interactive SSH session (use the compose daemon); logins/secrets are masked in all evidence.

## 4. Hardening-118 controls (steady-state safety; all DARK/default-OFF, strictly parsed, fail-closed)

| Setting | Default | Effect |
|---|---|---|
| `HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S` | `900` | Dedicated recovery staleness threshold (dashboard 900s untouched). Fail-closed to 900 on malformed/zero/negative/<300s. Practical floor ~600s. |
| `HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS` | *(unset)* | Fail-closed account allow-list. **Unset/empty/malformed ⇒ zero candidates.** Strict positive ints; narrows only; #46/#48 stay excluded even if listed. |
| `HOSTED_ESTABLISHED_RECOVERY_HOST_WINDOW_S` | `3600` | Host restart-storm rolling window. |
| `HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES` | `3` | Max relaunches per host per window (else circuit opens). |
| `HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN` | `2` | Max concurrently-down siblings per host (else the host cohort is blocked — a host-wide event is not auto-relaunched). |

## 5. Future bounded #50-only pilot sequence (its own Sponsor-gated packet — not authorized here)

1. Production recovery flags initially **OFF**.
2. Allow-list set to exactly `HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=50` (no sibling can become a candidate).
3. Conservative circuit-breaker settings (e.g. `HOST_MAX_RELAUNCHES=1`, `HOST_MAX_DOWN=1`) so a host-wide event is blocked.
4. Brief flag-on window (both flags for a LIVE subject), backend recreated `--no-deps --force-recreate` (never `--remove-orphans`).
5. Human confirms #50 flat, then deliberately stops **only #50's** terminal via the account-specific mechanism.
6. Prove **exactly one** relaunch of #50, identity re-verified, and a **genuine** collector snapshot (no fabrication).
7. Prove **no sibling** touched (every other account `skipped_allowlist`; no host circuit storm).
8. Restore both flags **OFF** byte-exact; recreate backend; verify DARK.
9. Collect the event-level latency evidence (detection→relaunch, relaunch→fresh-read; nulls where unavailable).
10. **Separate Sponsor decision** before any broader (pilot→full-cohort) steady-state activation.
