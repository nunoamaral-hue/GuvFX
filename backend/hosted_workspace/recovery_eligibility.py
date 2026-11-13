"""hosted_workspace.recovery_eligibility — the SINGLE authoritative onboarding-vs-established classifier for
automatic MT5 terminal-relaunch recovery (ESTABLISHED-ACCOUNT-AUTO-RECOVERY-109, 2026-11-13).

THE PROBLEM (packet-108 forensic, proven in prod on account #50): an ESTABLISHED, previously-monitored,
identity-pinned account whose MT5 terminal EXITS is classified as "onboarding" and refused an automatic
relaunch, because BOTH recovery mechanisms keyed "onboarding" off ``trading_account.workspace_confirmed_at is
None``:

  * ``capability_recovery`` returns ``skipped_onboarding`` for any unconfirmed account — correct for its
    ARMING-capability purpose, but it tarred an established MONITORING-only account with the same brush; and
  * ``liveness_recovery`` only ever considered ARMED (``execution_enabled`` ∧ ``execution_authorized_at``)
    workspaces, so a monitoring-only account was NEVER a candidate for the one mechanism that relaunches a
    down terminal.

Result: a monitoring-only account such as #50 (bridge healthy, terminal dead, observer projection frozen) had
NO automatic relaunch path and depended on an EXTERNAL/human relaunch.

THE CONTRACT (fail-closed): :func:`established_account_recovery_eligible` returns ``eligible=True`` ONLY when
ALL mandatory, DURABLE, account-scoped, server-authoritative evidence agrees that this is an ESTABLISHED,
identity-pinned account with PRIOR VERIFIED monitoring evidence whose missing terminal may be safely relaunched
through the EXISTING account-specific supported relaunch mechanism. Any missing, ambiguous, mismatched or
indeterminate evidence ⇒ INELIGIBLE with a stable reason code — NEVER a default toward relaunch.

THE TRUST BOUNDARY (the load-bearing design decision, see ADR-0107): for a MONITORING relaunch (which NEVER
arms, logs in, or places an order) the trust anchor is PRIOR VERIFIED EVIDENCE — a durable, immutable,
identity-matched ``account_read`` equity snapshot under the account's CURRENT identity pin — NOT the ADR-0034
customer-confirmation ACK (``workspace_confirmed_at``). Confirmation remains the trust anchor for ARMING
(capability recovery / auto-arm); it is deliberately NOT required here, because an account that has previously
produced an identity-verified broker read has already proven the exact (login, server, slot, terminal) it would
be relaunched into. A genuinely-new / awaiting-first-login account has no such evidence and so is always
INELIGIBLE (``no_prior_verified_read``), fail-closed.

WHAT THIS HELPER NEVER DOES: it reads NO credential; it considers NO execution / Copy-Trading authority
(``execution_enabled`` / authorizations / copier models are never read and never inferred); it performs NO host
call; and establishing eligibility NEVER itself relaunches, logs in, arms, advances state, or writes a snapshot.
The dynamic "is the terminal actually down" determination is NOT part of this helper — it stays in the liveness
pass's READ-ONLY observe (``process_running is False``, never inferred), so eligibility answers only "is this the
KIND of account that may be relaunched", and the observe answers "does it need relaunching right now".
"""
from __future__ import annotations

import logging
from typing import NamedTuple

logger = logging.getLogger("guvfx.hosted_workspace")

# Reserved / NEVER-touched identities — reuse the ONE definition from capability_recovery (no divergence). This
# import is one-directional at module-load time (capability_recovery's top-level imports do NOT include this
# module — it imports recovery_eligibility locally inside its function), so there is no import cycle.
from hosted_workspace.capability_recovery import _RESERVED_ACCOUNT_IDS

# ── Stable, secret-free reason codes (the classifier's full vocabulary) ───────────────────────────────────────
RC_ELIGIBLE = "eligible"
RC_NO_ACCOUNT = "no_account"
RC_DISCONNECTED = "account_disconnected"          # tombstoned (disconnected_at set) — decommissioning
RC_DISABLED = "account_disabled"                  # is_active False
RC_NO_OWNER = "no_owner"                           # not owner-bound
RC_RESERVED = "reserved_identity"                  # Customer Zero / account 18 / reserved set
RC_SLOT_UNDERIVABLE = "slot_underivable"           # derive_slot refused (e.g. non-positive id)
RC_WS_ACCOUNT_MISMATCH = "workspace_account_mismatch"   # ws.trading_account_id != account.id (caller inconsistency)
RC_NO_ENDPOINT = "no_endpoint"                     # no live (non-RETIRED) per-tenant endpoint for this account
RC_ENDPOINT_WS_MISMATCH = "endpoint_workspace_mismatch"  # endpoint denormalised owner drift (collision guard)
RC_NO_IDENTITY_PIN = "no_identity_pin"             # endpoint expected_login/expected_server blank
RC_NO_PRIOR_VERIFIED_READ = "no_prior_verified_read"     # genuinely new / awaiting first login — no evidence
RC_IDENTITY_PIN_DRIFT = "identity_pin_drift"       # prior reads exist but none under the CURRENT pin (rebind)
RC_LIVE_RECOVERY_DISABLED = "live_recovery_disabled"     # LIVE env + HOSTED_LIVE_RECOVERY_ENABLED off
RC_ENV_INDETERMINATE = "environment_indeterminate"       # neither clean demo nor clean live (fail closed)


class EligibilityResult(NamedTuple):
    """``(eligible, reason)`` — ``reason`` is always one of the ``RC_*`` codes above (``RC_ELIGIBLE`` iff
    ``eligible`` is True). Immutable and secret-free; safe to log/aggregate."""
    eligible: bool
    reason: str


