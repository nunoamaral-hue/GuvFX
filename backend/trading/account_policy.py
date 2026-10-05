"""trading.account_policy — the SINGLE authoritative DEMO/LIVE account-environment policy (Stream D1, 2026-10-05).

FOUR DISTINCT CONCEPTS — never conflated:
  1. **Account Environment** (DEMO | LIVE) — THIS module. Authoritative. Derived from ``TradingAccount.is_demo``
     (the per-account, Model-A lifecycle-instance truth). ``BrokerServer.environment`` is a CONSISTENCY signal
     only: a disagreement FAILS CLOSED (never silently pick one for a money-bearing account).
  2. **Monitoring Eligibility** — ``execution.readiness`` (environment-AGNOSTIC: a LIVE account may be observed,
     identity-matched and displayed). (Split introduced in D3.)
  3. **LIVE Execution Authorization** — ``execution.LiveExecutionAuthorization`` (D4; durable, human-gated).
  4. **Execution Readiness** — ``execution.readiness`` (the live order-time eligibility gate).

GOVERNANCE (Sponsor, 2026-10-05): every execution/strategy decision about DEMO vs LIVE MUST go through this module.
A raw ``account.is_demo`` / ``broker_server.environment`` comparison used to make an execution/strategy decision,
outside the explicitly-approved low-level modules, is forbidden and guarded by ``tests/test_account_policy_guard``.

This module does NOT change the DEMO/LIVE *stored* representation (no new enum): ``is_demo`` stays the stored truth
and existing accounts keep their classification. It centralizes the *interpretation* and makes it fail-closed.
"""
from __future__ import annotations


class Environment:
    DEMO = "DEMO"
    LIVE = "LIVE"


class AccountEnvironmentIntegrityError(Exception):
    """``is_demo`` disagrees with ``BrokerServer.environment`` (or the server env is unrecognised). The
    environment of a money-bearing account must NEVER be guessed — callers fail closed."""


def account_environment(account) -> str:
    """Return the AUTHORITATIVE environment (``Environment.DEMO`` / ``Environment.LIVE``) for ``account``.

    Derived from ``account.is_demo``; reuses the write-time ``trading.classification.classification_error`` rule to
    assert ``BrokerServer.environment`` agrees. On any disagreement / unrecognised server environment it raises
    ``AccountEnvironmentIntegrityError`` (fail closed — never silently resolve a mismatch)."""
    from django.core.exceptions import ObjectDoesNotExist
    from trading.classification import classification_error
    is_demo = bool(getattr(account, "is_demo", None))
    # ``getattr(..., None)`` swallows only AttributeError. An UNBOUND/absent server (``broker_server_id`` NULL)
    # resolves to None here — safe, cross-checked as "no server". But a bound ``broker_server_id`` whose
    # BrokerServer row is gone (a stale/orphaned FK — only reachable by a raw delete that bypasses
    # ``on_delete=PROTECT``) raises ObjectDoesNotExist from the descriptor, NOT AttributeError. We cannot
    # confirm the server environment, so for a money-bearing account we FAIL CLOSED with a sanitised integrity
    # error rather than guess or let DoesNotExist crash the many callers now routed through this policy.
    try:
        broker_server = getattr(account, "broker_server", None)
    except ObjectDoesNotExist:
        raise AccountEnvironmentIntegrityError(
            "broker_server could not be resolved (stale/orphaned FK); cannot classify the account")
    err = classification_error(is_demo, broker_server)
    if err:
        raise AccountEnvironmentIntegrityError(str(err))
    return Environment.DEMO if is_demo else Environment.LIVE


def is_demo_environment(account) -> bool:
    """FAIL-CLOSED demo gate: ``True`` ONLY when the environment cleanly resolves to DEMO. ``False`` for LIVE and
    ``False`` on ANY integrity error / mismatch. This REPLACES raw ``account.is_demo`` / ``broker_server.environment
    == 'live'`` checks in execution/strategy walls — behaviour-preserving for consistent DEMO accounts, blocks LIVE,
    and additionally fails closed on a demo/live disagreement (which an ad-hoc check could have let slip)."""
    try:
        return account_environment(account) == Environment.DEMO
    except AccountEnvironmentIntegrityError:
        return False


def is_live_environment(account) -> bool:
    """``True`` ONLY when the environment cleanly resolves to LIVE. ``False`` for DEMO and ``False`` on any
    integrity error (a mismatch is never treated as a clean LIVE). Use for LIVE-specific branches (D3/D4)."""
    try:
        return account_environment(account) == Environment.LIVE
    except AccountEnvironmentIntegrityError:
        return False


def demo_accounts_q(prefix: str = ""):
    """A ``Q`` object selecting DEMO accounts for QUERYSET pre-filters (e.g. routing/recovery candidate sets),
    centralising the field reference so it is not an ad-hoc ``is_demo`` read. The authoritative, fail-closed
    per-account decision still happens at the per-account gate downstream (defence in depth). ``prefix`` lets a
    related lookup target the account, e.g. ``demo_accounts_q("account__")`` -> ``Q(account__is_demo=True)``."""
    from django.db.models import Q
    return Q(**{f"{prefix}is_demo": True})
