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

WHAT THIS HELPER NEVER DOES: it reads NO credential; it considers NO execution authority (``execution_enabled`` /
authorizations are never read and never inferred) and NEVER grants trading authority; it performs NO host call; and
establishing eligibility NEVER itself relaunches, logs in, arms, advances state, or writes a snapshot. It reads the
Copy-Trading registries (``CopierMaster`` / ``CopierFollower``) for ONE purpose only — to EXCLUDE a master/follower
fail-closed (ELIGIBILITY-FIX-112), never to grant anything. ``is_active`` is DELIBERATELY NOT consulted: it is an
EXECUTION/TRADING-AUTHORITY control (it gates the order path in ``execution.readiness`` / ``trading.managed_start``),
not a monitoring-recovery control, so an established monitoring-only account intentionally left ``is_active=False``
(as every production LIVE monitoring account is) is still eligible to have its MONITORING terminal restored.
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
RC_DISABLED = "account_disabled"                  # RETIRED from this predicate (ELIGIBILITY-FIX-112): is_active is
                                                  # an EXECUTION-authority control, never a monitoring-recovery gate.
                                                  # Retained only for reason-vocabulary stability.
RC_NO_OWNER = "no_owner"                           # not owner-bound
RC_NOT_MONITORED = "not_monitored"                 # not a hosted per-tenant MONITORING subject per the authoritative
                                                   # model (no PERSISTENT_WORKSPACE provider AND no HostedMt5Workspace)
RC_RESERVED = "reserved_identity"                  # Customer Zero / account 18 / reserved set
RC_COPIER_PARTICIPANT = "copier_participant"       # Copy Trading master or follower — never auto-recovered here
RC_COPIER_CHECK_UNAVAILABLE = "copier_check_unavailable"  # copier membership indeterminate ⇒ fail closed (deny)
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
    cheapest/structural first so the FIRST failing gate names the reason. Reads no credential; grants no
    execution/trading authority; reads the Copy-Trading registries ONLY to EXCLUDE a master/follower (never to
    grant); performs no host call; never mutates anything.

    Mandatory evidence (ALL must agree; ``is_active`` is NOT among them — see gate 1):
      1. an EXISTING, owner-bound trading account (not tombstoned, has an owner);
      2. not a reserved / Customer Zero identity;
      3. a deterministic account-specific slot (``derive_slot(account.id)`` succeeds) — the relaunch target;
      3b. a HOSTED, per-tenant MONITORING subject per the authoritative monitoring model
          (``readiness_provider == PERSISTENT_WORKSPACE`` OR a ``HostedMt5Workspace`` row exists — the SAME
          disjunction ``_build_contexts`` uses, so a provider-field lag never false-negatives a monitored account);
      4. the workspace is THIS account's (``ws.trading_account_id == account.id``);
      4c. NOT a Copy Trading master or follower (fail-closed if copier membership is indeterminate);
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
    # 1 — existing, owner-bound, not tombstoned. NOTE (ELIGIBILITY-FIX-112): ``is_active`` is DELIBERATELY NOT a
    # gate here. It is an EXECUTION/TRADING-AUTHORITY control (``execution.readiness`` returns ``R_ACCOUNT_INACTIVE``
    # for it, and the order path / ``trading.managed_start`` gate on it), NOT a monitoring-recovery control. This
    # path only restarts the account's own ``terminal64`` — it NEVER arms, logs in, enables Copy Trading, or places
    # an order — so an established monitoring-only account intentionally left ``is_active=False`` (every production
    # LIVE monitoring account is) must still be eligible for MONITORING restoration. Establishment is governed by
    # the monitoring-enrolment gate (3b) + the identity-pin (6) + prior-verified-read (7) gates, never ``is_active``.
    if getattr(account, "disconnected_at", None) is not None:
        return _DENY(RC_DISCONNECTED)
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
    # 3b — MONITORING ENROLMENT (ELIGIBILITY-FIX-112; the explicit replacement for the retired ``is_active`` gate).
    # This path restores a HOSTED, per-tenant MONITORING subject's own terminal, so the account must be a hosted
    # monitoring subject per the AUTHORITATIVE monitoring model. ``execution.monitoring_enrolment._build_contexts``
    # classifies an account as hosted when ``readiness_provider == PERSISTENT_WORKSPACE`` OR a ``HostedMt5Workspace``
    # row exists for it (its verbatim rationale: "A workspace row means the account is on the hosted pipeline even if
    # readiness_provider lags"). This gate uses the SAME disjunction — never the narrower provider-only form, which
    # would FALSE-NEGATIVE an established monitoring account whose mutable ``readiness_provider`` still sits at the
    # model default while its terminal is down, re-opening the exact efficacy defect this packet fixes. A legacy /
    # global-agent account (no per-tenant workspace, non-PW provider) has no hosted slot to relaunch ⇒ not a
    # monitoring subject. ``is_active`` is NOT consulted (it is execution authority — see gate 1). Import-local
    # (``execution.readiness`` imports this app lazily; ``HostedMt5Workspace`` is this app's own model).
    from execution.readiness import PERSISTENT_WORKSPACE
    from hosted_workspace.models import HostedMt5Workspace
    hosted_subject = (str(getattr(account, "readiness_provider", "") or "") == PERSISTENT_WORKSPACE
                      or HostedMt5Workspace.objects.filter(trading_account_id=acct_id_int).exists())
    if not hosted_subject:
        return _DENY(RC_NOT_MONITORED)
    # 4 — the workspace belongs to THIS account (no substitution via a caller inconsistency).
    if ws is None or getattr(ws, "trading_account_id", None) != acct_id_int:
        return _DENY(RC_WS_ACCOUNT_MISMATCH)
    # 4c — COPY TRADING exclusion (ELIGIBILITY-FIX-112 Phase-3 invariant; regardless of flags). A Copy Trading
    # master or follower is NEVER auto-recovered on this path: its terminal is coupled to the copier control plane,
    # and an autonomous relaunch could perturb an active copy relationship. The Copy-Trading registries are read
    # ONLY to EXCLUDE (never to grant); on any error reading them, fail CLOSED (deny) — a possibly-copier account is
    # never recovered.
    try:
        from trade_copier.models import CopierFollower, CopierMaster
        is_copier = (CopierMaster.objects.filter(trading_account_id=acct_id_int).exists()
                     or CopierFollower.objects.filter(follower_account_id=acct_id_int).exists())
    except Exception:  # noqa: BLE001 — copier membership indeterminate ⇒ fail closed (never auto-recover)
        return _DENY(RC_COPIER_CHECK_UNAVAILABLE)
    if is_copier:
        return _DENY(RC_COPIER_PARTICIPANT)
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
