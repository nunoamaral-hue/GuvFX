"""CVM-Inc-3 B3P-2 — occupancy binding, process birth identity and task identity.

This module sits **above** the Windows primitive layer and exists to keep that layer ignorant. It produces
the complete immutable occupancy binding, hands a primitive only the slot-scoped portion it needs to act,
and reconciles the primitive's result back to the same occupancy before the result is believed.

Design test enforced here (architecture rule): *a primitive that needs a runtime UUID or ProvisioningJob id
to decide what local operation to perform is a leaking abstraction.* :func:`slot_scoped_view` is the
enforcement point — it is physically incapable of carrying those.
"""
import hashlib
import json

from lib.mgmt_agent_core import AgentError
from stores import occupancy_id, path_digest


class OccupancyBindingMismatch(AgentError):
    """A primitive result could not be reconciled back to the occupancy binding it was issued under."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("occupancy_binding_mismatch")


class ProcessIdentityMismatch(AgentError):
    """An observed process does not match the recorded process-birth evidence (PID reuse, or worse)."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("process_identity_mismatch")


class TaskDefinitionDrift(AgentError):
    """The installed scheduled task no longer matches its approved definition."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("task_definition_drift")


# ── 1. occupancy binding ───────────────────────────────────────────────────────────────────────────────
BINDING_FIELDS = (
    "runtime_uuid", "slot", "generation", "occupancy_id", "slot_path_digest",
    "task_identity", "integrity_outcome", "quarantined",
)

#: The ONLY keys a Windows primitive ever receives. Deliberately excludes runtime_uuid, generation,
#: occupancy_id, ProvisioningJob and every GuvFX policy concept.
SLOT_SCOPED_FIELDS = ("slot", "slot_path", "launch_task", "terminate_task")


def build_occupancy_binding(*, runtime_uuid, slot, generation, slot_path, task_identity,
                            integrity_outcome, quarantined) -> dict:
    """Produce the complete immutable binding the upper layer must hold before ANY mutating operation.

    Preserved in full in audit evidence; only :func:`slot_scoped_view` of it reaches a primitive.
    """
    binding = {
        "runtime_uuid": str(runtime_uuid),
        "slot": int(slot),
        "generation": int(generation),
        "occupancy_id": occupancy_id(slot, generation),
        "slot_path_digest": path_digest(slot_path),
        "task_identity": dict(task_identity or {}),
        "integrity_outcome": integrity_outcome,
        "quarantined": bool(quarantined),
    }
    return binding


def slot_scoped_view(binding: dict, *, slot_path: str, launch_task: str, terminate_task: str) -> dict:
    """The slot-scoped portion a primitive is allowed to see: physical facts only.

    A primitive receiving this cannot learn the runtime UUID, the generation, the occupancy id, the job or
    any tenant/entitlement concept — so it cannot make a policy decision even by accident.
    """
    return {
        "slot": int(binding["slot"]),
        "slot_path": slot_path,
        "launch_task": launch_task,
        "terminate_task": terminate_task,
    }


def reconcile_primitive_result(binding: dict, result: dict, *, slot_path: str) -> None:
    """Reject a primitive result that cannot be reconciled back to the SAME occupancy.

    A primitive reports physical facts (slot, path). Those must match the binding under which it was
    invoked; anything else means the result belongs to a different slot/occupancy and must not be believed.
    """
    if result is None:
        raise OccupancyBindingMismatch("no result")
    if int(result.get("slot", -1)) != int(binding["slot"]):
        raise OccupancyBindingMismatch("slot")
    reported_path = result.get("slot_path")
    if reported_path is not None and path_digest(reported_path) != binding["slot_path_digest"]:
        raise OccupancyBindingMismatch("slot_path")
    if path_digest(slot_path) != binding["slot_path_digest"]:
        raise OccupancyBindingMismatch("binding_path")


# ── 2. process birth identity ──────────────────────────────────────────────────────────────────────────
BIRTH_FIELDS = ("pid", "created_at", "image_digest", "executable_containment_verified",
                "user_sid", "session_id", "slot")


def build_process_birth(*, pid, created_at, image_digest, executable_containment_verified,
                        user_sid, session_id, slot) -> dict:
    """Record what a process WAS AT BIRTH, so a later look-alike cannot inherit its identity.

    PID alone is not identity: Windows reuses PIDs, so a later unrelated process can carry the PID of an
    earlier occupancy. Creation time + image + owner + session + slot together make reuse detectable.
    """
    return {
        "pid": int(pid),
        "created_at": created_at,
        "image_digest": image_digest,
        "executable_containment_verified": bool(executable_containment_verified),
        "user_sid": user_sid,
        "session_id": session_id,
        "slot": int(slot),
    }


def assert_same_process(birth: dict, observed: dict) -> None:
    """Compare an observed process against recorded birth evidence. FAIL CLOSED on any divergence.

    Used by VERIFY and STOP. A matching PID with a different creation time, owner, image or slot is
    **not** the same process — that is precisely the PID-reuse case, and it must never be acted upon.
    """
    if not birth or not observed:
        raise ProcessIdentityMismatch("missing evidence")
    if int(observed.get("pid", -1)) != int(birth["pid"]):
        raise ProcessIdentityMismatch("pid")
    for field in ("created_at", "image_digest", "user_sid", "session_id"):
        if observed.get(field) != birth.get(field):
            raise ProcessIdentityMismatch(field)          # same PID, different process
    if int(observed.get("slot", -1)) != int(birth["slot"]):
        raise ProcessIdentityMismatch("slot")
    if not observed.get("executable_containment_verified"):
        raise ProcessIdentityMismatch("executable_containment")


# ── 3. task identity ───────────────────────────────────────────────────────────────────────────────────
TASK_IDENTITY_FIELDS = ("task_name", "run_as_identity", "executable", "working_directory",
                        "logon_type", "run_level", "enabled")


def task_definition_digest(definition: dict) -> str:
    """Deterministic digest over the approved task fields (order-independent)."""
    material = {k: definition.get(k) for k in TASK_IDENTITY_FIELDS}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def build_task_identity(definition: dict) -> dict:
    ident = {k: definition.get(k) for k in TASK_IDENTITY_FIELDS}
    ident["definition_digest"] = task_definition_digest(definition)
    return ident


def assert_task_matches_approved(approved: dict, installed: dict) -> None:
    """Confirm the INSTALLED task still matches its APPROVED definition. Drift BLOCKS launch.

    The agent may trigger and query a task; it must never repair or rewrite one during runtime operation,
    so drift is surfaced as a refusal, not silently corrected.
    """
    if not approved or not installed:
        raise TaskDefinitionDrift("missing definition")
    if task_definition_digest(approved) != task_definition_digest(installed):
        differing = [k for k in TASK_IDENTITY_FIELDS if approved.get(k) != installed.get(k)]
        raise TaskDefinitionDrift(",".join(differing) or "digest")
    if not installed.get("enabled"):
        raise TaskDefinitionDrift("disabled")
