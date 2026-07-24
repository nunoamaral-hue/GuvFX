"""CVM-Inc-3 B3P-2 — lifecycle vocabulary: stage status, failure classification, per-stage contracts.

Three concerns that must never be conflated, each answering a different question:

* **STATUS** (requirement 1) — *what happened to this stage?* Its job is to survive a retry after an
  ambiguous failure: ``COMPLETED`` and ``ALREADY_COMPLETED`` are both success, but only one of them means
  *this* attempt did the work. Losing that distinction is how a retry silently becomes a second launch.
* **CATEGORY** (requirement 3) — *who or what must act?* A reason code maps to exactly one category, so an
  operational dashboard can route "the host refused" away from "our own state disagrees with itself".
* **CONTRACT** (requirement 2) — *what had to be true before, during and after?* Held as data next to the
  implementation rather than only in prose, so a stage cannot quietly drift away from its stated invariant.

This module is pure vocabulary and policy-free arithmetic: no Windows calls, no occupancy semantics, no
store access. It is safe for every layer to import.
"""

# ── requirement 1: operation idempotency evidence ──────────────────────────────────────────────────────
#: A stage that has not been attempted at all. Never inferred from a missing record — see
#: :func:`assert_evidence_present`; absence of evidence is an integrity concern, not a NOT_STARTED.
NOT_STARTED = "NOT_STARTED"
#: A trigger was accepted. NOT a claim that the effect occurred — the whole point of the launch split.
REQUESTED = "REQUESTED"
#: This attempt performed the work and proved it.
COMPLETED = "COMPLETED"
#: The work was ALREADY done when this attempt ran, and that was proven (not assumed from an error).
#: Retries after an ambiguous failure land here; conflating it with COMPLETED would hide a double-execution
#: bug, and conflating it with BLOCKED would make a safe retry look like a fault.
ALREADY_COMPLETED = "ALREADY_COMPLETED"
#: A precondition or guard refused BEFORE anything was attempted on the host. Nothing changed.
BLOCKED = "BLOCKED"
#: It was attempted and either failed or could not be proven to have succeeded.
FAILED = "FAILED"

STAGE_STATUSES = (NOT_STARTED, REQUESTED, COMPLETED, ALREADY_COMPLETED, BLOCKED, FAILED)
#: Statuses that mean "the stage's postcondition holds".
SUCCESS_STATUSES = (COMPLETED, ALREADY_COMPLETED)
#: Statuses that mean "the host was NOT touched by this attempt".
NO_EFFECT_STATUSES = (NOT_STARTED, BLOCKED, ALREADY_COMPLETED)


# ── requirement 3: failure classification ──────────────────────────────────────────────────────────────
#: Deployment/registration is wrong or absent — an operator fixes the configuration, nothing is corrupt.
CONFIGURATION = "CONFIGURATION"
#: Two things that must agree do not (digests, generation, ownership, containment, capability, audit).
#: Never silently repaired.
INTEGRITY = "INTEGRITY"
#: We could not see enough to make a claim. Explicitly NOT "the thing is absent".
OBSERVATION = "OBSERVATION"
#: The operating system refused, failed, or did not produce the effect that was requested.
WINDOWS = "WINDOWS"
#: A human decision or intervention is required before anything can proceed.
OPERATOR = "OPERATOR"
#: Something expired, skewed, or was still busy.
TIMEOUT = "TIMEOUT"

FAILURE_CATEGORIES = (CONFIGURATION, INTEGRITY, OBSERVATION, WINDOWS, OPERATOR, TIMEOUT)

