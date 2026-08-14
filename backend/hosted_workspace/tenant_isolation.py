"""hosted_workspace.tenant_isolation — host-level co-residency guard (ADR-0043 Addendum B, DARK).

Coarse-grained COMPLEMENT to the in-host W^X isolation model. The W^X model (ADR-0043, ``hosted_wx_isolation``)
isolates tenants that SHARE one physical host from each other; its behavioural certification
(``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED``) is still outstanding. Until it lands, this guard keeps every
NON-Customer-Zero tenant OFF the physical host that serves Customer Zero's live, money-bearing terminal. It
reduces the worst-case blast radius of an un-certified isolation escape from "a beta tenant reaches Customer
Zero's live account" to "a beta tenant reaches only other disposable beta tenants on a throwaway host".

Fail-closed and flag-gated: ``HOSTED_TENANT_NODE_ISOLATION_ENABLED`` (default OFF -> zero behaviour change).
When on, a NON-Customer-Zero hosted workspace may never be bound to a ``TerminalNode`` that
  (a) currently serves a Customer Zero account (derived LIVE from the DB -- no host address hardcoded), or
  (b) exposes an rdp_host listed in ``settings.HOSTED_BETA_FORBIDDEN_RDP_HOSTS``.
Customer Zero itself is unaffected -- it may occupy its own node.

"Who is Customer Zero" has ONE definition -- ``applocker_policy.RESERVED_CUSTOMER_ZERO`` -- reused here so the
account identity never diverges between the AppLocker layer and the allocation layer (security RULE 6). This
module performs NO host or broker action and holds no secret.
"""
from __future__ import annotations


class CrossTenantCoResidencyError(Exception):
    """Raised by the execution-node single writer when the co-residency guard is ON and a non-Customer-Zero
    account would be bound to a Customer-Zero (or configured-forbidden) node. A LOUD, fail-closed integrity
    stop -- never silently swallowed."""


def customer_zero_account_ids() -> frozenset:
    """Canonical Customer Zero account ids. Reuses ``applocker_policy.RESERVED_CUSTOMER_ZERO`` so the AppLocker
    layer and the allocation layer share ONE definition of Customer Zero (no divergence, security RULE 6)."""
    from hosted_workspace.applocker_policy import RESERVED_CUSTOMER_ZERO
    return RESERVED_CUSTOMER_ZERO


def is_customer_zero_account(account_id) -> bool:
    """True iff ``account_id`` is a Customer Zero account. Tolerant of str/int/None; a non-numeric id is NOT
    Customer Zero (fail toward applying the guard, never toward exempting an unknown account)."""
    try:
        return int(account_id) in customer_zero_account_ids()
    except (TypeError, ValueError):
        return False


def forbidden_execution_node_ids() -> set:
    """Node ids a NON-Customer-Zero hosted workspace must never be bound to. Union of two independent sources:
      (a) every ``TerminalNode`` a Customer Zero occupant is bound to -- derived live from the DB, so no host
          address is hardcoded. This reads BOTH the account pointer (``account.terminal_node``) AND the
          AUTHORITATIVE hosted-workspace bindings (``execution_node`` / ``workspace_node``): the two are kept
          equal only by the allocator, and ``account.terminal_node`` can be cleared independently (e.g.
          ``execution.views.unassign_account``) while the Customer Zero workspace keeps running on its node --
          so deriving from the workspace bindings too keeps the guard correct in that dangerous direction; and
      (b) every ``TerminalNode`` whose rdp_host appears in ``settings.HOSTED_BETA_FORBIDDEN_RDP_HOSTS`` -- an
          explicit belt-and-suspenders for a Customer Zero host whose account binding is not yet in the DB.
    Read-only. Returns ``set[int]`` (empty when no source yields a node)."""
    from django.conf import settings

    from execution.models import TerminalNode
    from hosted_workspace.models import HostedMt5Workspace
    from trading.models import TradingAccount

    cz = customer_zero_account_ids()
    ids = set(
        TradingAccount.objects
        .filter(pk__in=cz, terminal_node__isnull=False)
        .values_list("terminal_node_id", flat=True)
    )
    for exec_id, deliver_id in (HostedMt5Workspace.objects
                                .filter(trading_account_id__in=cz)
                                .values_list("execution_node_id", "workspace_node_id")):
        if exec_id is not None:
            ids.add(exec_id)
        if deliver_id is not None:
            ids.add(deliver_id)
    hosts = {str(h).strip() for h in getattr(settings, "HOSTED_BETA_FORBIDDEN_RDP_HOSTS", ()) if str(h).strip()}
    if hosts:
        # Case-INSENSITIVE match: rdp_host holds hostnames or IPs, and a config/stored case mismatch must never
        # silently drop a forbidden host from the belt. ``__in`` is case-sensitive on Postgres, so OR iexact.
        from django.db.models import Q
        q = Q()
        for h in hosts:
            q |= Q(rdp_host__iexact=h)
        ids |= set(TerminalNode.objects.filter(q).values_list("id", flat=True))
    return ids


def assert_allocation_allowed(account_id, node) -> None:
    """Fail-closed co-residency guard for the execution-node single writer. No-op when the flag is OFF (zero
    behaviour change) or the account IS Customer Zero. Otherwise raises ``CrossTenantCoResidencyError`` if
    ``node`` is a Customer-Zero / configured-forbidden node. Side-effect-free apart from the raise; the query
    it runs is read-only, so it is safe to call before the single writer's mutation."""
    from hosted_workspace.flags import hosted_tenant_node_isolation_enabled

    if not hosted_tenant_node_isolation_enabled():
        return
    if is_customer_zero_account(account_id):
        return
    node_id = getattr(node, "pk", None)
    if node_id is not None and node_id in forbidden_execution_node_ids():
        raise CrossTenantCoResidencyError(
            "cross_tenant_co_residency_forbidden: account %s may not be bound to Customer Zero node %s"
            % (account_id, node_id)
        )
