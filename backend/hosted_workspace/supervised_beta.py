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
     ``HOSTED_TENANT_NODE_ISOLATION_ENABLED``, so the supervised posture can never land a tenant on CZ's NODE —
     this is what keeps Customer Zero protected under the ADR-0044 amendment's host co-residency);
  7. the node is SINGLE-TENANT for this account — no OTHER account occupies THIS ``TerminalNode``, counting
     BOTH live legacy accounts (``terminal_node``) and hosted-workspace bindings (``execution_node`` OR
     ``workspace_node``). ADR-0044 AMENDMENT (Chief Architect, 2026-08-14 — CLOSED TRUSTED BETA only): the unit
     is the NODE, NOT the physical host, so a beta tenant on its OWN isolated node may co-reside with Customer
     Zero (on a DIFFERENT node) on the SAME box; the moment a SECOND tenant shares the SAME node the gate closes.
     The exception expires when STREAM 10E lands and must not survive into Public Launch.

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
    """True iff THIS execution ``TerminalNode`` currently serves EXACTLY this one account and no other tenant.

    ADR-0044 AMENDMENT (Chief Architect, 2026-08-14 — CLOSED TRUSTED BETA co-residency exception): the trust
    unit is the ``TerminalNode`` (one supervised beta tenant per isolated node), NOT the physical host. During
    the supervised beta a beta tenant MAY share a physical host (``rdp_host``) with Customer Zero, PROVIDED it
    occupies its OWN isolated node. Customer Zero stays protected: the beta can never bind to CZ's node
    (condition (6) / ``forbidden_execution_node_ids`` — checked unconditionally), and EVERY other isolation
    mechanism is unchanged (separate Windows identity, MT5 runtime, NTFS/G5 ACL, W^X, AppLocker, RemoteApp).
    This exception EXPIRES when STREAM 10E lands (``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED``) and MUST NOT survive
    into Public Launch. (Pre-amendment this aggregated occupancy across ALL node rows sharing ``rdp_host`` — the
    physical-host requirement removed by the amendment; nothing else here changed.)

    Occupancy sources on THIS node: a live legacy account via ``terminal_node``, and any hosted workspace via
    ``execution_node`` OR ``workspace_node`` (delivery). A single OTHER occupant of THIS node -> False.

    Fail-closed: a blank ``rdp_host`` (an execution node must carry a host identity to be deployable — this is
    a node-validity guard, no longer a physical-host aggregation key) or any error returns False."""
    node_id = getattr(node, "pk", None)
    if node_id is None or account_id is None:
        return False
    try:
        from django.db.models import Q

        from hosted_workspace.models import HostedMt5Workspace
        from trading.models import TradingAccount

        acct_id = int(account_id)
        if not str(getattr(node, "rdp_host", "") or "").strip():
            return False  # a valid execution node must have a host identity -> fail closed (defensive)
        # ADR-0044 amendment: single-tenancy is scoped to THIS TerminalNode, not the physical host. CZ on a
        # DIFFERENT node of the same box does NOT break this (CZ's own node is separately forbidden by (6)).
        node_ids = {node_id}
        # (a) any OTHER live legacy account pinned to THIS node.
        other_active = (TradingAccount.objects
                        .filter(terminal_node_id__in=node_ids, is_active=True)
                        .exclude(pk=acct_id)
                        .exists())
        if other_active:
            return False
        # (b) any OTHER hosted workspace occupying THIS node in EITHER role (execution or delivery).
        other_hosted = (HostedMt5Workspace.objects
                        .filter(Q(execution_node_id__in=node_ids)
                                | Q(workspace_node_id__in=node_ids))
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