#: EVERY sanitised reason code in the bundle maps to EXACTLY ONE category. A dict makes duplication
#: impossible by construction; ``tests_lifecycle`` walks the bundle's AST and fails if any reason code
#: raised anywhere is missing here, so this map cannot silently fall behind the implementation.
REASON_CATEGORY = {
    # ── INTEGRITY: our own state, or state vs the host, disagrees ──
    "capability_violation": INTEGRITY,
    "unauthorised_namespace": INTEGRITY,
    "stage_copy_refused": INTEGRITY,
    "stage_copy_precheck_failed": INTEGRITY,
    "stage_copy_incomplete": INTEGRITY,
    "unexpected_process_in_slot": INTEGRITY,
    "process_identity_mismatch": INTEGRITY,
    "occupancy_binding_mismatch": INTEGRITY,
    "task_definition_drift": INTEGRITY,
    "terminate_executable_unexpected": INTEGRITY,
    "terminate_scope_unbounded": INTEGRITY,
    # ADR-0016 launch-wrapper gate: the launch task now runs the fixed wrapper, so its ARGUMENT string (not a
    # beneath-the-slot executable) is what bounds it. A drift in any of these is a task-definition integrity
    # failure, exactly like the terminate-task ones above.
    "launch_executable_unexpected": INTEGRITY,
    "launch_wrapper_unscoped": INTEGRITY,
    "launch_scope_unbounded": INTEGRITY,
    "launch_grantee_missing": INTEGRITY,
    "launch_not_portable": INTEGRITY,
    "launch_inline_command": INTEGRITY,
    "approved_task_definition_missing": CONFIGURATION,
    "slot_integrity_mismatch": INTEGRITY,
    "audit_chain_corrupt": INTEGRITY,
    "impl_integrity_mismatch": INTEGRITY,
    "path_escape": INTEGRITY,
    "reparse_escape": INTEGRITY,
    "reparse_escape_after_materialise": INTEGRITY,
    "reparse_escapes_namespace": INTEGRITY,
    "reparse_point_present": INTEGRITY,
    "image_outside_slot": INTEGRITY,
    "image_not_owned": INTEGRITY,
    "executable_outside_slot": INTEGRITY,
    "forbidden_run_as_identity": INTEGRITY,
    "task_name_mismatch": INTEGRITY,
    "ownership_conflict": INTEGRITY,
    "not_owned": INTEGRITY,
    "cleanup_incomplete": INTEGRITY,
    "reparse_point_in_tree": INTEGRITY,
    "cleanup_precheck_failed": INTEGRITY,
    # WS-B open-handle probe: asked to inspect a path outside the beta slots root (defensive scope guard).
    "open_handles_path_outside_slots_root": INTEGRITY,
    # RELEASE (ADR 0014): a live process was observed in the slot at release time -> refuse to free it.
    "release_runtime_present": INTEGRITY,
    # Several processes run from one slot and none is the runtime executable: choosing one by enumeration
    # order would bind the whole termination chain to an arbitrary process.
    "ambiguous_slot_process": INTEGRITY,
    "evidence_missing": INTEGRITY,
    "bad_signature": INTEGRITY,
    "job_op_conflict": INTEGRITY,
    "nonce_replayed": INTEGRITY,

    # ── OBSERVATION: we could not see enough to make a claim ──
    "process_observation_unavailable": OBSERVATION,
    "task_observation_unavailable": OBSERVATION,
    "filesystem_observation_unavailable": OBSERVATION,
    "launch_not_observed": OBSERVATION,
    "termination_not_observed": OBSERVATION,
    "creation_time_unusable": OBSERVATION,
    "process_permission_denied": OBSERVATION,
    # B3P-2 real adapter: each of these means "the host could not be read reliably", never "absent".
    "process_enumeration_failed": OBSERVATION,
    # ADR-0015 unprivileged (Toolhelp) enumeration: a snapshot that could not be taken or fully walked is an
    # observation failure, never "no processes"; a plausible in-slot candidate that could not be attributed
    # (denied / path / owner / start-time unreadable) blocks rather than being read as absence.
    "process_snapshot_failed": OBSERVATION,
    "process_snapshot_empty": OBSERVATION,
    "process_snapshot_iteration_failed": OBSERVATION,
    "process_attribution_incomplete": OBSERVATION,
    # Several fully-attributed slot processes: a DISTINCT fail-closed state (never "absent", never a silent
    # pick-one). It blocks the lifecycle exactly like an unresolved observation.
    "multiple_matching_processes": OBSERVATION,
    # WS-B open-handle probe: Restart Manager / enumeration could not answer reliably. Blocks release.
    "handle_observation_unavailable": OBSERVATION,
    "process_open_failed": OBSERVATION,
    "process_times_unavailable": OBSERVATION,
    "volume_path_unavailable": OBSERVATION,
    "volume_identity_unavailable": OBSERVATION,
    # ADR-0015/PN two-stage normalisation: a genuine 8.3 short-name component the (least-privilege) service
    # was REQUIRED to resolve could not be resolved -> the path verdict is withheld, never a normal long
    # path. An ordinary long-form path never reaches here (Stage A is lexical, no parent listing). The old
    # blind-GetLongPathNameW code ``path_normalisation_failed`` is retired — it can no longer be raised.
    "short_name_unresolved": OBSERVATION,
    # No supported API can answer this on any Windows build — see the research findings, section 5.
    "handle_enumeration_unsupported": OBSERVATION,
    # Some process on the host could not be attributed to a location, so "nothing is running in this slot"
    # is not a claim we are entitled to make.
    "process_attribution_incomplete": OBSERVATION,
    "owner_marker_unreadable": OBSERVATION,
    "launch_precondition_unobservable": OBSERVATION,
    "tombstone_precheck_unavailable": OBSERVATION,
    "stage_copy_precheck_permission_denied": OBSERVATION,
    "task_permission_denied": OBSERVATION,
    "filesystem_permission_denied": OBSERVATION,

    # ── WINDOWS: the OS refused, failed, or did not produce the requested effect ──
    "launch_trigger_rejected": WINDOWS,
    "launch_trigger_permission_denied": WINDOWS,
    "launch_trigger_unavailable": WINDOWS,
    "terminate_trigger_rejected": WINDOWS,
    "terminate_trigger_permission_denied": WINDOWS,
    "terminate_trigger_unavailable": WINDOWS,
    "process_absent": WINDOWS,
    "process_still_running": WINDOWS,
    "tombstone_move_incomplete": WINDOWS,
    "cross_volume_move_refused": WINDOWS,
    "golden_copy_failed": WINDOWS,
    "stage_copy_failed": WINDOWS,
    "stage_copy_permission_denied": WINDOWS,
    "tombstone_move_permission_denied": WINDOWS,
    "agent_internal_error": WINDOWS,

    # ── CONFIGURATION: registration/deployment is wrong or absent ──
    "task_absent": CONFIGURATION,
    "task_definition_incomplete": CONFIGURATION,
    "terminal_path_absent": CONFIGURATION,
    "operation_not_allowed": CONFIGURATION,
    "unsupported_protocol_version": CONFIGURATION,
    "unknown_key_id": CONFIGURATION,
    "missing_signature": CONFIGURATION,
    "missing_field": CONFIGURATION,
    "malformed_request": CONFIGURATION,
    "malformed_time": CONFIGURATION,
    "bad_runtime_uuid": CONFIGURATION,
    "runtime_not_assigned": CONFIGURATION,
    "slot_binding_missing": CONFIGURATION,
    "copy_golden_not_available_off_box": CONFIGURATION,
    "launch_not_available_off_box": CONFIGURATION,
    "process_enum_not_available_off_box": CONFIGURATION,
    "stop_not_available_off_box": CONFIGURATION,
    "windows_api_unavailable": CONFIGURATION,
    "no_existing_ancestor": CONFIGURATION,
    "golden_source_unavailable": CONFIGURATION,
    "runtime_identity_required": CONFIGURATION,
    "runtime_identity_unresolvable": CONFIGURATION,

    # ── OPERATOR: a human must decide or intervene ──
    "pool_exhausted": OPERATOR,
    "allocation_blocked": OPERATOR,
    "release_proof_missing": OPERATOR,
    "quarantine_clearance_refused": OPERATOR,
    "slot_quarantined": OPERATOR,

    # ── TIMEOUT: expiry, skew, or still-busy ──
    "request_expired": TIMEOUT,
    "expiry_too_far": TIMEOUT,
    "timestamp_skew": TIMEOUT,
    "runtime_busy": TIMEOUT,
    "agent_busy": TIMEOUT,
    "agent_stopping": TIMEOUT,
}


