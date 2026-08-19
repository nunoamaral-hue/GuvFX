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

from django.db import IntegrityError, transaction

from hosted_workspace.entitlement import hosted_workspace_admission
from hosted_workspace.flags import hosted_deferred_identity_bind_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning_timing import (
    STAGE_NODE_ALLOCATED,
    STAGE_REQUEST_RECEIVED,
    STAGE_WAITING_FOR_LOGIN,
    record_stage_timing,
)
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.telemetry import WorkspaceEvent, emit_workspace_event

# Stable, secret-free reason codes (in addition to the admission codes in entitlement.py).
REQ_LOGIN_REQUIRED = "expected_login_required"
REQ_IDENTITY_INVALID = "expected_identity_invalid"   # login/server too long or otherwise unstorable → clean 400
REQ_PASSWORD_FORBIDDEN = "broker_password_forbidden"
REQ_CREATED = "created"
REQ_EXISTS = "exists"
ALLOC_NO_CAPACITY = "no_node_capacity"
ALLOC_NODE_NOT_DELIVERABLE = "node_not_deliverable"   # G12: node has capacity but no durable rdp_host
ALLOC_CZ_NODE_FORBIDDEN = "cz_node_forbidden"         # ADR-0043 Addendum B: refuse a non-CZ tenant on a CZ node
ALLOC_NODE_NOT_EXECUTION_OPERATIONAL = "node_not_execution_operational"  # ADR-0048: no execution-commissioned node
ALLOC_ALREADY = "already_bound"
ALLOC_OK = "allocated"
CONFIRM_NOT_OWNER = "not_owner"
CONFIRM_NO_MATCH = "no_discovered_match"
CONFIRM_OK = "confirmed"
CONFIRM_ALREADY = "already_confirmed"
# ADR-0047: explicit customer authorization to execute ("Enable automated trading").
AUTHZ_NOT_OWNER = "not_owner"
AUTHZ_NOT_CONFIRMED = "account_not_confirmed"          # must have confirmed identity first
AUTHZ_NOT_READY = "workspace_not_execution_ready"      # authorize only a connected+matched, EXECUTION_READY ws
AUTHZ_OK = "authorized"
AUTHZ_ALREADY = "already_authorized"
# Deferred broker-identity binding (Beta UX Correction, Sponsor 2026-08-15).
BIND_OK = "bound"
BIND_IDEMPOTENT = "already_bound_identical"
BIND_NOT_OWNER = "not_owner"
BIND_LOGIN_REQUIRED = "expected_login_required"
BIND_NOT_HOSTED = "not_hosted_workspace"
BIND_LIVE_FORBIDDEN = "live_identity_forbidden"          # Closed Beta is DEMO-only
BIND_WRONG_STATE = "identity_bind_not_allowed_in_state"
BIND_ALREADY = "identity_already_bound"                  # write-once: a DIFFERENT second bind fails closed
BIND_IDENTITY_INVALID = "expected_identity_invalid"      # too long / duplicate / unstorable → clean 400, not 500

# Field length ceilings (mirror TradingAccount.account_number / BrokerServer.server_name) — validated BEFORE the
# DB write so an over-long identifier is a clean 4xx, never an uncaught Postgres DataError (HTTP 500).
_MAX_LOGIN_LEN = 64
_MAX_SERVER_LEN = 160


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


@dataclass(frozen=True)
class BindResult:
    ok: bool
    reason: str


@dataclass(frozen=True)
class AuthorizeResult:
    ok: bool
    reason: str
    arm_reason: str = ""   # the arm outcome (armed / a precondition reason) once authorization is recorded


def _mask(login: str) -> str:
    login = str(login or "")
    return ("***" + login[-3:]) if login else ""


def _node_has_capacity(node) -> bool:
    """True iff ``node`` can take one more occupant. Occupancy = the count of DISTINCT occupant ACCOUNTS across
    BOTH binding sources: a live legacy account via ``terminal_node`` (``is_active=True``), and a Hosted
    Workspace via ``execution_node`` (regardless of the intent account's ``is_active``). Counting a UNION of
    account ids (not the sum of two separately-filtered counts) is robust to (a) an activated hosted account —
    ``is_active=True`` after confirmation (ADR-0044) — which the old ``is_active=False`` hosted filter would
    have dropped, and (b) the ``terminal_node``/``execution_node`` desync (e.g. ``unassign_account`` clears
    ``terminal_node`` while the workspace keeps ``execution_node``), which would otherwise make the account
    escape BOTH terms and let the allocator over-fill the node. Must be called with the node row LOCKED so the
    count-then-bind can't over-commit under concurrency."""
    return node_occupant_count(node) < node.max_accounts


