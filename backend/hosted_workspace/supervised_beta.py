"""hosted_workspace.supervised_beta — ADR-0044 SUPERVISED_SINGLE_TENANT_BETA bounded gate (DARK, default OFF).

The Sponsor (2026-08-14) authorised an EXPLICITLY BOUNDED interim operational posture that lets the FIRST
end-to-end product validation advance a Hosted Workspace to EXECUTION_READY *without* the full behavioural
isolation certification (``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED``). It is NOT a certification and emits NO
certification marker; it is a coarse operational carve-out that bounds the still-un-certified forgeable-
observation risk (ADR-0041) to a SINGLE supervised, disposable, demo tenant on a throwaway host.

``supervised_single_tenant_beta_active(workspace)`` is the ONE predicate that opens the gate. It is FAIL-CLOSED
and returns True ONLY when EVERY one of the boundary conditions the Sponsor set holds:

  1. the ``SUPERVISED_SINGLE_TENANT_BETA_ENABLED`` flag is on (default OFF -> the whole gate is inert);
  2. the workspace resolves to a real ``TradingAccount``;
  3. that account is NOT Customer Zero (``tenant_isolation`` canonical definition);
  4. the account is a DEMO account (``is_demo is True``) — the demo-only wall, so the posture can only ever
     touch demo money (execution has its own independent demo walls in readiness + arm preconditions);
  5. the workspace is bound to an execution ``TerminalNode`` that is ACTIVE;
  6. that node is NOT a Customer-Zero / configured-forbidden node — derived LIVE from the DB via the same
     ``forbidden_execution_node_ids`` the co-residency guard uses (checked here UNCONDITIONALLY, independent of
     ``HOSTED_TENANT_NODE_ISOLATION_ENABLED``, so the supervised posture can never land a tenant on CZ's host);
  7. the node is SINGLE-TENANT for this account — no OTHER account occupies it, counting BOTH live legacy
     accounts (``terminal_node``) and hosted-workspace bindings (``execution_node`` OR ``workspace_node``).
     This is the load-bearing multi-tenant guard: the moment a second tenant shares the node the gate closes.

Any ambiguity, missing relation, or exception -> False (fail-closed). This module performs NO host or broker
action, arms nothing, and holds no secret; it is a pure, read-only predicate. When the full cert lands and
``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` is set, ``SUPERVISED_SINGLE_TENANT_BETA_ENABLED`` is simply turned off
and this posture dissolves with no code change (both are read as an OR in ``live_observe.live_observe_fn``).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("guvfx.hosted_workspace")


def _account_of(workspace):
    return getattr(workspace, "trading_account", None)


def node_is_single_tenant_for(node, account_id) -> bool:
    """True iff the PHYSICAL HOST behind ``node`` currently serves EXACTLY this one account and no other tenant.

    The trust unit is the physical host, addressed by ``rdp_host`` (the observer reaches the host via
    ``execution_node.rdp_host``, and the co-residency guard already treats ``rdp_host`` as the host identity) —
    NOT the ``TerminalNode`` row pk, because two node rows can share one ``rdp_host`` (no DB uniqueness). So we
    gather EVERY ACTIVE node row sharing this node's ``rdp_host`` (case-insensitive) and count occupancy across
    ALL of them. Occupancy sources: a live legacy account via ``terminal_node``, and any hosted workspace via
    ``execution_node`` OR ``workspace_node`` (delivery). A single OTHER occupant on the host -> False.

    Fail-closed: a blank ``rdp_host`` (host identity unknown) or any error returns False."""
    node_id = getattr(node, "pk", None)
    if node_id is None or account_id is None:
        return False
    try:
        from django.db.models import Q

        from execution.models import TerminalNode
        from hosted_workspace.models import HostedMt5Workspace
        from trading.models import TradingAccount

        acct_id = int(account_id)
        rdp = str(getattr(node, "rdp_host", "") or "").strip()
        if not rdp:
            return False  # host identity unknown -> cannot prove single-tenant -> fail closed
        # Every ACTIVE node ROW that resolves to the SAME physical host (same rdp_host, case-insensitive).
        host_node_ids = set(
            TerminalNode.objects
            .filter(rdp_host__iexact=rdp, status=TerminalNode.Status.ACTIVE)
            .values_list("id", flat=True)
        )
        host_node_ids.add(node_id)  # include this node even if a status/read race dropped it
        # (a) any OTHER live legacy account pinned to ANY node row of this host.
        other_active = (TradingAccount.objects
                        .filter(terminal_node_id__in=host_node_ids, is_active=True)
                        .exclude(pk=acct_id)
                        .exists())
        if other_active:
            return False
        # (b) any OTHER hosted workspace occupying ANY node row of this host in EITHER role.
        other_hosted = (HostedMt5Workspace.objects
                        .filter(Q(execution_node_id__in=host_node_ids)
                                | Q(workspace_node_id__in=host_node_ids))
                        .exclude(trading_account_id=acct_id)
                        .exists())
        return not other_hosted
    except Exception:  # noqa: BLE001 — any query/model error is ambiguous -> not single-tenant (fail-closed)
        logger.warning("supervised_beta: single-tenant probe failed for node=%s account=%s",
                       node_id, account_id, exc_info=True)
        return False


def supervised_single_tenant_beta_active(workspace) -> bool:
    """The bounded, fail-closed gate (ADR-0044). See module docstring for the seven conditions. Returns True
    ONLY for a single non-CZ DEMO tenant alone on a dedicated ACTIVE non-CZ node while the flag is on."""
    try:
        from hosted_workspace.flags import supervised_single_tenant_beta_enabled
        if not supervised_single_tenant_beta_enabled():                       # (1)
            return False
        acct = _account_of(workspace)                                         # (2)
        acct_id = getattr(acct, "id", None) or getattr(acct, "pk", None)
        if acct is None or acct_id is None:
            return False
        from hosted_workspace.tenant_isolation import (
            forbidden_execution_node_ids,
            is_customer_zero_account,
        )
        if is_customer_zero_account(acct_id):                                 # (3)
            return False
        if getattr(acct, "is_demo", False) is not True:                      # (4) demo-only wall
            return False
        node = getattr(workspace, "execution_node", None)                    # (5)
        node_id = getattr(node, "pk", None)
        if node is None or node_id is None:
            return False
        from execution.models import TerminalNode
        if getattr(node, "status", None) != TerminalNode.Status.ACTIVE:
            return False
        if node_id in forbidden_execution_node_ids():                        # (6) never a CZ / forbidden node
            return False
        if not node_is_single_tenant_for(node, acct_id):                     # (7) single-tenant guard
            return False
        return True
    except Exception:  # noqa: BLE001 — any failure is ambiguous -> gate CLOSED (fail-closed)
        logger.warning("supervised_beta: gate probe failed for workspace=%s",
                       getattr(workspace, "pk", None), exc_info=True)
        return False


def execution_permitted_under_posture(workspace) -> bool:
    """ADR-0044: whether a Hosted Workspace's EXECUTION (readiness / arm) may proceed given the trust posture.

    True when the full isolation cert is held (co-residency allowed — no single-tenant requirement); OR when we
    are NOT running the supervised posture (the observation trust anchor already governs — nothing extra here);
    OR when the bounded supervised carve-out CURRENTLY holds for THIS workspace. False ONLY when running the
    supervised posture (flag on, cert off) AND the single-tenant boundary is currently violated — so a SECOND
    tenant landing on the host fails execution closed at ORDER time (dispatch re-evaluates readiness before
    every order), not merely at observation time. Fail-closed on any error.

    This is the execution-side complement to the observation-side gate: together they make "single non-CZ demo
    tenant, alone" an invariant enforced at BOTH the observation and the order gate while uncertified."""
    try:
        from hosted_workspace.flags import (
            hosted_remoteapp_isolation_certified,
            supervised_single_tenant_beta_enabled,
        )
        if hosted_remoteapp_isolation_certified():
            return True
        if not supervised_single_tenant_beta_enabled():
            return True
        return supervised_single_tenant_beta_active(workspace)
    except Exception:  # noqa: BLE001 — fail-closed
        logger.warning("supervised_beta: posture probe failed for workspace=%s",
                       getattr(workspace, "pk", None), exc_info=True)
        return False
