"""hosted_workspace.slot_preparation — Beta Readiness Stream 4: the host provisioning engine (DARK).

``prepare_hosted_slot(workspace)`` is the single idempotent, fail-closed orchestrator that makes a customer's
Windows hosted slot EXIST on its node — everything the customer needs to log their OWN broker in, and nothing
more. It provisions, in dependency order:

    identity record → Windows identity+folders → per-user NTFS ACL (G5) → mark PROVISIONED → golden runtime
    → RDP grant → single-session → RemoteApp (verify) → AppLocker PREPARATION (AuditOnly) → observer (deferred)

It then GATES the ``PROVISIONING → WAITING_FOR_LOGIN`` transition: the certified single writer advances only on
a ``prepared`` result, so a customer is never told "log in" with no slot behind it (the state-ahead-of-reality
gap the audit found).

BOUNDARY (architecture.md, 2026-07-22). This orchestrator lives in the Django plane ABOVE the Windows-primitive
boundary. It legitimately knows workspace identity / ownership / node binding (like ``delivery.py``), but it
NEVER reaches into a Windows primitive with a UUID/generation/job — host mutation is delegated to a signed
``HostExecutor`` that receives ONLY a fixed slot identity (``guvfx_u_<id>``), a fixed ``runtime_root``, and the
node's ``rdp_host``. The G5 ACL brain (``workspace_acl``) is pure policy+verification, not an agent primitive.

DARK. Two-level darkness like the G15 scheduler: dormant unless ``hosted_persistent_mt5_enabled()`` AND the
dedicated ``hosted_slot_prep_enabled()`` are on. And even armed, the host-executor is a PLUGGABLE seam that is
``None`` in this repository-only phase (``resolve_host_executor`` returns None, exactly like the G15
``_dark_observe_fn``) — so every host step fails closed (``host_executor_unavailable``), the state is NOT
advanced, and NO host is ever contacted from the repository. A later host-certification increment supplies a
real signed executor here without touching this control flow. Customer Zero / the PRODUCTION identity is
refused up front. Nothing here arms execution or performs a broker login — broker login is excluded entirely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger("guvfx.hosted_workspace")

# ---- Stable, secret-free reason codes --------------------------------------------------------------------
PREP_DARK = "slot_prep_dark"                     # a flag is off → no-op, no host contact
PREP_REFUSED_RESERVED = "reserved_identity_refused"   # Customer Zero / PRODUCTION / admin identity
PREP_NOT_BOUND = "workspace_not_bound"           # no execution_node yet (allocate must run first)
PREP_NODE_UNCONFIGURED = "node_rdp_host_unset"   # bound node has no durable rdp_host (G12)
PREP_NO_EXECUTOR = "host_executor_unavailable"   # DARK seam: no signed host bridge in this phase
PREP_EXECUTOR_INCOMPLETE = "host_executor_incomplete"   # executor lacks a required host step
PREP_IDENTITY_FAILED = "identity_materialise_failed"
PREP_ACL_FAILED = "workspace_acl_failed"         # apply or read-back verification failed → rolled back
PREP_POPULATE_FAILED = "runtime_populate_failed"
PREP_AUTOTRADING_FAILED = "autotrading_config_failed"
PREP_RDP_FAILED = "rdp_grant_failed"
PREP_SESSION_FAILED = "single_session_failed"
PREP_REMOTEAPP_FAILED = "remoteapp_not_published"
PREP_APPLOCKER_FAILED = "applocker_prepare_failed"
PREP_OBSERVER_FAILED = "observer_prepare_failed"   # BB#1: required observer prep (flag on) failed / not implemented
PREP_BRIDGE_FAILED = "order_bridge_activation_failed"        # host activate/health-check failed → fail closed
PREP_BRIDGE_ENDPOINT_CONFLICT = "order_bridge_endpoint_conflict"  # node already routes elsewhere (e.g. CZ :8788)
PREP_BRIDGE_FORBIDDEN_NODE = "order_bridge_forbidden_node"   # Customer-Zero / forbidden execution node — refused
PREP_HOST_ERROR = "host_step_error"              # executor raised — sanitised, never leaks detail
PREP_OK = "prepared"

# Every non-OK slot-preparation outcome, as one authoritative set (single source of truth). The provisioning
# scheduler uses it to bucket a slot-prep failure DISTINCTLY from an unexpected allocation error, so a bad
# rollout of a REQUIRED host step (notably the BB#1 observer edge) is visible in the summary, not swallowed into
# the generic ``errors`` count (adversarial-review MEDIUM fix).
PREP_FAILURE_REASONS = frozenset({
    PREP_DARK, PREP_REFUSED_RESERVED, PREP_NOT_BOUND, PREP_NODE_UNCONFIGURED, PREP_NO_EXECUTOR,
    PREP_EXECUTOR_INCOMPLETE, PREP_IDENTITY_FAILED, PREP_ACL_FAILED, PREP_POPULATE_FAILED,
    PREP_AUTOTRADING_FAILED, PREP_RDP_FAILED, PREP_SESSION_FAILED, PREP_REMOTEAPP_FAILED,
    PREP_APPLOCKER_FAILED, PREP_OBSERVER_FAILED, PREP_BRIDGE_FAILED, PREP_BRIDGE_ENDPOINT_CONFLICT,
    PREP_BRIDGE_FORBIDDEN_NODE, PREP_HOST_ERROR,
})

# Stage labels (for stage_reached / audit — never carry a secret).
ST_GUARD = "guard"
ST_IDENTITY_RECORD = "identity_record"
ST_MATERIALISE = "materialise_identity_and_folders"
ST_ACL = "apply_workspace_acl"
ST_MARK = "mark_materialised"
ST_POPULATE = "populate_runtime"
ST_AUTOTRADING = "apply_autotrading_config"
ST_RDP = "grant_rdp"
ST_SESSION = "enforce_single_session"
ST_REMOTEAPP = "verify_remoteapp"
ST_APPLOCKER = "applocker_prepare"
ST_BRIDGE = "activate_order_bridge"
ST_OBSERVER = "register_observer"
ST_DONE = "done"


# The per-node order-bridge listen port. SINGLE SOURCE OF TRUTH: this constant, the node bridge launcher
# (deploy/node2-order-bridge/start_node2_bridge.bat HTTP_SERVER_PORT) and the activation primitive
# (Activate-GuvfxOrderBridge.ps1 $PORT) MUST all be 8789 — the Closed-Beta node bridge port, distinct from
# Customer Zero's :8788 and the executor/agent :8790/:8791. It is deliberately NOT an operator knob: a
# runtime-configurable port on the Django side (that the host does not honour) could persist a routing URL
# pointing at a dead port — or, at 8788, at Customer Zero's un-pinned bridge — while the host still binds
# 8789 and its /health passes (a divergent-source-of-truth defect). A static test asserts the three agree.
ORDER_BRIDGE_PORT = 8789


def _emit_bridge_event(ws, account, *, activated: bool) -> None:
    """Fail-open telemetry for the order-bridge activation outcome. Never breaks provisioning, never carries the
    endpoint/host (the node's persisted ``order_bridge_base_url`` is the record of truth)."""
    try:
        from hosted_workspace.telemetry import WorkspaceEvent, emit_workspace_event
        ev = (WorkspaceEvent.ORDER_BRIDGE_ACTIVATED if activated
              else WorkspaceEvent.ORDER_BRIDGE_ACTIVATION_FAILED)
        emit_workspace_event(
            ev, workspace_uuid=ws.workspace_uuid, account=account,
            summary=("Order bridge activated" if activated else "Order bridge activation failed"))
    except Exception:  # noqa: BLE001 — telemetry must never break provisioning
        pass


@dataclass(frozen=True)
class SlotPreparationResult:
    """Secret-free result. Carries only booleans / stable reason codes / stage labels — never a password, a
    resolved path, a command, or an exception message."""
    prepared: bool
    reason: str
    stage_reached: str
    observer_deferred: bool = False
    applocker_deferred: bool = False
    detail: dict = field(default_factory=dict)   # small, non-secret (e.g. {"already": True})


def _reserved_account_ids() -> set[int]:
    """Account ids ``prepare_hosted_slot`` must NEVER touch. Default {1} = Customer Zero (the legacy shared
    Administrator runtime / ``guvfx_u_1``). Overridable via settings/env for defence in depth."""
    raw = getattr(settings, "HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS", None)
    if raw is None:
        import os
        raw = os.getenv("HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS")
    if raw is None:
        return {1}          # true default (unset): protect Customer Zero (account #1 / guvfx_u_1)
    raw_str = str(raw).strip()
    if raw_str == "":
        return set()        # explicit empty string = deliberate operator override ("no reserved ids")
    out: set[int] = set()
    for tok in raw_str.replace(",", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            continue
    # Customer Zero (account #1) is a HARD FLOOR: any non-empty override UNIONS with {1}, never removes it —
    # so a garbled value ("none") or a typo ("1o 2" → {1,2}) still protects CZ. Only an explicit "" disables.
    return out | {1}


def resolve_host_executor(account_id=None, rdp_host=""):
    """Return the signed host-executor the engine drives, or ``None`` (DARK). Delegates to the Stream 5
    ``SignedHostExecutor`` factory, which returns ``None`` unless ``HOSTED_HOST_EXECUTOR_ENABLED`` is on AND the
    keyring / base_url / envelope key are configured — so in the repository-only / unarmed phase this is ``None``
    and every host step fails closed, contacting no host (the ``_dark_observe_fn`` pattern)."""
    if account_id is None:
        return None
    from hosted_workspace.host_executor import resolve_signed_host_executor
    return resolve_signed_host_executor(account_id=account_id, rdp_host=rdp_host)


def _ok(res) -> bool:
    return bool(res) and bool(res.get("ok"))


def prepare_hosted_slot(workspace, *, executor=None, actor: str = "", request=None) -> SlotPreparationResult:
    """Idempotent, fail-closed. Returns ``prepared=True`` only when every non-deferred step read-back-verified;
    the caller then advances ``PROVISIONING → WAITING_FOR_LOGIN``. Never raises into the caller, never arms
    execution, never performs a broker login."""
    from hosted_workspace.flags import (
        hosted_delivery_lifecycle_enabled, hosted_persistent_mt5_enabled, hosted_slot_prep_enabled)

    # ---- Stage 0: fail-closed guards (no host contact) ----------------------------------------------------
    if not (hosted_persistent_mt5_enabled() and hosted_slot_prep_enabled()):
        return SlotPreparationResult(False, PREP_DARK, ST_GUARD)

    from django.db import transaction

    from terminal_provisioning import services as prov_services
    from terminal_provisioning.models import AccountProvisioning

    from hosted_workspace.models import HostedMt5Workspace
    from hosted_workspace.provisioning_timing import (
        STAGE_ACL_COMPLETE, STAGE_IDENTITY_CREATED, STAGE_OBSERVER_PREPARED, STAGE_ORDER_BRIDGE_ACTIVATED,
        STAGE_REMOTEAPP_PUBLISHED, STAGE_RUNTIME_MATERIALISED, record_stage_timing)
    from hosted_workspace.workspace_acl import AclError, build_workspace_acl_plan, verify_workspace_acl

    ws = (HostedMt5Workspace.objects.select_related("trading_account").get(pk=workspace.pk))
    account = ws.trading_account
    account_id = account.pk

    if account_id in _reserved_account_ids():
        return SlotPreparationResult(False, PREP_REFUSED_RESERVED, ST_GUARD)

    # Refuse the legacy Administrator identity (defence in depth beyond the reserved-id set).
    existing_prov = AccountProvisioning.objects.filter(trading_account=account).first()
    if existing_prov is not None and existing_prov.is_admin:
        return SlotPreparationResult(False, PREP_REFUSED_RESERVED, ST_GUARD)

    if ws.execution_node_id is None:
        return SlotPreparationResult(False, PREP_NOT_BOUND, ST_GUARD)

    from execution.models import TerminalNode
    node = TerminalNode.objects.filter(pk=ws.execution_node_id).first()
    rdp_host = str(getattr(node, "rdp_host", "") or "").strip() if node else ""
    if not rdp_host:
        return SlotPreparationResult(False, PREP_NODE_UNCONFIGURED, ST_GUARD)

    # ---- Stage 1: identity record (DB only, idempotent) ---------------------------------------------------
    try:
        prov = prov_services.provision(account, actor=None)
    except prov_services.ProvisioningError:
        return SlotPreparationResult(False, PREP_IDENTITY_FAILED, ST_IDENTITY_RECORD)
    username = prov.windows_username
    runtime_root = prov.runtime_root

    # From here every step touches the host. Resolve the executor; DARK by default → fail closed.
    ex = executor if executor is not None else resolve_host_executor(account_id=account_id, rdp_host=rdp_host)
    if ex is None:
        return SlotPreparationResult(False, PREP_NO_EXECUTOR, ST_MATERIALISE)

    def _call(method_name, stage, *args, required=True, **kwargs):
        """Invoke one host step. Missing method → EXECUTOR_INCOMPLETE (required) or (None, deferred). Any
        exception → sanitised PREP_HOST_ERROR (never leaks detail)."""
        fn = getattr(ex, method_name, None)
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — host errors are sanitised, never propagated or logged verbatim
            logger.warning("hosted slot prep: host step %s errored for account=%s", stage, account_id)
            return {"ok": False, "reason": PREP_HOST_ERROR}

    # ---- Stage 2: materialise identity + folders on the host (spec carries the password — NEVER logged) ----
    spec = prov_services.build_spec(prov)          # includes decrypted password → passed to executor only
    res = _call("materialise_identity", ST_MATERIALISE, spec, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_MATERIALISE)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_IDENTITY_FAILED, ST_MATERIALISE)
    record_stage_timing(ws, STAGE_IDENTITY_CREATED)   # UX timing (fail-open)

    # ---- Stage 3: per-user NTFS ACL (G5) — apply then SID-typed read-back verify; roll back on mismatch ----
    try:
        plan = build_workspace_acl_plan(runtime_root, username)
    except AclError:
        return SlotPreparationResult(False, PREP_ACL_FAILED, ST_ACL)
    res = _call("apply_workspace_acl", ST_ACL, plan, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_ACL)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_ACL_FAILED, ST_ACL)
    verdict = verify_workspace_acl(res.get("rows", []), user_sid=res.get("user_sid", ""),
                                   protected=bool(res.get("protected", False)))
    if not verdict.ok:
        # Independent read-back proved the DACL is wrong (e.g. a leaking principal). Instruct the executor to
        # restore the pre-apply snapshot so the host is never left with a leaking / half-applied ACL and retries
        # are not additive (best-effort — the host Apply also rebuilds the DACL to exactly the target set and
        # self-rolls-back on its own control failure). We then fail closed and never advance state.
        _call("rollback_workspace_acl", ST_ACL, plan, rdp_host=rdp_host, required=False)
        logger.warning("hosted slot prep: ACL verify failed (%s) for account=%s", verdict.reason, account_id)
        return SlotPreparationResult(False, PREP_ACL_FAILED, ST_ACL, detail={"acl_reason": verdict.reason})
    record_stage_timing(ws, STAGE_ACL_COMPLETE)   # UX timing (fail-open)

    # ---- Stage 4: mark PROVISIONED — ONLY after identity + ACL host read-back proofs (not operator word) ---
    with transaction.atomic():
        prov_services.mark_materialized(account, identity=True, runtime=True)

    # ---- Stage 5: golden runtime into runtime_root\terminal (RULE 10 clean image) -------------------------
    res = _call("populate_runtime", ST_POPULATE, runtime_root, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_POPULATE)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_POPULATE_FAILED, ST_POPULATE)
    record_stage_timing(ws, STAGE_RUNTIME_MATERIALISED)   # UX timing (fail-open)

    # ---- Stage 5b: AutoTrading CAPABILITY config — write [Experts] AllowLiveTrading=1 Enabled=1 into the
    #      runtime's common.ini (the empirically certified minimum). This is CAPABILITY ONLY: it authorises no
    #      order. Execution stays gated independently (HOSTED_MT5_EXECUTION_ENABLED + the per-workspace arm + the
    #      live order-time bridge, all DARK) and no broker is logged in — so the key is inert until a human-gated
    #      arm. If the executor does not implement it (older host), the step is required and fails closed. --------
    res = _call("apply_autotrading_config", ST_AUTOTRADING, runtime_root, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_AUTOTRADING)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_AUTOTRADING_FAILED, ST_AUTOTRADING)

    # ---- Stage 5c: autonomous per-node ORDER-BRIDGE activation (FINAL Closed-Beta stream) -----------------
    #      When HOSTED_ORDER_BRIDGE_AUTO_ACTIVATE_ENABLED is on, activate THIS node's dedicated pin-enforcing
    #      order bridge as a REQUIRED, fail-closed host step, then persist the node's order_bridge_base_url —
    #      so the customer reaches WAITING_FOR_LOGIN with the order path already wired (no manual step). The
    #      runtime is populated (Stage 5) so the tenant terminal path exists; the bridge attaches per-order at
    #      trade time. Broker identity (deferred bind) is NOT needed: the per-job pin carries the server-derived
    #      expected login/server. While the flag is off this whole stage is skipped — byte-identical to before
    #      this stream. Customer Zero is protected FOUR independent ways: the reserved-account guard (Stage 0),
    #      the forbidden-node guard, the never-overwrite-a-different-endpoint guard, and the host-side
    #      reserved-identity refusal. This grants NO order authority (the order-time bridge gate stays live).
    from hosted_workspace.flags import (hosted_order_bridge_auto_activate_enabled,
                                         hosted_per_tenant_transport_enabled)
    if hosted_order_bridge_auto_activate_enabled():
        from hosted_workspace.tenant_isolation import forbidden_execution_node_ids
        # Guard A: never activate/route a Customer-Zero / forbidden execution node (derived live from the DB).
        if ws.execution_node_id in forbidden_execution_node_ids():
            return SlotPreparationResult(False, PREP_BRIDGE_FORBIDDEN_NODE, ST_BRIDGE)

        if hosted_per_tenant_transport_enabled():
            # ---- P0-B1.1 PER-TENANT bridge: allocate THIS tenant's own endpoint (unique port) and activate its
            # OWN pin-enforcing bridge on that port — so N tenants share one node/host, each with a private
            # bridge/terminal. No node-global :8789 is written. Identity is server-derived (allocate_endpoint
            # reads the PROVISIONED AccountProvisioning, set at Stage 4); a NEW tenant is an is_active=False
            # intent account so no re-home guard trips. Fail-closed on any allocation/activation error.
            from execution import endpoint_service
            try:
                alloc = endpoint_service.allocate_endpoint(ws, actor="slot_preparation")
            except endpoint_service.EndpointError:
                return SlotPreparationResult(False, PREP_BRIDGE_FAILED, ST_BRIDGE)
            res = _call("activate_tenant_bridge", ST_BRIDGE, runtime_root, port=alloc.port, rdp_host=rdp_host)
            if res is None:
                return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_BRIDGE)
            if not _ok(res):
                _emit_bridge_event(ws, account, activated=False)
                return SlotPreparationResult(False, PREP_BRIDGE_FAILED, ST_BRIDGE)
            # Bridge proven up + health-checked host-side → the endpoint is now routable. Guarded like
            # allocate_endpoint so a concurrent deprovision (endpoint retired mid-prep) fails closed with a
            # sanitised result rather than a raw traceback (the endpoint stays non-READY → still unroutable).
            try:
                endpoint_service.mark_ready(ws, health_ok=True, actor="slot_preparation")
            except endpoint_service.EndpointError:
                return SlotPreparationResult(False, PREP_BRIDGE_FAILED, ST_BRIDGE)
            _emit_bridge_event(ws, account, activated=True)
            record_stage_timing(ws, STAGE_ORDER_BRIDGE_ACTIVATED)
        else:
            # ---- LEGACY per-node :8789 path — byte-identical to before P0-B1.1 when the per-tenant flag is off.
            endpoint = "http://%s:%d" % (rdp_host, ORDER_BRIDGE_PORT)
            # Guard B: never clobber a node already routing to a DIFFERENT endpoint (e.g. Customer Zero's
            # :8788). A blank or already-equal value is idempotent; anything else is a conflict → fail closed.
            cur = str(TerminalNode.objects.filter(pk=node.pk)
                      .values_list("order_bridge_base_url", flat=True).first() or "").strip()
            if cur and cur != endpoint:
                return SlotPreparationResult(False, PREP_BRIDGE_ENDPOINT_CONFLICT, ST_BRIDGE)
            res = _call("activate_order_bridge", ST_BRIDGE, runtime_root, rdp_host=rdp_host)
            if res is None:
                return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_BRIDGE)
            if not _ok(res):
                _emit_bridge_event(ws, account, activated=False)
                return SlotPreparationResult(False, PREP_BRIDGE_FAILED, ST_BRIDGE)
            # Persist the SERVER-DERIVED endpoint (never the host's asserted value). The filter re-checks the
            # never-clobber invariant atomically, so a concurrent writer can never race the URL onto a CZ node.
            TerminalNode.objects.filter(pk=node.pk, order_bridge_base_url__in=("", endpoint)).update(
                order_bridge_base_url=endpoint)
            _emit_bridge_event(ws, account, activated=True)
            record_stage_timing(ws, STAGE_ORDER_BRIDGE_ACTIVATED)   # UX timing (fail-open)

    # ---- Stage 6: RDP grant (hard-scoped to guvfx_u_*) ----------------------------------------------------
    res = _call("grant_rdp", ST_RDP, username, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_RDP)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_RDP_FAILED, ST_RDP)

    # ---- Stage 7: single-session enforcement (reconnect rejoins the one per-user session) -----------------
    res = _call("enforce_single_session", ST_SESSION, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_SESSION)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_SESSION_FAILED, ST_SESSION)

    # ---- Stage 8: RemoteApp — VERIFY exactly-one alias published for this identity (publication is host-cert
    #      manual; prepare only verifies). Fail closed if the ||terminal64 alias is not present exactly once. -
    res = _call("verify_remoteapp", ST_REMOTEAPP, username, runtime_root, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_REMOTEAPP)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_REMOTEAPP_FAILED, ST_REMOTEAPP)
    record_stage_timing(ws, STAGE_REMOTEAPP_PUBLISHED)   # UX timing (fail-open)

    # ---- Stage 9: AppLocker PREPARATION (AuditOnly) — NEVER -Enforce here. DEFERRED / non-blocking: the current
    #      policy model REPLACES the machine-wide policy (no -Merge), so running it on a host that carries an
    #      ENFORCED Customer-Zero policy would wipe CZ's enforcement. Until the multi-tenant -Merge model lands it
    #      must be enabled only on a host WITHOUT an enforced CZ policy (ADR-0037 / runbook). Execution is DARK, so
    #      no confinement gap is exercised meanwhile. A missing/failed step therefore does NOT block prepared. ----
    applocker_deferred = True
    res = _call("applocker_prepare", ST_APPLOCKER, username, rdp_host=rdp_host, required=False)
    if res is not None and _ok(res):
        applocker_deferred = False

    # ---- Stage 10: observer registration. BB#1 (Sponsor 2026-08-16): with the delivery-lifecycle flag ON the
    #      read-only session-bound observer is a REQUIRED, idempotent, stage-timed host step — a fresh non-CZ
    #      hosted account MUST receive its observer autonomously (the observe bridge now exists, so the historical
    #      "deferred until a bridge lands" no longer holds; without it delivery can never reach a TRUSTED
    #      CONNECTED and the customer stalls). The observer is read-only (guarded-attach; never launch/login/
    #      trade) so this grants NO authority. While the flag is OFF this is EXACTLY the prior best-effort
    #      DEFERRED step — byte-identical, so Customer Zero and every existing slot are unchanged. ---------------
    observer_deferred = True
    res = _call("register_observer", ST_OBSERVER, username, runtime_root, rdp_host=rdp_host)
    if res is not None and _ok(res):
        observer_deferred = False
        if hosted_delivery_lifecycle_enabled():
            record_stage_timing(ws, STAGE_OBSERVER_PREPARED)   # lifecycle timing (fail-open)
    elif hosted_delivery_lifecycle_enabled():
        # REQUIRED under the delivery-lifecycle flag and it did not read-back-verify → fail closed (never
        # advance to WAITING_FOR_LOGIN with no observer). Missing method (older host) → EXECUTOR_INCOMPLETE;
        # an ok:false / host error → OBSERVER_FAILED. Idempotency is the host primitive's contract (re-running
        # register_observer for an already-registered slot returns ok:true).
        if res is None:
            return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_OBSERVER)
        return SlotPreparationResult(False, PREP_OBSERVER_FAILED, ST_OBSERVER)

    logger.info("hosted slot prep: prepared account=%s node=%s observer_deferred=%s applocker_deferred=%s",
                account_id, ws.execution_node_id, observer_deferred, applocker_deferred)
    return SlotPreparationResult(True, PREP_OK, ST_DONE, observer_deferred=observer_deferred,
                                 applocker_deferred=applocker_deferred)
