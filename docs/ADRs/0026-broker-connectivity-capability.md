# ADR-0026 — Broker connectivity, account management & health (capability design + phase plan)

- Status: Design accepted; **capability PAUSED at design** pending Phase 1 (the in-place broker-login
  validation primitive). Sponsor-directed (2026-08-02).
- Related: ADR-0021 (dedicated-runtime onboarding, broker-login gate), ADR-0025 (broker-server resolution),
  `.claude/rules/data.md` (immutable evidence), `.claude/rules/architecture.md` (small additive changes).

## Context (read-only investigation, 6 areas)

The requested "complete broker-connectivity system" is **largely greenfield** and collides with the current
architecture at load-bearing points:

- **No credentialed broker-login primitive exists.** The provisioner `configure()` sends no password and
  `verify()` hardcodes `logged_in=False` (broker-INDEPENDENT phase; `PROVISIONING_REQUIRE_BROKER_LOGIN` OFF).
  The wired "Test connection" is an EA-presence check that **409s for every dedicated-runtime account**
  (`mt5_instance=None`, incl. Customer Zero). The only real login test (`bridge login_and_validate`) is
  **orphaned + session-destructive** (`mt5.shutdown()`). **Credential injection only runs inside a
  re-materialise cycle** — there is no in-place login.
- `validation_status`/`validated_at` have **no writer** — every self-service account is permanently `NEVER`;
  the health UI has no data source.
- Deletion is **CASCADE** (would destroy immutable `Trade`/execution history — violates `data.md`) and also
  blocked by **PROTECT** FKs (→ unhandled 500) for any real account.
- "One active account per user" is **not modelled** (constraint keys on `mt5_instance`, which is NULL);
  routing is **signal-source-scoped, not active-account-scoped**; auto-exec is **DEMO-only, LIVE undefined**.
- No continuous health monitoring / in-app notifications / execution auto-pause exist. The `reliability` app
  (`reliability_tick`, `AlertEvent` dedup, `TradingHealthSnapshot.can_trade`, recovery models) is the right
  home to **extend** — but it is shadow-only, probes a single global bridge (not per-runtime), has been
  flag-OFF, has no per-user in-app inbox, and `AlertAcknowledge` is staff-only.
- The **production frontend diverges** from the local repo (VPS-hosted); the local accounts page is stubbed.
- **Customer Zero** is `validation_status=NEVER` + `broker_login_verified=False` + `mt5_instance=None`; a
  naïve health/migration would wrongly flag it and could trigger a re-login/re-provision (forbidden).

## Decisions (Sponsor-confirmed 2026-08-02)

1. **Validation backbone:** build the **in-place broker-login validation primitive FIRST** as a separate,
   host-certified packet (Phase 1). It must validate a login+password+server against the already-RUNNING
   runtime **without** re-materialising, restarting, placing a trade, or destroying the session. The broader
   capability pauses at design until that primitive is reviewed and proven. **No simulated/permanently-stubbed
   validation; re-provisioning is NOT the normal customer validation mechanism.**
2. **Deletion:** **soft-disconnect + immediate credential destruction + non-secret audit tombstone; never
   row-delete** where it would damage `Trade`/execution evidence or protected relations. Customer-facing:
   "deleted and disconnected immediately." Retain the minimum non-secret identity for audit + referential
   integrity.
3. **Strategy resumption:** after connectivity is restored, execution paused by an auth/health failure
   **requires explicit customer confirmation to resume — never automatic.** The UI explains which strategies
   resume and on which active account. (Auto-resume reconsidered later with Trusted-Beta evidence.)
4. **Delivery:** governed **additive phases**; LIVE-account / real-money execution remain deferred.

## Customer-facing health model (design)

Three states: **Connected & ready** / **Needs attention** (actionable: invalid/changed password, disabled,
identity mismatch, trading disabled, outage beyond retry window — execution paused) / **Disconnected** (user
disconnected/deleted). Internal model is richer and **must not overload `is_active`**: separate credential
lifecycle, validation-attempt lifecycle, broker health, activation, runtime connectivity, execution-paused.
Transient (timeout/maintenance/DNS/brief loss) → bounded-backoff retry, retain creds, degraded, notify only
past threshold. Credential/account failure → prompt Needs Attention, pause execution, notify, offer edit.

## Phase plan

- **Phase 1 — in-place broker-login/test primitive** (design + host-certify). *Active.*
- **Phase 2 — backend**: account lifecycle, `ValidationAttempt` records + health states, credential
  replacement, disconnect/tombstone, one-active-per-user rules, execution-pause controls.
- **Phase 3 — frontend**: modal validation journey, progress polling, edit, activation, deletion,
  notifications.
- **Phase 4 — continuous broker-health monitoring** (extend `reliability_tick`, per-runtime-addressed) +
  controlled recovery (manual-resume).
- **Phase 5 — Customer Zero production deployment + broker-login validation.**
- **Phase 6 — separately authorised strategy + crypto execution testing.**

## Consequences

- Nothing is implemented under this ADR yet; it records the design + governance decisions so they are durable.
- Customer Zero remains broker-independent RUNNING; provisioner DARK; `PROVISIONING_REQUIRE_BROKER_LOGIN` OFF.
- Immutable-evidence rule is honoured (tombstone-not-delete); the manual-resume default matches the
  platform's fail-closed posture.
