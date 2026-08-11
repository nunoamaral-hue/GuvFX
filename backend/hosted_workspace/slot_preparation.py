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
PREP_HOST_ERROR = "host_step_error"              # executor raised — sanitised, never leaks detail
PREP_OK = "prepared"

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
ST_OBSERVER = "register_observer"
ST_DONE = "done"


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
    from hosted_workspace.flags import hosted_persistent_mt5_enabled, hosted_slot_prep_enabled

    # ---- Stage 0: fail-closed guards (no host contact) ----------------------------------------------------
    if not (hosted_persistent_mt5_enabled() and hosted_slot_prep_enabled()):
        return SlotPreparationResult(False, PREP_DARK, ST_GUARD)

    from django.db import transaction

    from terminal_provisioning import services as prov_services
    from terminal_provisioning.models import AccountProvisioning

    from hosted_workspace.models import HostedMt5Workspace
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

    # ---- Stage 4: mark PROVISIONED — ONLY after identity + ACL host read-back proofs (not operator word) ---
    with transaction.atomic():
        prov_services.mark_materialized(account, identity=True, runtime=True)

    # ---- Stage 5: golden runtime into runtime_root\terminal (RULE 10 clean image) -------------------------
    res = _call("populate_runtime", ST_POPULATE, runtime_root, rdp_host=rdp_host)
    if res is None:
        return SlotPreparationResult(False, PREP_EXECUTOR_INCOMPLETE, ST_POPULATE)
    if not _ok(res):
        return SlotPreparationResult(False, PREP_POPULATE_FAILED, ST_POPULATE)

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

    # ---- Stage 9: AppLocker PREPARATION (AuditOnly) — NEVER -Enforce here. DEFERRED / non-blocking: the current
    #      policy model REPLACES the machine-wide policy (no -Merge), so running it on a host that carries an
    #      ENFORCED Customer-Zero policy would wipe CZ's enforcement. Until the multi-tenant -Merge model lands it
    #      must be enabled only on a host WITHOUT an enforced CZ policy (ADR-0037 / runbook). Execution is DARK, so
    #      no confinement gap is exercised meanwhile. A missing/failed step therefore does NOT block prepared. ----
    applocker_deferred = True
    res = _call("applocker_prepare", ST_APPLOCKER, username, rdp_host=rdp_host, required=False)
    if res is not None and _ok(res):
        applocker_deferred = False

    # ---- Stage 10: observer registration — DEFERRED. The host observe bridge does not exist yet (G15's
    #      observe_fn is dark), so a missing register_observer method does NOT block prepared: the slot exists
    #      and the customer can log in; autonomous state advance past WAITING_FOR_LOGIN awaits the bridge. -----
    observer_deferred = True
    res = _call("register_observer", ST_OBSERVER, username, runtime_root, rdp_host=rdp_host, required=False)
    if res is not None and _ok(res):
        observer_deferred = False

    logger.info("hosted slot prep: prepared account=%s node=%s observer_deferred=%s applocker_deferred=%s",
                account_id, ws.execution_node_id, observer_deferred, applocker_deferred)
    return SlotPreparationResult(True, PREP_OK, ST_DONE, observer_deferred=observer_deferred,
                                 applocker_deferred=applocker_deferred)
