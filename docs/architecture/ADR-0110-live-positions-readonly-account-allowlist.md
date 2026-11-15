# ADR-0110 — Account-level allow-list for LIVE read-only position monitoring

- **Status:** Accepted (repository-only; additive; migration-free; **default-OFF / fail-closed**; no execution /
  Copy-Trading authority; no credential handling; no production/host mutation, no flag enablement by this packet).
- **Packet:** GFX-DASHBOARD-LIVE-POSITIONS-READONLY-GATE-135.
- **Date:** 2026-11-15.
- **Builds on (never replaces):** GFX-LIVE-POSITIONS-READONLY-001 / ADR-0072 (the bridge read-only positions flag
  `MT5_LIVE_READONLY_POSITIONS_ENABLED`, consulted in exactly one read path — `fetch_positions`), ADR-0094
  (route-scoped follower read authority), ADR-0049/0049.1 (signed per-tenant bridge-env transport + host env-key
  allow-list + `MT5_ALLOW_LIVE` posture + host time-bound), and ADR-0100 (dashboard structural/transient reason
  taxonomy). The execution stack (`allow_live_for_account`, `live_execution_permitted`, `LiveExecutionAuthorization`,
  `_bridge_live_admitted`, `_copier_live_mutation_admitted`, `HOSTED_LIVE_EXECUTION_ENABLED`) is **unchanged**.

## Context

Post-`ac5bdf0a` the dashboard truthfully classifies six LIVE accounts (#48, #53, #54, #56, #57, #59; owner user 29,
all `persistent_workspace`) as `positions_unsupported_live` / **structural**: their `/mt5/positions` read returns
HTTP 400 `account_not_demo` because the bridge demo-gates live-position reads unless its per-tenant env carries
`MT5_LIVE_READONLY_POSITIONS_ENABLED` (`scripts/mt5_signal_bridge.py:2159`). The capability to serve a LIVE account's
positions **read-only** already exists end-to-end and is **provably execution-independent**:

- The bridge consults the read flag in **exactly one** path, `fetch_positions`; it never appears in any
  order/open/close/modify path. A read reaches only `positions_get()` — never `order_send`. (Verified, 5-lane trace.)
- The genuine execution boundary is a **physically separate** mechanism (`_bridge_live_admitted` for opens;
  `_copier_live_mutation_admitted` for close/modify) that never reads the monitoring flag.
- The backend arming decision `readonly_positions_for_account` is independent of every execution gate and fail-closed.

**The gap this ADR closes.** Before this change the read flag could be armed only two ways: a **global** flag
(`LIVE_READONLY_POSITIONS_MONITORING_ENABLED`) that would arm *every* LIVE account at once (a broad bypass), or the
**route-scoped follower** authority (ADR-0094) that binds to a copier follower with a live `CopierLiveAuthorization`
(covers #48 only, and only during an authorized window). There was **no account-level allow-list** for approved
monitoring accounts that are not copier followers (#53/#54/#56/#57/#59 are a member's own hosted LIVE terminals). The
packet requires "account-level capability/allow-listing rather than a global bypass", default-OFF for unapproved
accounts.

## Decision

Add a third, **default-OFF, account-level** arming path to the existing execution-independent decision, and permit the
read-only key through the host transport validator so a rendered monitoring env is not structurally rejected.

1. **`execution/bridge_config.py`** — new `LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST` (settings override, else env; a
   comma/whitespace-separated list of integer account ids). `readonly_positions_for_account(account)` returns True when
   the account is authoritatively LIVE **and** (global monitoring armed **OR** the account id is on the allow-list
   **OR** route-scoped authorized follower). `follower_positions_read_readiness` gains the truthful
   `("FOLLOWER_POSITIONS_READ_OK", "account_allowlisted")` reason. Parsing is **fail-closed**: unset/blank ⇒ empty
   set ⇒ every account denied; a non-integer token is ignored and can never widen the set; a missing/non-integer
   account id denies. This is purely an arming **decision** — it still only ever sets the READ flag
   (`MT5_LIVE_READONLY_POSITIONS_ENABLED`), never `MT5_ALLOW_LIVE` or any execution authority.
2. **`hosted_workspace/host_agent_dispatch.py`** — add `MT5_LIVE_READONLY_POSITIONS_ENABLED` to
   `_BRIDGE_ENV_ALLOWED_KEYS`. `render_bridge_env` can already emit this key; the host validator previously did not
   list it, so a rendered monitoring env would have been rejected `payload_env_key_not_allowed`. The key is
   **orthogonal to the `MT5_ALLOW_LIVE` posture gate**: RESTORE (execution-OFF) still refuses `MT5_ALLOW_LIVE`,
   ACTIVATE still requires it, and the read key can never satisfy the LIVE posture.

## Consequences

- **Default-OFF / byte-identical until enabled.** With the allow-list unset (production default) every account —
  including the six — remains refused exactly as today; the dashboard keeps showing them `positions_unsupported_live`
  / structural. No account is armed, no bridge env changes, nothing is rendered differently.
- **Read stays separated from execution.** Arming an account for read-only monitoring confers no order, modify, close,
  cancel, pending-order, or Copy-Trading authority. #48 (the copier follower) may be allow-listed for *reads* with its
  follower/execution boundary independently OFF — the read allow-list is never consulted by any execution path.
- **Account-scoped, owner-isolated.** Only explicitly listed ids are armed, only when LIVE. Each account's bridge still
  pins its own identity (`MT5_REQUIRE_IDENTITY_PIN`, login/server/username/workspace_uuid) and the dashboard read still
  passes the unchanged per-account identity firewall, so an allow-list entry can only ever enable *that* account's own
  positions read.

## Deliberate boundaries / out of scope (separately Sponsor-gated)

This PR delivers the **decision capability** only, default-OFF. Making the six accounts actually readable additionally
requires, as separate governed steps (HARD STOP for this packet — no host mutation, no flag enablement):

1. Populate `LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST` with the approved account ids (enablement).
2. A monitoring-provisioning path that renders each approved account's bridge env with
   `readonly_positions=readonly_positions_for_account(account)` and a host op to apply it + restart the bridge
   (host mutation). No such op is created here; the copier `live_bridge_provision` render path is execution-scoped and
   is intentionally **not** reused for monitoring.
3. Deploy + enable, then owner VISUAL acceptance.

The broad `LIVE_READONLY_POSITIONS_MONITORING_ENABLED` flag is retained (no silent deletion) as a separate authority;
the account-level allow-list is the **preferred** account-scoped mechanism and should be used instead of the global
flag for approved monitoring accounts.