def node_occupant_count(node) -> int:
    """The number of DISTINCT occupant ACCOUNTS on ``node`` — the single authoritative occupancy count
    used both by the allocator (``_node_has_capacity``) and by the operational capacity warning, so the
    two can never disagree. Occupancy = the UNION of account ids across BOTH binding sources: a live
    legacy account via ``terminal_node`` (``is_active=True``) and a Hosted Workspace via
    ``execution_node`` (regardless of the intent account's ``is_active``). See ``_node_has_capacity`` for
    why the union (not the sum of two filtered counts) is the robust definition."""
    from trading.models import TradingAccount
    occupants = set(
        TradingAccount.objects.filter(terminal_node_id=node.pk, is_active=True).values_list("id", flat=True)
    )
    occupants |= set(node.bound_hosted_workspaces.values_list("trading_account_id", flat=True))
    return len(occupants)


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
    # DEFERRED IDENTITY BIND (Beta UX Correction): when the flag is ON the customer may request the workspace
    # WITHOUT declaring a broker login/server up front — the intent account is created with an empty identity
    # and provisioning stays broker-identity agnostic; the identity is declared later via bind_broker_identity.
    # While the flag is OFF this is byte-identical to before (a login is mandatory at request).
    if not login and not hosted_deferred_identity_bind_enabled():
        return RequestResult(False, REQ_LOGIN_REQUIRED)
    # Bound-length validation BEFORE the DB write so an over-long identifier is a clean 400, not a 500.
    if len(login) > _MAX_LOGIN_LEN or len(str(expected_server or "").strip()) > _MAX_SERVER_LEN:
        return RequestResult(False, REQ_IDENTITY_INVALID)

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
    record_stage_timing(workspace, STAGE_REQUEST_RECEIVED)   # UX timing (fail-open)
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
        from hosted_workspace.flags import (hosted_execution_path_gate_enabled,
                                             hosted_tenant_node_isolation_enabled)
        from hosted_workspace.tenant_isolation import forbidden_execution_node_ids, is_customer_zero_account
        _guard_on = hosted_tenant_node_isolation_enabled() and not is_customer_zero_account(ws.trading_account_id)
        _forbidden = forbidden_execution_node_ids() if _guard_on else set()
        # ADR-0048 (DARK, default OFF): when ON, a hosted automated-execution account may be allocated ONLY to
        # an execution-COMMISSIONED node (its bridge + a dedicated node-aware order worker are proven), so a
        # future beta customer can never land on a node that cannot claim its orders. OFF ⇒ zero behaviour
        # change (the current journey commissions the node's worker after allocation; the read model stays
        # honest via execution_path_state). Customer Zero uses the legacy path and is exempt.
        _exec_gate_on = (hosted_execution_path_gate_enabled()
                         and not is_customer_zero_account(ws.trading_account_id))
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
            exec_not_operational = False         # ADR-0048: a viable node lacks a commissioned execution path
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
                if _exec_gate_on:                  # ADR-0048: require a commissioned execution path
                    from execution.node_execution import node_execution_operational
                    if not node_execution_operational(node).operational:
                        exec_not_operational = True
                        continue
                candidate = node
                break
            if candidate is None:
                # Fail closed. Prefer the CZ-forbidden reason when the ONLY blocker was a Customer Zero node
                # (operator action = "provision a separate non-CZ host"), distinct from "buy capacity" /
                # "set rdp_host" (G12) / "commission the node's execution path" (ADR-0048).
                if forbidden_blocked and not capacity_but_undeliverable and not exec_not_operational:
                    return AllocResult(False, ALLOC_CZ_NODE_FORBIDDEN)
                if exec_not_operational and not capacity_but_undeliverable:
                    return AllocResult(False, ALLOC_NODE_NOT_EXECUTION_OPERATIONAL)
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

    if reason == ALLOC_OK:
        record_stage_timing(workspace, STAGE_NODE_ALLOCATED)   # UX timing (fail-open); only on a NEW bind

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
        record_stage_timing(workspace, STAGE_WAITING_FOR_LOGIN)   # UX timing (fail-open)
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
    workspace). Stamps ``TradingAccount.workspace_confirmed_at`` (the durable ACK) and ACTIVATES the account
    (``is_active=True``) — the customer-specific activation the autonomous journey must perform (ADR-0044
    Decision 2): the Provider-B readiness gate and the arm preconditions both require ``is_active``, and the
    intent account was created ``is_active=False`` (provisioning), so without activating here a confirmed,
    connected, matched hosted account could never become execution-ready. Confirmation is the right point: it
    is the human ACK on an already CONNECTED + matched workspace, and the node-occupancy metric explicitly
    anticipates a live hosted account (no double-count). Idempotent; NEVER accepts a password; the ACK is NOT
    execution authority — the live bridge gate remains the order authority, and arming stays a separate step."""
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
        # Activate the account atomically with the ACK (was is_active=False as an intent account). This is a
        # customer-specific step of the autonomous journey; it is NOT arming (execution_enabled stays False).
        fields = ["workspace_confirmed_at"]
        # P0 DATA-ISOLATION (cutover contract): confirmation is the server-derived, immutable milestone at
        # which a hosted customer's OWN GuvFX-executed history begins. Stamp the ingest cutover here (once,
        # never overwriting an operator-set value) so pre-customer broker history — e.g. a reused broker
        # login's prior life, or any deal predating this customer — is NEVER auto-imported as GuvFX history.
        # Deliberate historical broker import, if ever offered, is a separate, explicitly-classified path.
        if getattr(acct, "ingest_cutover_time", None) is None:
            acct.ingest_cutover_time = acct.workspace_confirmed_at
            fields.append("ingest_cutover_time")
        if acct.is_active is not True:
            acct.is_active = True
            fields.append("is_active")
        acct.save(update_fields=fields)

    _audit(request, "HOSTED_WORKSPACE_ACCOUNT_CONFIRMED", acct, actor)
    emit_workspace_event(WorkspaceEvent.ACCOUNT_CONFIRMED, workspace_uuid=ws.workspace_uuid, account=acct,
                         summary="broker account confirmed by customer",
                         source="hosted_workspace.onboarding")
    return ConfirmResult(True, CONFIRM_OK)


def authorize_workspace_execution(user, workspace, *, actor="", request=None) -> AuthorizeResult:
    """ADR-0047 — the customer's EXPLICIT, durable "Enable automated trading" authorization. This is the ONLY
    writer of ``HostedMt5Workspace.execution_authorized_at`` and the ONLY path by which a hosted workspace may
    ever become armed. It records the authorization, then attempts the certified
    ``arm_hosted_workspace_execution`` (which re-proves every precondition, incl. the now-satisfied authz).

    Contract (supersedes ADR-0044 Decision 2): MT5 automation CAPABILITY (trade_allowed / EXECUTION_READY) is
    NOT authorization. Reaching EXECUTION_READY alone can NEVER arm — both the arm preconditions and the order
    gate fail closed on NULL authorization. Offered ONLY once the account is CONFIRMED (identity ACK) and the
    workspace is observed CONNECTED + matched AND canonically EXECUTION_READY, so the customer authorizes a
    genuinely ready workspace. Owner-scoped (IDOR-safe), idempotent, audited. NEVER accepts a secret; places no
    order; the live bridge gate remains the sole order authority."""
    ok, reason = hosted_workspace_admission(user)
    if not ok:
        return AuthorizeResult(False, reason)

    from django.utils import timezone
    from execution.hosted_provisioning import arm_hosted_workspace_execution

    with transaction.atomic():
        ws = (HostedMt5Workspace.objects.select_for_update()
              .select_related("trading_account").get(pk=workspace.pk))
        if ws.trading_account.user_id != getattr(user, "pk", None):
            return AuthorizeResult(False, AUTHZ_NOT_OWNER)            # owner-scoped (trading_account.user), IDOR-safe
        acct = ws.trading_account
        if acct.workspace_confirmed_at is None:                       # identity ACK must precede authorization
            return AuthorizeResult(False, AUTHZ_NOT_CONFIRMED)
        # Authorize ONLY a workspace observed CONNECTED + matched AND canonically EXECUTION_READY. Capability
        # precedes authorization; "reaching EXECUTION_READY" is never itself the arm — this explicit click is.
        if (str(ws.canonical_state) != S.EXECUTION_READY
                or ws.proj_connected is not True or ws.proj_account_match is not True):
            return AuthorizeResult(False, AUTHZ_NOT_READY)
        already = ws.execution_authorized_at is not None
        if not already:
            ws.execution_authorized_at = timezone.now()
            ws.save(update_fields=["execution_authorized_at", "updated_at"])

    # Attempt the certified arm OUTSIDE the authorization row-lock. It re-proves EVERY precondition (including
    # the authorization just recorded) and is itself idempotent + audited; the durable authorization stands
    # regardless of the arm outcome, so a transient not-ready just leaves the (now-authorized) auto_arm_runner
    # to complete the arm on the next EXECUTION_READY cycle.
    arm = arm_hosted_workspace_execution(acct, actor=actor, request=request)
    _audit(request, "HOSTED_EXECUTION_AUTHORIZED", acct, actor)
    return AuthorizeResult(True, AUTHZ_ALREADY if already else AUTHZ_OK, arm_reason=arm.reason_code)


def bind_broker_identity(user, workspace, *, expected_login, expected_server="", actor="", request=None) -> BindResult:
    """DEFERRED IDENTITY BIND (Beta UX Correction, Sponsor 2026-08-15). Declare the customer's expected broker
    identity (login + server) AFTER the workspace is provisioned, from the trusted customer/API call arguments —
    the authoritative, EXTERNAL declaration that every later gate compares the OBSERVED login against. Owner-
    scoped (IDOR-safe); Provider-B hosted + DEMO only; allowed only while the identity is UNBOUND and the
    workspace is still pre-connected (PROVISIONING / WAITING_FOR_LOGIN). WRITE-ONCE: an identical re-declaration
    is idempotent; a DIFFERENT second bind fails closed (never overwrites). NEVER accepts a password, and NEVER
    derives the expected identity from an observation (tenant-forgeable pre-cert, ADR-0041) — the observation
    stays CONFIRMATION evidence only. Arms nothing and advances no state; it only writes the two durable identity
    fields (``account_number`` / ``broker_server``) that the order-time pin and account-match already read, so
    the pin is unchanged — only the MOMENT the identity becomes non-empty moves from request to here. An unbound
    account has an empty expected identity, so no order can flow (account_match False → holds at WAITING_FOR_LOGIN,
    never is_active, never armed; and the bridge fails closed ``identity_pin_required``)."""
    login = str(expected_login or "").strip()
    server_name = str(expected_server or "").strip()
    if not login:
        return BindResult(False, BIND_LOGIN_REQUIRED)
    # Bound-length validation BEFORE the DB write so an over-long identifier is a clean 400, not a 500.
    if len(login) > _MAX_LOGIN_LEN or len(server_name) > _MAX_SERVER_LEN:
        return BindResult(False, BIND_IDENTITY_INVALID)

    from execution.readiness import PERSISTENT_WORKSPACE
    from trading.models import BrokerServer

    with transaction.atomic():
        # NB: only select_related the REQUIRED trading_account FK — NOT the nullable broker_server (Postgres
        # cannot apply FOR UPDATE to the nullable side of an outer join). broker_server lazy-loads below.
        ws = (HostedMt5Workspace.objects.select_for_update()
              .select_related("trading_account").get(pk=workspace.pk))
        acct = ws.trading_account
        if acct.user_id != getattr(user, "pk", None):
            return BindResult(False, BIND_NOT_OWNER)          # owner-scoped (trading_account.user), IDOR-safe
        if str(getattr(acct, "readiness_provider", "")) != PERSISTENT_WORKSPACE:
            return BindResult(False, BIND_NOT_HOSTED)         # Provider-B hosted accounts only
        if acct.is_demo is not True:
            return BindResult(False, BIND_LIVE_FORBIDDEN)     # Closed Beta is DEMO-only; never self-authorize live
        if str(ws.canonical_state) not in (S.PROVISIONING, S.WAITING_FOR_LOGIN):
            return BindResult(False, BIND_WRONG_STATE)        # only before the workspace connects
        prior_login = str(acct.account_number or "").strip()
        if prior_login:
            # WRITE-ONCE. Idempotent iff the SAME (login, server) is re-declared; any difference fails closed.
            prior_server = str(getattr(acct.broker_server, "server_name", "") or "").strip()
            if prior_login == login and prior_server == server_name:
                return BindResult(True, BIND_IDEMPOTENT)
            return BindResult(False, BIND_ALREADY)
        server = None
        if server_name:
            server, _ = BrokerServer.objects.get_or_create(server_name=server_name)
        acct.account_number = login
        acct.broker_server = server
        try:
            with transaction.atomic():   # nested savepoint: a duplicate-identity collision is a clean 4xx, not a 500
                acct.save(update_fields=["account_number", "broker_server"])
        except IntegrityError:
            return BindResult(False, BIND_IDENTITY_INVALID)

    _audit(request, "HOSTED_WORKSPACE_IDENTITY_BOUND", acct, actor)
    emit_workspace_event(WorkspaceEvent.IDENTITY_BOUND, workspace_uuid=ws.workspace_uuid, account=acct,
                         summary="broker identity declared by customer",
                         detail={"expected_login_masked": _mask(login), "expected_server": server_name},
                         source="hosted_workspace.onboarding")
    return BindResult(True, BIND_OK)


def _audit(request, event_type, account, actor) -> None:
    """Best-effort, non-secret audit (fail-open — never blocks the onboarding action)."""
    try:
        from core.audit import log_event
        log_event(request, event_type, severity="INFO", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata={"actor": str(actor or "")})
    except Exception:  # noqa: BLE001
        pass
