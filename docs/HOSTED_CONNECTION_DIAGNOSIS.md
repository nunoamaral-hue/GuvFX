# Hosted Workspace — WHY-not-CONNECTED diagnosis (DARK, read-only)

**Packet:** operations observability for hosted MT5 workspaces that have not reached CONNECTED.
**Status:** ENGINEERING COMPLETE + DARK. Backend-only, read-only, **no migration**, no deployment.
**Branch:** `feat/hosted-connect-diagnosis` (from `main` `ee47c36a`).

## Problem

A hosted account can sit at `delivery_state=AUTHORIZED` / `canonical_state=WAITING_FOR_LOGIN` with
`proj_connected=false` because the trusted observer has never corroborated its slot session. In production the
relevant flags are ON (`HOSTED_REMOTEAPP_ISOLATION_CERTIFIED=1`, `HOSTED_MT5_OBSERVATION_ENABLED=1`), so this is
**not** a trust-gate-off case — it is a corroboration mismatch / unpopulated slot runtime. Operations had **no
way to see WHICH corroboration gate failed** (the production Account-49 incident).

## What this adds (observability ONLY)

All new code is pure, read-only, secret-free. It **never contacts the host, never mutates workspace state, and
never feeds the trust/observer decision.** No persisted field, no migration (computed diagnosis, per the packet's
"prefer exposing a computed read-only diagnosis").

> **Release-gating correction (post-review):** the HTTP endpoint is gated by a DEDICATED default-OFF flag,
> `HOSTED_CONNECTION_DIAGNOSTICS_ENABLED`, checked FIRST and independently of the existing Hosted Workspace
> flags (which are already ON in production), and is STAFF-ONLY. The operator management command is unaffected
> (no flag gate, no HTTP surface). Fleet metrics are BOUNDED (DB aggregates for headline counts + a capped,
> single-query reason-distribution scan — no N+1).

- `backend/hosted_workspace/corroboration_diagnosis.py`
  - `classify_corroboration_result(result, *, workspace, now)` — faithful classifier that re-walks the EXACT gate
    order of `live_observe.corroboration_matches` + `build_observation_from_host` over a raw `OBSERVE_WORKSPACE`
    result and returns the FIRST failed gate's code. Imports the real pure gate primitives from `live_observe` /
    `producer` / `matching` so it cannot drift from the gates it explains.
  - `diagnose_workspace(workspace, *, now)` — operational diagnosis derived PURELY from durable persisted fields.
  - `observation_health_metrics(*, now)` — fail-open fleet metrics (never raises).
- `backend/hosted_workspace/diagnostics_views.py` — `GET /api/hosted-workspace/diagnostics/`. **STAFF-ONLY
  operational diagnostics, not a customer feature.** Gated FIRST by a DEDICATED, independent flag
  `HOSTED_CONNECTION_DIAGNOSTICS_ENABLED` (DEFAULT OFF): the route 404s while it is OFF **even when every other
  Hosted Workspace flag is ON** (production already runs the master/observation flags, so the endpoint must not
  ride on those). With the flag ON, a non-staff caller — including the workspace owner — still gets 404 (member
  exposure needs separate product + information-disclosure approval). Staff → fleet + metrics, or a selected
  account via `?account_id=`/`?workspace_uuid=`.
- `backend/hosted_workspace/management/commands/hosted_observation_diagnose.py` — operator command (no flag gate,
  no HTTP surface; staff by shell access) `manage.py hosted_observation_diagnose [--account <id>]`. Read-only.
- Route added in `backend/hosted_workspace/urls.py`.

## Reason codes → exact gate / field mapping

| Code | Gate (in `live_observe` / `corroboration_matches` / `producer`) | Persisted-state source |
|------|------------------------------------------------------------------|------------------------|
| `no_runtime_populated` | corroboration block absent / for another account (`corroboration_matches` a/c) | `observation_version == 0` (never accepted) — **the Account-49 case** |
| `no_slot_process_or_session` | `process_present` not True (d) / `session_id <= 0` (f) | `proj_process_running` not True |
| `wrong_session_owner` | `owner_user != guvfx_u_<id>` (e) | — (fails-closed persists nothing) |
| `runtime_root_mismatch` | `runtime_root != derive_slot(...)` (g) | — |
| `ipc_not_ready` | `attach_succeeded && ipc_available` not both True (producer) | `proj_ipc_available` not True |
| `identity_login_mismatch` | matcher `active_account_login_mismatch` | `currently_attached_login` vs `account_number` (legacy, best-effort) |
| `identity_server_mismatch` | matcher `active_account_server_mismatch` | `currently_attached_server` vs `broker_server.server_name` |
| `stale_heartbeat` | `collected_at` missing / outside the 60s anti-replay window (h + producer freshness) | `last_decision_at` older than the operational threshold |
| `environment_unresolved` | expected identity underivable (b) / matcher `classification_mismatch` / `live_execution_not_authorised` | `account_policy.account_environment` raises |
| `observer_task_not_scheduled` | host `OBSERVE` `ok:false` / `reason=no_observer_task` | — |

