"""hosted_workspace.provisioning — ADR-0034 Onboarding provisioning orchestrator (DARK, repository-side).

The authoritative REPOSITORY-side driver of the customer Hosted Workspace journey:

    request → intent-only account → idempotent workspace → node allocation (WAITING_FOR_LOGIN)
    … customer logs into their OWN MT5 … agent observes → discovery → customer CONFIRMS.

It creates durable intent and advances canonical state ONLY through the certified single writer
(``persistence.persist_workspace_decision``). It performs NO host or broker action: it never SSHes, never
creates a Windows user, never installs RDS, never launches MT5, and — the load-bearing product invariant —
never receives or stores a broker PASSWORD. The customer supplies only broker IDENTIFIERS (expected login /
server); confirmation is gated on an OBSERVED active-account match. Every entry point is admission-gated,
owner-scoped, fail-closed, and idempotent. Nothing here arms execution (``execution_enabled``) or authorises
an order — the journey stops at assignment-eligibility, strictly below arming and the live order-time gate.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from hosted_workspace.entitlement import hosted_workspace_admission
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.telemetry import WorkspaceEvent, emit_workspace_event

# Stable, secret-free reason codes (in addition to the admission codes in entitlement.py).
REQ_LOGIN_REQUIRED = "expected_login_required"
REQ_PASSWORD_FORBIDDEN = "broker_password_forbidden"
REQ_CREATED = "created"
REQ_EXISTS = "exists"
ALLOC_NO_CAPACITY = "no_node_capacity"
ALLOC_NODE_NOT_DELIVERABLE = "node_not_deliverable"   # G12: node has capacity but no durable rdp_host
ALLOC_CZ_NODE_FORBIDDEN = "cz_node_forbidden"         # ADR-0043 Addendum B: refuse a non-CZ tenant on a CZ node
ALLOC_ALREADY = "already_bound"
ALLOC_OK = "allocated"
CONFIRM_NOT_OWNER = "not_owner"
CONFIRM_NO_MATCH = "no_discovered_match"
CONFIRM_OK = "confirmed"
CONFIRM_ALREADY = "already_confirmed"


@dataclass(frozen=True)
class RequestResult:
    ok: bool
    reason: str
    workspace: object = None
    created: bool = False


@dataclass(frozen=True)
class AllocResult:
    ok: bool
    reason: str
    node_hostname: str = ""


@dataclass(frozen=True)
class ConfirmResult:
    ok: bool
    reason: str


def _mask(login: str) -> str:
    login = str(login or "")
    return ("***" + login[-3:]) if login else ""


def _node_has_capacity(node) -> bool:
    """True iff ``node`` can take one more occupant. Occupancy = live legacy accounts
    (``computed_active_accounts``, ``is_active=True``) PLUS bound Hosted Workspaces — which are held on
    INACTIVE intent accounts (``is_active=False``) and are therefore invisible to the legacy metric. Counting
    only the legacy metric fail-OPENs (a hosted binding never increments it, so a node over-fills and every
    customer piles onto node 1). The hosted term filters to ``is_active=False`` so a hosted account that is
    also live is not double-counted. Must be called with the node row LOCKED so the count-then-bind can't
    over-commit under concurrency."""
    active = node.computed_active_accounts
    hosted = node.bound_hosted_workspaces.filter(trading_account__is_active=False).count()
    return (active + hosted) < node.max_accounts


def _node_deliverable(node) -> bool:
    """G12 — a node is DELIVERABLE only when it carries a durable ``rdp_host`` (the transport identity the
    RemoteApp descriptor is minted from; distinct from ``hostname``). Binding a workspace to a node without
    one would leave the ``workspace → node → rdp_host → RemoteApp`` chain incomplete and force a manual repair
    before customer delivery, so allocation fails closed here and the provisioning driver simply retries once
    the operator has set ``rdp_host``. Pre-existing bindings are never regressed (checked only for NEW binds)."""
    return bool(str(getattr(node, "rdp_host", "") or "").strip())


def request_hosted_workspace(user, *, expected_login, expected_server="", broker_name="", is_demo=True,
                             actor="", request=None) -> RequestResult:
    """Customer requests a Hosted Workspace. Admission-gated (fail-closed). IDEMPOTENT: one workspace per
    eligible user — a re-request returns the existing one (never a second). Creates an INTENT-ONLY
    ``TradingAccount`` (``is_active=False``, no ``mt5_instance``, ``readiness_provider=persistent_workspace``)
    carrying only the broker IDENTIFIERS the customer already knows (expected login/server) — NEVER a
    password. Serialised on the user row so concurrent requests cannot make two workspaces."""
    ok, reason = hosted_workspace_admission(user)
    if not ok:
        return RequestResult(False, reason)
    login = str(expected_login or "").strip()
    if not login:
        return RequestResult(False, REQ_LOGIN_REQUIRED)

    from django.contrib.auth import get_user_model
    from execution.readiness import PERSISTENT_WORKSPACE
    from trading.models import BrokerServer, TradingAccount

    with transaction.atomic():
        # Serialise this user's requests so a duplicate/concurrent request cannot create two workspaces.
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        existing = (HostedMt5Workspace.objects.filter(trading_account__user=locked_user)
                    .select_related("trading_account").first())   # ownership = trading_account.user
        if existing is not None:
            return RequestResult(True, REQ_EXISTS, existing, False)

        server = None
        srv_name = str(expected_server or "").strip()
        if srv_name:
            server, _ = BrokerServer.objects.get_or_create(server_name=srv_name)
        # ``brokeridentity_present`` requires a broker_server OR a non-empty broker_name. The customer may
        # supply neither identifier at request time, so default broker_name to a safe placeholder (display
        # only — the certified matcher keys on broker_server.server_name, never on this string).
        bname = str(broker_name or "").strip() or "Hosted Workspace"
        account = TradingAccount.objects.create(
            user=locked_user, name=bname, broker_name=bname,
            account_number=login, broker_server=server, is_demo=bool(is_demo), is_active=False,
            readiness_provider=PERSISTENT_WORKSPACE)
        # Ownership is carried by the account's ``user`` (immutable OneToOne binding) — no separate owner FK.
        workspace = HostedMt5Workspace.objects.create(trading_account=account)

    _audit(request, "HOSTED_WORKSPACE_REQUESTED", account, actor)
    emit_workspace_event(WorkspaceEvent.REQUESTED, workspace_uuid=workspace.workspace_uuid, account=account,
                         summary="hosted workspace requested",
                         detail={"expected_login_masked": _mask(login), "expected_server": srv_name},
                         source="hosted_workspace.onboarding")
    return RequestResult(True, REQ_CREATED, workspace, True)


def allocate_workspace_node(workspace, *, actor="", request=None) -> AllocResult:
    """Durably bind the workspace + its account to ONE authorised, ACTIVE ``TerminalNode`` with capacity, then
    advance ``PROVISIONING → WAITING_FOR_LOGIN`` via the certified single writer. Capacity is reserved
    ATOMICALLY (``select_for_update`` over candidate nodes) so the check-then-write can't overfill a node.
    Fail-closed when no node has capacity. Idempotent: an already-bound workspace is returned unchanged. Keeps
    ``account.terminal_node == workspace.execution_node`` (the ``resolve_hosted_route`` agreement invariant).
    In the single-host persistent-workspace model the customer's ONE host serves BOTH order execution and
    RemoteApp delivery, so allocation also assigns ``workspace_node`` (the delivery host, via the delivery
    single writer) to the SAME node — as a SECOND, EXPLICIT authority assignment, NOT a fallback: execution
    reads only ``execution_node``, delivery only ``workspace_node``, and neither reads the other (ADR-0034 §9).
    Performs NO host action — it only records the durable bindings + drives canonical state."""
    from execution.hosted_provisioning import assign_workspace_execution_node
    from execution.models import TerminalNode

    from hosted_workspace.delivery_persistence import assign_workspace_node as assign_delivery_node

    with transaction.atomic():
        ws = (HostedMt5Workspace.objects.select_for_update()
              .select_related("trading_account").get(pk=workspace.pk))
        already_bound = ws.execution_node_id is not None
        # ADR-0043 Addendum B (DARK): host-level co-residency guard. A NON-Customer-Zero workspace must never be
        # bound to a node that serves Customer Zero. OFF by default → ``_forbidden`` is empty → zero behaviour
        # change. Computed once under the lock (read-only query). The authoritative fail-closed enforcement is
        # in the single writer (``assign_workspace_execution_node``); here we skip forbidden candidates and
        # surface a DISTINCT reason so the driver/operator learns "provision a non-CZ host" rather than a
        # generic "no capacity". The single writer's raise is therefore never triggered from THIS path.
        from hosted_workspace.flags import hosted_tenant_node_isolation_enabled
        from hosted_workspace.tenant_isolation import forbidden_execution_node_ids, is_customer_zero_account
        _guard_on = hosted_tenant_node_isolation_enabled() and not is_customer_zero_account(ws.trading_account_id)
        _forbidden = forbidden_execution_node_ids() if _guard_on else set()
        # Whether we still need to drive PROVISIONING → WAITING_FOR_LOGIN. Guarded on canonical == PROVISIONING
        # so a RETRY converges a workspace left stuck at PROVISIONING (advance failed after a prior bind) yet
        # NEVER regresses a workspace that has already progressed (e.g. CONNECTED) back toward login.
        needs_advance = str(ws.canonical_state) == S.PROVISIONING
        if already_bound:
            if _forbidden and ws.execution_node_id in _forbidden:
                # A non-CZ workspace already sits on a Customer Zero node — a pre-existing co-residency
                # violation. Surface it fail-closed rather than silently reporting "already_bound".
                return AllocResult(False, ALLOC_CZ_NODE_FORBIDDEN)
            node = TerminalNode.objects.filter(pk=ws.execution_node_id).first()
            hostname = node.hostname if node else ""
            reason = ALLOC_ALREADY
        else:
            candidate = None
            capacity_but_undeliverable = False   # G12: distinguish "no room" from "room but no rdp_host"
            forbidden_blocked = False            # a viable node was refused SOLELY for CZ co-residency
            for node in (TerminalNode.objects.select_for_update()
                         .filter(status=TerminalNode.Status.ACTIVE).order_by("id")):
                if node.pk in _forbidden:          # Customer Zero node — never for a non-CZ tenant
                    forbidden_blocked = True
                    continue
                if not _node_has_capacity(node):   # counts hosted bindings, not just is_active legacy accounts
                    continue
                if not _node_deliverable(node):    # G12: no durable rdp_host → not deliverable, fail closed
                    capacity_but_undeliverable = True
                    continue
                candidate = node
                break
            if candidate is None:
                # Fail closed. Prefer the CZ-forbidden reason when the ONLY blocker was a Customer Zero node
                # (operator action = "provision a separate non-CZ host"), distinct from "buy capacity" /
                # "set rdp_host" (G12).
                if forbidden_blocked and not capacity_but_undeliverable:
                    return AllocResult(False, ALLOC_CZ_NODE_FORBIDDEN)
                return AllocResult(False,
                                   ALLOC_NODE_NOT_DELIVERABLE if capacity_but_undeliverable else ALLOC_NO_CAPACITY)
            if _forbidden and candidate.pk in _forbidden:
                # Defence in depth: the loop already skips forbidden nodes, so this is unreachable via normal
                # flow. Fail closed rather than bind if a logic/TOCTOU error ever selected a Customer Zero node.
                return AllocResult(False, ALLOC_CZ_NODE_FORBIDDEN)
            acct = ws.trading_account
            if acct.terminal_node_id != candidate.pk:
                acct.terminal_node = candidate
                acct.save(update_fields=["terminal_node"])
            assign_workspace_execution_node(acct, candidate, actor=actor, request=request)
            assign_delivery_node(ws, candidate)   # delivery host = same node (explicit 2nd authority, not a fallback)
            hostname, reason = candidate.hostname, ALLOC_OK

    if needs_advance:
        # Stream 4 GATE: when the host-provisioning engine is armed, advance PROVISIONING → WAITING_FOR_LOGIN
        # ONLY once the customer's Windows slot actually exists on the host (identity+folders+ACL+runtime+RDP+
        # RemoteApp+AppLocker prep). DARK by default: while HOSTED_SLOT_PREP_ENABLED is off this branch is not
        # taken and allocation advances exactly as before (zero behaviour change / no regression). Armed but
        # with the dark host-executor, prepare fails closed (host_executor_unavailable) and we do NOT advance —
        # the workspace stays at PROVISIONING and is re-driven idempotently next cycle. See ADR-0036 (Stream 4).
        from hosted_workspace.flags import hosted_slot_prep_enabled
        if hosted_slot_prep_enabled():
            from hosted_workspace.slot_preparation import prepare_hosted_slot
            prep = prepare_hosted_slot(workspace, actor=actor, request=request)
            if not prep.prepared:
                return AllocResult(False, prep.reason, hostname)
        _advance_to_awaiting_login(workspace)
    return AllocResult(True, reason, hostname)


def _advance_to_awaiting_login(workspace) -> None:
    """Drive ``PROVISIONING → WAITING_FOR_LOGIN`` through the certified single writer by feeding a synthetic
    'provisioned, not-yet-connected' observation (``_target_for`` maps previous=PROVISIONING + not-connected
    → WAITING_FOR_LOGIN). No second writer, no host action; idempotent (already-WAITING_FOR_LOGIN holds)."""
    from hosted_workspace.manager import WorkspaceObservation, derive_workspace_decision
    from hosted_workspace.persistence import persist_workspace_decision
    ws = HostedMt5Workspace.objects.get(pk=workspace.pk)
    obs = WorkspaceObservation(
        process_running=False, ipc_available=False, connected=False, account_match=False,
        trade_allowed=False, fresh=False, previous_state=str(ws.canonical_state),
        previous_reason=str(ws.canonical_reason))
    decision = derive_workspace_decision(obs)
    persist_workspace_decision(
        ws, obs, decision, observation_version=int(ws.observation_version or 0) + 1,
        correlation_id="", source="hosted_workspace.onboarding")


def confirm_broker_account(user, workspace, *, actor="", request=None) -> ConfirmResult:
    """Customer ACK 'yes, this is my broker account'. Owner-scoped + gated on a POSITIVE observed active-
    account match (the certified matcher's result, cached in ``proj_account_match``, on a CONNECTED/ready
    workspace). Stamps ``TradingAccount.workspace_confirmed_at`` (the durable ACK). Idempotent; NEVER accepts
    a password; the ACK is NOT execution authority — the live bridge gate remains the order authority."""
    ok, reason = hosted_workspace_admission(user)
    if not ok:
        return ConfirmResult(False, reason)

    from django.utils import timezone

    with transaction.atomic():
        ws = (HostedMt5Workspace.objects.select_for_update()
              .select_related("trading_account").get(pk=workspace.pk))
        if ws.trading_account.user_id != getattr(user, "pk", None):
            return ConfirmResult(False, CONFIRM_NOT_OWNER)          # owner-scoped (trading_account.user), IDOR-safe
        acct = ws.trading_account
        if acct.workspace_confirmed_at is not None:
            return ConfirmResult(True, CONFIRM_ALREADY)            # idempotent
        # A confirmable account requires a real OBSERVED positive match on a connected workspace — never a
        # password, never a client-asserted identity.
        if (str(ws.canonical_state) not in (S.CONNECTED, S.EXECUTION_READY)
                or ws.proj_account_match is not True):
            return ConfirmResult(False, CONFIRM_NO_MATCH)
        acct.workspace_confirmed_at = timezone.now()
        acct.save(update_fields=["workspace_confirmed_at"])

    _audit(request, "HOSTED_WORKSPACE_ACCOUNT_CONFIRMED", acct, actor)
    emit_workspace_event(WorkspaceEvent.ACCOUNT_CONFIRMED, workspace_uuid=ws.workspace_uuid, account=acct,
                         summary="broker account confirmed by customer",
                         source="hosted_workspace.onboarding")
    return ConfirmResult(True, CONFIRM_OK)


def _audit(request, event_type, account, actor) -> None:
    """Best-effort, non-secret audit (fail-open — never blocks the onboarding action)."""
    try:
        from core.audit import log_event
        log_event(request, event_type, severity="INFO", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata={"actor": str(actor or "")})
    except Exception:  # noqa: BLE001
        pass