_DENY = lambda code: EligibilityResult(False, code)  # noqa: E731 — tiny local constructor for readability


def established_account_recovery_eligible(ws, account) -> EligibilityResult:
    """Fail-closed: may this ESTABLISHED account's DOWN terminal be safely relaunched by the automatic MONITORING
    recovery? Returns ``EligibilityResult``. Evaluates ONLY durable, account-scoped, server-authoritative facts,
    cheapest/structural first so the FIRST failing gate names the reason. Reads no credential; infers no
    execution/Copy-Trading authority; performs no host call; never mutates anything.

    Mandatory evidence (ALL must agree):
      1. an EXISTING, ENABLED, owner-bound trading account (not tombstoned, not disabled, has an owner);
      2. not a reserved / Customer Zero identity;
      3. a deterministic account-specific slot (``derive_slot(account.id)`` succeeds) — the relaunch target;
      4. the workspace is THIS account's (``ws.trading_account_id == account.id``);
      5. a live (non-RETIRED) per-tenant ``HostedExecutionEndpoint`` owned by THIS account, whose denormalised
         ownership agrees with the workspace (no endpoint/workspace collision);
      6. a non-empty expected (login, server) IDENTITY PIN on that endpoint;
      7. PRIOR VERIFIED EVIDENCE — ≥1 immutable, identity-matched ``account_read`` equity snapshot for THIS
         account, AND at least one such snapshot under the CURRENT endpoint login pin (so a since-rebound
         identity fails closed → human login required);
      8. a RECOVERABLE environment — a clean DEMO account, or a clean LIVE account only when the governed
         ``HOSTED_LIVE_RECOVERY_ENABLED`` infrastructure-recovery gate is on (never widens the LIVE surface).
    """
    if account is None:
        return _DENY(RC_NO_ACCOUNT)
    # 1 — existing, enabled, owner-bound, not tombstoned.
    if getattr(account, "disconnected_at", None) is not None:
        return _DENY(RC_DISCONNECTED)
    if getattr(account, "is_active", False) is not True:
        return _DENY(RC_DISABLED)
    if getattr(account, "user_id", None) is None:
        return _DENY(RC_NO_OWNER)
    # 2 — reserved / Customer Zero (coerce-total; a non-numeric id fails toward applying the guard).
    acct_id = getattr(account, "id", None)
    try:
        acct_id_int = int(acct_id)
    except (TypeError, ValueError):
        return _DENY(RC_RESERVED)
    from hosted_workspace.tenant_isolation import is_customer_zero_account
    if acct_id_int in _RESERVED_ACCOUNT_IDS or is_customer_zero_account(acct_id_int):
        return _DENY(RC_RESERVED)
    # 3 — deterministic account-specific slot (the exact existing relaunch target).
    from hosted_workspace.host_agent_dispatch import derive_slot
    try:
        derive_slot(acct_id_int)
    except Exception:  # noqa: BLE001 — a non-derivable slot is fail-closed, never a relaunch
        return _DENY(RC_SLOT_UNDERIVABLE)
    # 4 — the workspace belongs to THIS account (no substitution via a caller inconsistency).
    if ws is None or getattr(ws, "trading_account_id", None) != acct_id_int:
        return _DENY(RC_WS_ACCOUNT_MISMATCH)
    # 5/6 — a live, account-owned endpoint with a non-empty identity pin (and no endpoint/workspace drift).
    from execution.models import HostedExecutionEndpoint
    retired = HostedExecutionEndpoint.State.RETIRED
    ep = (HostedExecutionEndpoint.objects
          .filter(trading_account_id=acct_id_int)
          .exclude(state=retired)
          .order_by("-id")
          .first())
    if ep is None:
        return _DENY(RC_NO_ENDPOINT)
    if getattr(ep, "workspace_id", None) != getattr(ws, "pk", None):
        return _DENY(RC_ENDPOINT_WS_MISMATCH)
    expected_login = (ep.expected_login or "").strip()
    expected_server = (ep.expected_server or "").strip()
    if not expected_login or not expected_server:
        return _DENY(RC_NO_IDENTITY_PIN)
    # 7 — PRIOR VERIFIED EVIDENCE: an immutable, identity-matched account_read snapshot under the CURRENT pin.
    # A row is written ONLY from an identity-verified fresh broker read (capture_account_snapshot,
    # verify_identity=True), so observed_login non-empty ⇒ it matched the expected identity AT WRITE TIME; the
    # current-pin filter additionally fails closed on a since-changed login (rebind). Scoped to THIS account id
    # ⇒ no cross-owner / another-tenant evidence can ever qualify it.
    from analytics.models import AccountEquitySnapshot
    verified = (AccountEquitySnapshot.objects
                .filter(trading_account_id=acct_id_int, source="account_read")
                .exclude(observed_login=""))
    if not verified.exists():
        return _DENY(RC_NO_PRIOR_VERIFIED_READ)
    if not verified.filter(observed_login=expected_login).exists():
        return _DENY(RC_IDENTITY_PIN_DRIFT)
    # 8 — recoverable environment (demo always; LIVE only behind the governed infra-recovery gate; else closed).
    from trading.account_policy import is_demo_environment, is_live_environment
    if is_live_environment(account):
        from hosted_workspace.flags import hosted_live_recovery_enabled
        if not hosted_live_recovery_enabled():
            return _DENY(RC_LIVE_RECOVERY_DISABLED)
    elif not is_demo_environment(account):
        return _DENY(RC_ENV_INDETERMINATE)
    return EligibilityResult(True, RC_ELIGIBLE)
