"""P0-B1 — per-tenant order-execution endpoint lifecycle + deterministic host-local port allocation.

This is the SERVER-SIDE authority that makes hosted execution repeatable for Customer N without one physical
server per customer: each hosted workspace is given its OWN ``HostedExecutionEndpoint`` (a unique host:port
that a dedicated pin-enforcing bridge process listens on), so many tenants share one execution host/node while
each keeps an isolated bridge → terminal → broker session.

Every routing/identity value is SERVER-DERIVED (windows_username from ``AccountProvisioning``, workspace_uuid
from ``HostedMt5Workspace``, login/server/is_demo from the account). Nothing here contacts the host or places
an order; it only records the durable authority the (separately supervised) bridge is configured from and the
order transport routes to. DARK: the endpoint is only READ when ``HOSTED_PER_TENANT_TRANSPORT_ENABLED`` is on.

Port allocation is deterministic, bounded, collision-safe (unique host:port among non-retired endpoints,
enforced in the DB under a row-locking transaction), restart-safe (durable in the DB — reboot reconstruction
reads the same rows), and reclaimable (a RETIRED endpoint frees its port for reuse).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("guvfx.execution.endpoint")

# ── bounded host-local port range for per-tenant bridges ──
# Deliberately DISJOINT from the reserved GuvFX ports so a per-tenant bridge never collides with them:
#   8787 backtest Windows agent · 8788 Customer Zero LEGACY order bridge · 8789 Closed-Beta node bridge
#   (support@) · 8791 beta validation agent. The range is far larger than any certified beta capacity.
PORT_RANGE_START = 8800
PORT_RANGE_END = 8899
RESERVED_PORTS = frozenset({8787, 8788, 8789, 8791})

# Host runtime layout (the established portable-MT5 convention; overridable for non-standard hosts).
ACCOUNTS_ROOT = getattr(settings, "HOSTED_ACCOUNTS_ROOT", r"C:\GuvFX\accounts")

# ── stable, secret-free lifecycle reason codes ──
EP_ALLOCATED = "endpoint_allocated"
EP_REACTIVATED = "endpoint_reactivated"
EP_READY = "endpoint_ready"
EP_RETIRED = "endpoint_retired"
EP_NO_NODE = "endpoint_no_execution_node"
EP_NODE_NO_HOST = "endpoint_node_no_rdp_host"
EP_NO_WINDOWS_IDENTITY = "endpoint_no_windows_username"
EP_NO_WORKSPACE_UUID = "endpoint_no_workspace_uuid"
EP_PORT_EXHAUSTED = "endpoint_port_range_exhausted"


class EndpointError(Exception):
    """Raised for a fail-closed allocation/lifecycle condition. Carries a stable secret-free ``reason``."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class EndpointResult:
    ok: bool
    reason: str
    endpoint_id: int | None = None
    base_url: str = ""
    port: int | None = None


def runtime_terminal_path(account_id) -> str:
    """The authoritative portable-MT5 terminal path for an account (matches the host layout the bridge pins
    to, e.g. ``C:\\GuvFX\\accounts\\<id>\\terminal\\terminal64.exe``). Server-derived; never client input."""
    root = str(ACCOUNTS_ROOT).rstrip("\\/")
    return f"{root}\\{account_id}\\terminal\\terminal64.exe"


def _base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def allocate_port(host: str, *, exclude: frozenset[int] = frozenset()) -> int:
    """Lowest free port in [PORT_RANGE_START, PORT_RANGE_END] for ``host`` — excluding reserved ports, ports
    held by a LIVE (non-retired) endpoint on that host, and any caller-supplied ``exclude``. Deterministic
    (lowest-first) so allocation is reproducible. MUST be called inside the allocating transaction with the
    candidate rows locked so two concurrent provisionings cannot pick the same port (the DB unique constraint
    is the final backstop). Raises ``EndpointError(EP_PORT_EXHAUSTED)`` if the range is full."""
    from execution.models import HostedExecutionEndpoint

    taken = set(
        HostedExecutionEndpoint.objects
        .exclude(state=HostedExecutionEndpoint.State.RETIRED)
        .filter(host=host)
        .values_list("port", flat=True)
    )
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port in RESERVED_PORTS or port in exclude or port in taken:
            continue
        return port
    raise EndpointError(EP_PORT_EXHAUSTED, f"host={host}")


def _derive_identity(account, workspace) -> dict:
    """Gather the SERVER-DERIVED identity the endpoint (and its bridge) is bound to. No client input.

    Reads the windows_username from the authoritative isolation system-of-record
    (``terminal_provisioning.AccountProvisioning``, OneToOne + UNIQUE windows_username) DIRECTLY — an
    allocation-time provisioning concern that must NOT depend on the runtime transport flag being on (unlike
    the order-path helper ``hosted_pin.hosted_windows_username_for``, which is flag-gated by design). Still
    fail-closed: only a PROVISIONED profile yields an identity; a PENDING/DISABLED/RETIRED profile or none
    refuses allocation."""
    from terminal_provisioning.models import AccountProvisioning

    prof = (AccountProvisioning.objects
            .filter(trading_account=account, status=AccountProvisioning.Status.PROVISIONED).first())
    windows_username = str(getattr(prof, "windows_username", "") or "").strip() if prof else ""
    if not windows_username:
        raise EndpointError(EP_NO_WINDOWS_IDENTITY, f"account={getattr(account, 'id', None)}")
    workspace_uuid = str(getattr(workspace, "workspace_uuid", "") or "").strip()
    if not workspace_uuid:
        raise EndpointError(EP_NO_WORKSPACE_UUID, f"account={getattr(account, 'id', None)}")
    return {
        "windows_username": windows_username,
        "workspace_uuid": workspace_uuid,
        "runtime_path": runtime_terminal_path(account.id),
        "expected_login": str(getattr(account, "account_number", "") or "").strip(),
        "expected_server": str(getattr(getattr(account, "broker_server", None), "name", "")
                               or getattr(account, "broker_server_name", "") or "").strip(),
        "is_demo": bool(getattr(account, "is_demo", True)),
    }