class UnclassifiedReasonCode(Exception):
    """A reason code exists with no category. A build-time defect, surfaced by CI rather than shipped."""


def classify(reason_code: str, *, strict: bool = True) -> str:
    """Map a sanitised reason code to exactly one failure category.

    ``strict=True`` (tests, CI) raises on an unclassified code so the map cannot fall behind the code.
    ``strict=False`` (runtime) degrades to ``INTEGRITY`` — the most conservative category, because an
    outcome we cannot even classify is by definition a disagreement — and callers record
    ``classification_complete=False`` so the degradation is visible rather than mistaken for a real
    classification.
    """
    if not reason_code:
        return ""
    try:
        return REASON_CATEGORY[reason_code]
    except KeyError:
        if strict:
            raise UnclassifiedReasonCode(reason_code)
        return INTEGRITY


def is_classified(reason_code: str) -> bool:
    return not reason_code or reason_code in REASON_CATEGORY


# ── requirement 2: per-stage contracts ─────────────────────────────────────────────────────────────────
#: Preconditions / invariant / postconditions per mutating stage, held as DATA beside the implementation.
#: ``tests_lifecycle`` asserts every mutating stage has an entry and that the statuses a stage declares are
#: the statuses it can actually produce — so a stage that grows a new outcome cannot keep an old contract.
STAGE_CONTRACTS = {
    "stage_copy": {
        "preconditions": (
            "capability is MUTATING",
            "slot input is the authorised fixed slot namespace",
            "golden source digest and manifest version match the approved values",
            "destination is absent, or already present with a matching digest (ALREADY_COMPLETED)",
            "expected generation equals actual generation",
        ),
        "invariant": "the destination path is derived from the slot number alone and never leaves "
                     "BETA_SLOTS_ROOT\\<slot>; a reparse point on the slot directory aborts before any write",
        "postconditions": (
            "destination digest equals the approved golden digest",
            "runtime executable digest is present",
            "portable marker present",
            "ownership marker present",
            "no partial copy is left eligible for launch",
        ),
        "statuses": (COMPLETED, ALREADY_COMPLETED, BLOCKED, FAILED),
    },
    "precheck_launch_task": {
        "preconditions": ("capability is MUTATING", "stage copy COMPLETED or ALREADY_COMPLETED",
                          "an approved task definition exists for this slot"),
        "invariant": "nothing is triggered yet, and the task is never repaired — drift is a refusal",
        "postconditions": ("the installed launch task matches its approved definition field for field, "
                           "and is enabled",),
        "statuses": (COMPLETED, BLOCKED),
    },
    "precheck_terminate_task": {
        "preconditions": ("capability is MUTATING", "an approved task definition exists for this slot's "
                          "terminate task"),
        "invariant": "nothing is triggered yet, and the task is never repaired - drift is a refusal. The "
                     "terminate task is the one that can reach a process, so it is the one that must be "
                     "proven unchanged before it runs",
        "postconditions": ("the installed terminate task matches its approved definition field for field",),
        "statuses": (COMPLETED, BLOCKED),
    },
    "request_launch": {
        "preconditions": ("capability is MUTATING", "launch-task verification COMPLETED",
                          "slot input is authorised"),
        "invariant": "the occupancy binding is unchanged; only the fixed per-slot launch task is triggered "
                     "and no process identity is asserted",
        "postconditions": ("a REQUESTED record exists carrying no process facts",),
        # No BLOCKED: an unauthorised slot input RAISES here rather than returning a record, and a refused
        # trigger was still attempted. The contract test enforces this against the implementation.
        "statuses": (REQUESTED, FAILED),
    },
    "confirm_launch": {
        "preconditions": ("capability is MUTATING", "launch was REQUESTED"),
        "invariant": "same slot, same occupancy; observation performs no state change",
        "postconditions": ("either COMPLETED with process-birth evidence (pid + creation FILETIME + image "
                           "digest + containment + SID + session), or FAILED with the observation state",),
        "statuses": (COMPLETED, FAILED),
    },
    "request_terminate": {
        "preconditions": ("capability is MUTATING", "slot input is authorised"),
        "invariant": "only the fixed per-slot terminate task is triggered; no process is signalled directly",
        "postconditions": ("a REQUESTED record exists; success of the trigger is never success of the stop",),
        "statuses": (REQUESTED, FAILED),
    },
    "confirm_terminated": {
        "preconditions": ("capability is MUTATING", "birth evidence from the launch is available"),
        "invariant": "same slot, same occupancy; an unobservable process is never reported as terminated",
        "postconditions": ("COMPLETED only when the slot process is ABSENT; a surviving process is "
                           "process_still_running and a different process is unexpected_process_in_slot",),
        "statuses": (COMPLETED, FAILED),
    },
    "tombstone": {
        "preconditions": ("capability is MUTATING", "process confirmed terminated",
                          "destination is beneath this slot's tombstone root"),
        "invariant": "a MOVE within one volume; never a delete, never a copy+delete, never another slot's "
                     "tombstone history",
        "postconditions": ("the slot directory no longer exists and its contents are retained under the "
                           "tombstone root",),
        "statuses": (COMPLETED, ALREADY_COMPLETED, BLOCKED, FAILED),
    },
    "precheck_cleanup": {
        "preconditions": ("capability is MUTATING", "process confirmed terminated"),
        "invariant": "nothing has been moved yet — this stage exists so that a teardown which cannot "
                     "complete costs nothing",
        "postconditions": ("all four pre-move proofs hold, or the missing ones are named and the move is "
                           "not attempted",),
        "statuses": (COMPLETED, BLOCKED),
    },
    "verify_cleanup": {
        "preconditions": ("capability is MUTATING", "tombstone COMPLETED or ALREADY_COMPLETED"),
        "invariant": "generation has NOT advanced — cleanup runs before release, so an advanced generation "
                     "means the release protocol ran out of order",
        "postconditions": ("all six cleanup proofs hold, or the missing ones are named in the evidence",),
        "statuses": (COMPLETED, FAILED),
    },
}

