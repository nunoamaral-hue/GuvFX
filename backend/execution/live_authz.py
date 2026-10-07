"""Stream D4 (§3) — the ONE centralized LIVE-execution authorization validity/invalidation policy.

Every execution/strategy wall that must additionally gate a LIVE account on a durable, human-written
authorization calls ``is_live_execution_authorized`` here (never re-deriving it), so the rule lives in one
fail-closed place. This module is DEMO-agnostic: a DEMO account's execution is gated solely by the existing
``is_demo_environment`` path and never touches this — callers only consult this for a LIVE account.

Validity (all required, fail-closed on any absence/ambiguity/error):
  * an ACTIVE, non-revoked ``LiveExecutionAuthorization`` row exists for THIS account lifecycle instance
    (a re-added Model-A account is a new instance ⇒ needs a new authorization — the FK binds the instance);
  * its ``broker_identity_snapshot`` still equals the account's CURRENT pinned identity (login + server) —
    identity drift (a different/absent identity than was reviewed) invalidates it.

This grants NO order authority by itself: it is a precondition the readiness gate (D4a) and the strategy/
planning/promotion/routing walls (D4b) AND the arm gate consult; the per-runtime ``MT5_ALLOW_LIVE`` boundary
(D4b) and the order-time bridge identity-pin gate remain the final money-path authority. No real order is
possible from this module.
"""
from __future__ import annotations


def _current_identity(account):
    """The account's current pinned (login, server) as trimmed strings. Never raises."""
    login = str(getattr(account, "account_number", "") or "").strip()
    try:
        server = str(getattr(getattr(account, "broker_server", None), "server_name", "") or "").strip()
    except Exception:  # noqa: BLE001 — a stale/unresolvable FK must not raise into a gate
        server = ""
    return login, server


def live_authorization_for(account):
    """The single ACTIVE, non-revoked ``LiveExecutionAuthorization`` for this account instance, else ``None``.
    Never raises (absence/error ⇒ None ⇒ the caller fails closed)."""
    try:
        from execution.models import LiveExecutionAuthorization
        acct_id = getattr(account, "id", None) or getattr(account, "pk", None)
        if acct_id is None:
            return None
        return (LiveExecutionAuthorization.objects
                .filter(trading_account_id=acct_id, is_active=True, revoked_at__isnull=True)
                .order_by("-created_at")
                .first())
    except Exception:  # noqa: BLE001 — fail closed: any DB/import error ⇒ not authorized
        return None


def _hosted_live_execution_enabled() -> bool:
    """D4 DARK gate (import-local; fail-closed). OFF ⇒ no LIVE account is ever order-path admitted."""
    try:
        from hosted_workspace.flags import hosted_live_execution_enabled
        return hosted_live_execution_enabled()
    except Exception:  # noqa: BLE001
        return False


def live_execution_permitted(account) -> bool:
    """D4b — the ONE fail-closed order-path predicate every wall uses to ADMIT a LIVE account: the DARK flag
    is ON **and** a valid, human-written authorization exists (``is_live_execution_authorized``). Mirrors the
    readiness condition-11 logic exactly so the router / planning / promotion walls and the readiness gate all
    admit a LIVE account by identical criteria from one place. DEMO is NOT this function's concern — callers
    admit DEMO via ``is_demo_environment`` and consult this ONLY for a non-demo account. It NEVER authorizes an
    order by itself: the per-runtime ``MT5_ALLOW_LIVE`` boundary + the order-time bridge identity-pin gate
    remain the final money-path authority. Fail-closed on any error."""
    try:
        return _hosted_live_execution_enabled() and is_live_execution_authorized(account)
    except Exception:  # noqa: BLE001
        return False


def is_live_execution_authorized(account) -> bool:
    """FAIL-CLOSED: ``True`` ONLY when an active, non-revoked authorization exists for THIS account instance
    AND its ``broker_identity_snapshot`` still matches the account's current pinned (login, server), with a
    non-empty login. Any absence, identity drift, malformed snapshot, or error ⇒ ``False``.

    NOTE this is NOT about DEMO vs LIVE classification (that is ``account_policy``) and it NEVER authorizes an
    order — it only reports whether the durable human authorization is currently valid; callers gate LIVE on
    it IN ADDITION to the environment policy, the arm bit, ADR-0047, readiness and the order-time bridge gate."""
    try:
        authz = live_authorization_for(account)
        if authz is None:
            return False
        snap = authz.broker_identity_snapshot or {}
        login, server = _current_identity(account)
        if not login:
            return False   # an unpinned identity can never be authorized
        if not (str(snap.get("login", "")).strip() == login
                and str(snap.get("server", "")).strip() == server):
            return False   # identity drift
        return _material_facts_unchanged(authz, account)
    except Exception:  # noqa: BLE001 — total fail-closed
        return False


def _material_facts_unchanged(authz, account) -> bool:
    """D4c invalidation: the authorized STRATEGY + SIZING must still match what was authorized. A reassigned,
    removed, deactivated, or resized strategy is a MATERIAL change that makes the authorization stale ⇒ invalid
    (a fresh ceremony is required). Fail-closed on any error. The sizing fingerprint uses the SAME function the
    ceremony wrote with, so the comparison is exact."""
    try:
        from strategies.models import StrategyAssignment
        from hosted_workspace.provisioning import sizing_snapshot_for
        asn = getattr(authz, "strategy_assignment", None)
        if asn is None:
            return True    # minimal (no-strategy) authorization ⇒ identity-only validity; nothing to invalidate.
                           # Every ceremony-written authz HAS a strategy, so the full material check below applies.
        if not getattr(asn, "is_active", False):
            return False
        acct_id = getattr(account, "id", None) or getattr(account, "pk", None)
        if getattr(asn, "account_id", None) != acct_id:
            return False
        if str(getattr(asn, "stage", "")) != StrategyAssignment.STAGE_LIVE:
            return False
        # The authorized assignment must be the account's SOLE active LIVE assignment: a newly-added OR a
        # swapped-in different strategy is a material change that invalidates (so the authz can never admit a
        # strategy it did not authorize).
        active_live = list(StrategyAssignment.objects.filter(
            account_id=acct_id, is_active=True, stage=StrategyAssignment.STAGE_LIVE).values_list("pk", flat=True))
        if active_live != [asn.pk]:
            return False
        return sizing_snapshot_for(asn) == (authz.sizing_snapshot or {})
    except Exception:  # noqa: BLE001 — fail-closed
        return False