Benign terminals (not faults): `corroborated`, `broker_not_connected`, `connection_not_corroborated`,
`expected_identity_unconfigured`, `observe_unavailable`, `no_account`, plus flag-posture codes.

## Redaction

Output is booleans / enum codes / coarse ages only — **no credential, full login, Windows username, or
runtime/terminal path.** Sensitive fields (`owner_user`, `runtime_root`, `account_number`, `currently_attached_*`)
are read for comparison and NEVER echoed. `account_id` / `owner_id` are DB ids (staff context only, same as the
existing `onboarding_ops` surface). A host-provided `reason` string is sanitised to a strict lower_snake shape.

## Verification (exact commands + results)

- New focused suite: `DB_NAME=<isolated> manage.py test hosted_workspace.tests_corroboration_diagnosis
  hosted_workspace.tests_diagnostics_api` → **50 OK** (classifier per-gate accuracy incl. positive control +
  LIVE flag-on/off faithfulness; state-derived accuracy; redaction; no-mutate; the dedicated-flag gate
  (master-on + diagnostics-off ⇒ 404); staff-only (non-staff incl. owner ⇒ 404); bounded metrics / truncation;
  malformed-uuid ⇒ 404; command smoke).
- Full regression (serial): `DB_NAME=<isolated> manage.py test hosted_workspace core` → **1385 OK (skipped=1)**.
- `manage.py makemigrations --check --dry-run` → **no `hosted_workspace` migration** (pre-existing unrelated local
  index-rename noise in backtests/reliability/signal_intake/strategies only; none introduced here).
- `python3 scripts/check_no_secrets.py` → **no findings.**

## Independent adversarial review (disposition)

Reviewed twice by independent general-purpose subagents against the real gates
(`live_observe.py`/`producer.py`/`matching.py`) and the persistence writer.

**Second review (post release-gating revision):** verified the dedicated-flag gate (404 while
`HOSTED_CONNECTION_DIAGNOSTICS_ENABLED` is OFF, even with master/observation flags ON; checked first, before any
DB read), staff-only audience (non-staff incl. owner → 404; no member path), the ungated read-only command, and
BOUNDED metrics (DB aggregates + capped single-query scan, **no N+1** — traced that `diagnose_workspace` reads
only `select_related` fields). **No HIGH/MED.** Two LOW fixed: a malformed `workspace_uuid` now → 404 (not 500);
a truncated metrics scan never reports HEALTHY.

**First review (initial diff):** leakage / mutation / owner-scope **clean**. Findings fixed:

- **MED — classifier drift (false-`corroborated`).** `_expected_account` ignored `hosted_live_monitoring_enabled()`,
  so a clean LIVE account with the flag OFF (the DARK default) could classify `corroborated` where the production
  matcher does not match it. **Fixed** to mirror the producer's exact flag-gated derivation (demo-only default;
  LIVE opt-in only under the flag). New tests assert both flag states + the demo-only faithfulness.
- **LOW — freshness ordering.** Moved the freshness-*window* check after the connection-agreement gate (keeping the
  `collected_at`-present check at the structural gate), mirroring the producer's `fresh` ordering.
- **LOW — mislabelled benign code.** Added distinct `identity_not_observable` (connected + IPC up but active id not
  yet readable) instead of reusing `broker_not_connected`.
- **LOW — host-reason echo.** `_safe_code` now requires a letter (rejects an all-digit login-shaped value).
- **LOW — free-text field echo.** `delivery_reason` / `canonical_reason` pass through a display sanitiser.
- **Robustness.** The metrics loop now guards each row (one bad row cannot collapse the fleet payload;
  `diagnose_errors` counter added).

## Scope / governance

- Decision class: **Green** (additive, behaviour-preserving, read-only observability within the hosted_workspace
  diagnostics surface). It does NOT touch the observer, the delivery writer, the single state writer, analytics,
  or the frontend (a sibling session owns dashboard/analytics + frontend).
- `core/operational_health.py` `_PROBES`/rollup is deliberately **not** modified (the metric is exposed via the
  staff endpoint + command, reusing `HealthState`, so the global rollup severity is unchanged). Noted here so a
  future increment can add a probe if operations want the metric in the unified rollup.
- Shared `docs/STATUS.md` / `docs/NEXT.md` were left unedited to avoid a merge conflict with the concurrent
  sibling session; this file is the packet's concise git evidence.