@transaction.atomic
def allocate_endpoint(workspace, *, actor: str = "endpoint_service", explicit_port: int | None = None,
                      explicit_base_url: str = "") -> EndpointResult:
    """Allocate (or reactivate) the per-tenant endpoint for ``workspace``. Idempotent: a live endpoint is
    returned unchanged; a RETIRED one is reactivated in place (reusing its row). ``explicit_port`` /
    ``explicit_base_url`` seed an EXISTING bridge (e.g. support@'s :8789) without allocating a new port — so
    turning the flag on never re-homes support@. Fail-closed on any missing authoritative binding.

    Leaves the endpoint ALLOCATED (NOT routable) — a bridge must be stood up and health/pin-certified via
    ``mark_ready`` before any order can reach it."""
    from execution.models import HostedExecutionEndpoint

    account = workspace.trading_account
    node = getattr(workspace, "execution_node", None) or getattr(workspace, "workspace_node", None)
    if node is None:
        raise EndpointError(EP_NO_NODE, f"workspace={workspace.pk}")
    host = str(getattr(node, "rdp_host", "") or "").strip()
    if not host:
        raise EndpointError(EP_NODE_NO_HOST, f"node={node.pk}")

    identity = _derive_identity(account, workspace)

    # Lock any existing row for this workspace so concurrent callers converge on one endpoint.
    existing = (HostedExecutionEndpoint.objects.select_for_update()
                .filter(workspace=workspace).first())

    if explicit_port is not None:
        port = int(explicit_port)
        base = _clean_url(explicit_base_url) or _base_url(host, port)
    elif existing is not None and existing.state != HostedExecutionEndpoint.State.RETIRED:
        # Live endpoint already present — keep its port/url (idempotent).
        port, base = existing.port, existing.base_url
    else:
        # Lock live endpoints on this host so the port scan + insert cannot race another provisioning.
        list(HostedExecutionEndpoint.objects.select_for_update()
             .exclude(state=HostedExecutionEndpoint.State.RETIRED).filter(host=host).values_list("id", flat=True))
        port = allocate_port(host)
        base = _base_url(host, port)

    fields = dict(
        trading_account=account, terminal_node=node, host=host, port=port, base_url=base,
        state=HostedExecutionEndpoint.State.ALLOCATED, retired_at=None, **identity,
    )
    if existing is None:
        ep = HostedExecutionEndpoint.objects.create(workspace=workspace, last_reason=EP_ALLOCATED, **fields)
        reason = EP_ALLOCATED
    else:
        was_retired = existing.state == HostedExecutionEndpoint.State.RETIRED
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.last_reason = EP_REACTIVATED if was_retired else EP_ALLOCATED
        existing.save()
        ep = existing
        reason = existing.last_reason

    logger.info("endpoint %s account=%s ws=%s %s port=%s actor=%s",
                reason, account.id, workspace.pk, ep.base_url, port, actor)
    return EndpointResult(True, reason, endpoint_id=ep.pk, base_url=ep.base_url, port=port)


@transaction.atomic
def mark_ready(workspace, *, health_ok: bool, actor: str = "endpoint_service") -> EndpointResult:
    """Transition ALLOCATED → READY once the tenant's bridge is proven up + pin-certified. Only a READY
    endpoint is routable. A non-ok health leaves it ALLOCATED (fail-closed: never routes an unproven bridge)."""
    from execution.models import HostedExecutionEndpoint

    ep = (HostedExecutionEndpoint.objects.select_for_update()
          .filter(workspace=workspace).exclude(state=HostedExecutionEndpoint.State.RETIRED).first())
    if ep is None:
        raise EndpointError(EP_NO_NODE, f"no live endpoint for workspace={workspace.pk}")
    ep.last_health_ok = bool(health_ok)
    ep.last_health_at = timezone.now()
    if health_ok:
        ep.state = HostedExecutionEndpoint.State.READY
        ep.activated_at = ep.activated_at or timezone.now()
        ep.last_reason = EP_READY
    ep.save()
    logger.info("endpoint mark_ready account=%s ws=%s health_ok=%s state=%s actor=%s",
                ep.trading_account_id, workspace.pk, health_ok, ep.state, actor)
    return EndpointResult(True, ep.last_reason, endpoint_id=ep.pk, base_url=ep.base_url, port=ep.port)


@transaction.atomic
def retire_endpoint(workspace, *, actor: str = "endpoint_service") -> EndpointResult:
    """Retire the workspace's endpoint on deprovision: mark RETIRED (never routable again) and free its port
    for reuse. Idempotent — no live endpoint is a no-op."""
    from execution.models import HostedExecutionEndpoint

    ep = (HostedExecutionEndpoint.objects.select_for_update()
          .filter(workspace=workspace).exclude(state=HostedExecutionEndpoint.State.RETIRED).first())
    if ep is None:
        return EndpointResult(True, EP_RETIRED, endpoint_id=None)
    ep.state = HostedExecutionEndpoint.State.RETIRED
    ep.retired_at = timezone.now()
    ep.last_reason = EP_RETIRED
    ep.save()
    logger.info("endpoint retired account=%s ws=%s port=%s actor=%s",
                ep.trading_account_id, workspace.pk, ep.port, actor)
    return EndpointResult(True, EP_RETIRED, endpoint_id=ep.pk, base_url=ep.base_url, port=ep.port)


def _clean_url(url) -> str:
    return str(url or "").strip().rstrip("/")