MUTATING_STAGES = tuple(STAGE_CONTRACTS)


# ── requirement 5: evidence completeness ───────────────────────────────────────────────────────────────
class EvidenceMissing(Exception):
    """A stage ran but produced no usable evidence.

    Treated as an INTEGRITY concern rather than an absence: a lifecycle in which an operation was recorded
    but left no evidence is indistinguishable from one that was tampered with, so it is never accepted as
    "probably fine".
    """

    def __init__(self, operation: str = "", detail: str = ""):
        self.operation, self.detail = operation, detail
        self.reason_code = "evidence_missing"
        super().__init__("evidence_missing")


#: The fields every stage result must carry, whatever the outcome. Success and failure are BOTH first-class
#: audit events, so this is checked on the failure paths too.
REQUIRED_ATTESTATION_KEYS = ("slot", "operation", "outcome", "reason_code", "stage_status",
                             "failure_category", "evidence_digest", "primitive_version")


def assert_evidence_present(result, operation: str = "") -> dict:
    """Verify a stage result is a complete, auditable evidence record — success or failure alike."""
    if not isinstance(result, dict):
        raise EvidenceMissing(operation, "no result")
    att = result.get("attestation")
    if not isinstance(att, dict):
        raise EvidenceMissing(operation, "no attestation")
    for key in REQUIRED_ATTESTATION_KEYS:
        if key not in att:
            raise EvidenceMissing(att.get("operation") or operation, f"missing {key}")
    if not att.get("evidence_digest"):
        raise EvidenceMissing(att.get("operation") or operation, "empty evidence digest")
    if att.get("stage_status") not in STAGE_STATUSES:
        raise EvidenceMissing(att.get("operation") or operation, "unknown stage status")
    if "evidence" not in result:
        raise EvidenceMissing(att.get("operation") or operation, "no evidence body")
    return att
