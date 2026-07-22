"""CVM-Inc-3 B3P-2 — MUTATING Windows primitives (stages 4–9).

Every function here declares MUTATING capability and calls :func:`win_primitives.require_mutating`, so a
future edit that invokes one from an observation path fails loudly instead of quietly writing to the
operator's host.

The layer stays ignorant in exactly the same way as the read-only stages: it receives a slot number, a
fixed slot directory and fixed task names. It never learns a runtime UUID, generation, occupancy id,
ProvisioningJob, tenant or entitlement — those are bound to the local attestation ABOVE this layer.

Two semantics are load-bearing and deliberately not shortened:

* **Launch is two records.** A trigger being accepted is NOT evidence that MT5 started; only observed
  process-birth evidence completes a launch.
* **Stop success means the process is ABSENT.** A terminate task can succeed while the process lives on.
"""
from lib.mgmt_agent_core import AgentError, is_beneath
from lifecycle import ALREADY_COMPLETED, BLOCKED, COMPLETED, FAILED, REQUESTED
from win_primitives import (ABSENT, BETA_SLOTS_ROOT, FORBIDDEN_FRAGMENTS, PRESENT_VALID,
                            PrimitiveContext, SlotInput, UnauthorisedNamespace,
                            assert_authorised_slot_input, attest, inspect_task, observe_process,
                            require_mutating)

#: Tombstoned runtimes are retained here — inside the beta namespace, never beside the operator's estate.
BETA_TOMBSTONES_ROOT = r"C:\GuvFX\beta\tombstones"


class StageCopyRefused(AgentError):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("stage_copy_refused")


class LaunchNotObserved(AgentError):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("launch_not_observed")


class TerminationNotObserved(AgentError):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("termination_not_observed")


class CleanupIncomplete(AgentError):
    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__("cleanup_incomplete")


# ── stage 4: golden-runtime stage copy ─────────────────────────────────────────────────────────────────
STAGE_PRE_CHECKS = ("source_digest_matches", "source_manifest_version_matches", "destination_absent",
                    "destination_beneath_slot", "destination_not_reparse", "generation_matches")
STAGE_POST_CHECKS = ("destination_digest_matches", "executable_digest_matches", "portable_marker_present",
                     "ownership_marker_present")


def stage_copy(win, ctx: PrimitiveContext, si: SlotInput, *, expected_source_digest,
               expected_source_manifest_version, expected_generation, actual_generation,
               owner_marker, observed_at=None) -> dict:
    """Copy the golden runtime into the fixed slot directory, with integrity proven on BOTH sides.

    **No partial copy may ever proceed to launch:** every post-check must pass, and a failure leaves the
    caller with a refusal rather than a "probably fine" directory.

    ``owner_marker`` is an OPAQUE string. The stage writes it and never parses it — its content is
    occupancy identity, which this layer is forbidden to understand. It is written here rather than by the
    caller because ``ownership_marker_present`` is one of the four post-checks: a caller writing it
    afterwards could never satisfy its own stage, and the marker would have no stage record of its own.
    """
    require_mutating(ctx, "stage_copy")
    assert_authorised_slot_input(si)
    pre = {}
    try:
        src = win.golden_source_info()
        pre["destination_absent"] = not win.path_exists(si.slot_path)
        real = win.real_path(rf"{BETA_SLOTS_ROOT}\{si.slot}")
    except PermissionError:
        return _fail(si, "stage_copy", "stage_copy_precheck_permission_denied", {}, observed_at,
                     status=BLOCKED)
    except Exception:
        # Nothing validates the golden image before this point, so a wrong or unreadable
        # BETA_AGENT_GOLDEN_DIR first surfaces HERE — as a stage refusal, not as an escaped exception.
        return _fail(si, "stage_copy", "golden_source_unavailable", {}, observed_at, status=BLOCKED)
    pre["source_digest_matches"] = bool(src) and src.get("digest") == expected_source_digest
    pre["source_manifest_version_matches"] = bool(src) and bool(src.get("manifest_version")) and \
        src.get("manifest_version") == expected_source_manifest_version
    # Containment is asserted against the FIXED roots, not against the destination's own parent — the
    # latter is a tautology (every path is beneath its own parent) and would record a check that proved
    # nothing while reading as if it had.
    slot_dir = rf"{BETA_SLOTS_ROOT}\{si.slot}"
    pre["destination_beneath_slot"] = is_beneath(si.slot_path, slot_dir)
    # The slot directory is created ONCE by the operator at the install gate, with the slot identity's ACL.
    # Requiring it to exist does two things: it stops the agent materialising into a slot nobody
    # provisioned (no identity, no ACL, no tasks — robocopy would happily create the whole chain), and it
    # makes the reparse check meaningful. Treating "could not resolve" as a pass would leave the guard
    # vacuously satisfied exactly when the directory is absent.
    pre["destination_not_reparse"] = real is not None and is_beneath(real, BETA_SLOTS_ROOT)
    pre["generation_matches"] = int(expected_generation) == int(actual_generation)
    failed_pre = [k for k in STAGE_PRE_CHECKS if not pre.get(k)]

    if failed_pre:
        # ALREADY_COMPLETED, proven — not assumed. A retry after an ambiguous failure finds the destination
        # present; that is only success if the EXISTING destination passes every post-check, and if the sole
        # failed precondition is its presence. Any other failed precondition (wrong generation, wrong golden
        # source, a reparse point) still blocks, however complete the directory looks.
        if failed_pre == ["destination_absent"]:
            existing = _post_checks(win, si, expected_source_digest)
            if not existing["failed"]:
                return _ok(si, "stage_copy",
                           {"pre_checks": pre, "post_checks": existing["checks"], "idempotent": True},
                           observed_at, status=ALREADY_COMPLETED)
        return _fail(si, "stage_copy", "stage_copy_precheck_failed",
                     {"pre_checks": pre, "failed": failed_pre}, observed_at, status=BLOCKED)

    try:
        win.copy_golden(si.slot_path)
        # The marker goes down INSIDE the staged tree, after the copy and before the proof, so the same
        # stage that claims the runtime is complete is the one that claims it is owned.
        win.write_owner_tag(si.slot_path, owner_marker)
    except PermissionError:
        return _fail(si, "stage_copy", "stage_copy_permission_denied",
                     {"pre_checks": pre}, observed_at)
    except Exception:
        return _fail(si, "stage_copy", "stage_copy_failed", {"pre_checks": pre}, observed_at)

    post = _post_checks(win, si, expected_source_digest)
    if post["failed"]:
        # Partial or corrupt copy: refuse, and say so — never hand this to launch.
        return _fail(si, "stage_copy", "stage_copy_incomplete",
                     {"pre_checks": pre, "post_checks": post["checks"], "failed": post["failed"]},
                     observed_at)
    return _ok(si, "stage_copy", {"pre_checks": pre, "post_checks": post["checks"]}, observed_at)


def _post_checks(win, si: SlotInput, expected_source_digest) -> dict:
    """Evaluate the four destination integrity checks. Used after a copy AND to prove an existing
    destination is genuinely complete before calling a retry ALREADY_COMPLETED."""
    dest = win.destination_info(si.slot_path) or {}
    checks = {
        "destination_digest_matches": dest.get("digest") == expected_source_digest,
        "executable_digest_matches": bool(dest.get("executable_digest")),
        "portable_marker_present": bool(dest.get("portable_marker")),
        "ownership_marker_present": bool(dest.get("ownership_marker")),
    }
    return {"checks": checks, "failed": [k for k in STAGE_POST_CHECKS if not checks.get(k)]}


# ── stage 4b: launch-task verification (F3 — REQUIRED before any trigger) ──────────────────────────────
def precheck_launch_task(win, ctx: PrimitiveContext, si: SlotInput, *, approved_definition,
                         observed_at=None) -> dict:
    """Assert the INSTALLED launch task still matches its APPROVED definition, before it is triggered.

    Same shape as :func:`precheck_cleanup`, and for the same reason: the check that decides whether an
    irreversible action may happen runs BEFORE it, so a refusal costs nothing.

    Having ``inspect_task`` and ``assert_task_matches_approved`` available was not sufficient — nothing
    called them, so the agent would have triggered a task without ever asserting what that task now does.
    A task whose executable, principal, logon type or run level has been changed since approval is a
    different task, and triggering it would launch something the platform never approved under an identity
    it never approved.

    The agent NEVER repairs a task. Drift is a refusal.
    """
    require_mutating(ctx, "precheck_launch_task")
    assert_authorised_slot_input(si)
    from occupancy import TaskDefinitionDrift, assert_task_matches_approved
    obs = inspect_task(win, si, which="launch", observed_at=observed_at)
    outcome = obs["attestation"]["outcome"]
    if outcome != PRESENT_VALID:
        return _fail(si, "precheck_launch_task",
                     obs["attestation"]["reason_code"] or "task_observation_unavailable",
                     {"phase": "PRE_TRIGGER", "observation": obs["attestation"]}, observed_at,
                     status=BLOCKED)
    installed = obs["evidence"]
    try:
        assert_task_matches_approved(approved_definition, installed)
    except TaskDefinitionDrift as drift:
        return _fail(si, "precheck_launch_task", "task_definition_drift",
                     {"phase": "PRE_TRIGGER", "differing": getattr(drift, "detail", "")}, observed_at,
                     status=BLOCKED)
    return _ok(si, "precheck_launch_task",
               {"phase": "PRE_TRIGGER", "definition_digest": installed.get("definition_digest"),
                "portable_switch": installed.get("portable_switch"),
                "run_as_identity": installed.get("run_as_identity")}, observed_at)


# ── stage 5: fixed-task launch trigger (REQUESTED) ─────────────────────────────────────────────────────
def request_launch(win, ctx: PrimitiveContext, si: SlotInput, *, observed_at=None) -> dict:
    """Trigger the fixed per-slot launch task. Emits the **REQUESTED** record ONLY.

    A trigger being accepted is not evidence that MT5 started; :func:`confirm_launch` is what completes a
    launch. This function deliberately reports no process facts.
    """
    require_mutating(ctx, "request_launch")
    assert_authorised_slot_input(si)
    try:
        accepted = win.run_task(si.launch_task)
    except PermissionError:
        return _fail(si, "request_launch", "launch_trigger_permission_denied", {}, observed_at)
    except Exception:
        return _fail(si, "request_launch", "launch_trigger_unavailable", {}, observed_at)
    if not accepted:
        return _fail(si, "request_launch", "launch_trigger_rejected",
                     {"task": si.launch_task}, observed_at)
    return _ok(si, "request_launch", {"phase": "REQUESTED", "trigger_accepted": True}, observed_at,
               status=REQUESTED)


# ── stage 6: launch confirmation / verification via process-birth evidence ─────────────────────────────
def confirm_launch(win, ctx: PrimitiveContext, si: SlotInput, *, observed_at=None) -> dict:
    """Emit the **OBSERVED** record: the process actually exists, contained, with usable birth evidence.

    Only this completes a launch. Observation itself is read-only, but confirmation belongs to the
    mutating lifecycle, so the mutating capability is required to record it.
    """
    require_mutating(ctx, "confirm_launch")
    obs = observe_process(win, si, observed_at=observed_at)
    outcome = obs["attestation"]["outcome"]
    if outcome != PRESENT_VALID:
        return _fail(si, "confirm_launch", obs["attestation"]["reason_code"] or "launch_not_observed",
                     {"phase": "OBSERVED", "observation": obs["attestation"]}, observed_at)
    ev = obs["evidence"]
    birth = {
        "pid": ev["pid"], "created_at_filetime": ev["created_at_filetime"],
        "image_digest": ev.get("image_digest"),
        "executable_containment_verified": ev["image_containment_verified"],
        "user_sid": ev.get("user_sid"), "session_id": ev.get("session_id"), "slot": si.slot,
    }
    return _ok(si, "confirm_launch", {"phase": "OBSERVED", "birth": birth}, observed_at)


# ── stage 7: fixed-task terminate trigger + confirmation ───────────────────────────────────────────────
def request_terminate(win, ctx: PrimitiveContext, si: SlotInput, *, observed_at=None) -> dict:
    """Trigger the fixed per-slot terminate task. REQUESTED only — never treated as success."""
    require_mutating(ctx, "request_terminate")
    assert_authorised_slot_input(si)
    try:
        accepted = win.run_task(si.terminate_task)
    except PermissionError:
        return _fail(si, "request_terminate", "terminate_trigger_permission_denied", {}, observed_at)
    except Exception:
        return _fail(si, "request_terminate", "terminate_trigger_unavailable", {}, observed_at)
    if not accepted:
        return _fail(si, "request_terminate", "terminate_trigger_rejected", {}, observed_at)
    return _ok(si, "request_terminate", {"phase": "REQUESTED", "trigger_accepted": True}, observed_at,
               status=REQUESTED)


def confirm_terminated(win, ctx: PrimitiveContext, si: SlotInput, *, birth, observed_at=None) -> dict:
    """STOP succeeds ONLY when the process is genuinely **absent**.

    A terminate task can report success while the process lives on, so this observes the slot again. If a
    process is still present it is compared against the recorded birth evidence: the SAME process still
    running is a plain failure; a DIFFERENT process (PID reuse, or something else in the slot) is an
    identity mismatch and is escalated rather than mistaken for success.
    """
    require_mutating(ctx, "confirm_terminated")
    obs = observe_process(win, si, observed_at=observed_at)
    outcome = obs["attestation"]["outcome"]
    if outcome == ABSENT:
        return _ok(si, "confirm_terminated", {"process_absent": True}, observed_at)
    if outcome != PRESENT_VALID:
        # Could not observe -> cannot claim termination. Never assume absence from an unreadable query.
        return _fail(si, "confirm_terminated", obs["attestation"]["reason_code"] or
                     "termination_not_observed", {"observation": obs["attestation"]}, observed_at)
    ev = obs["evidence"]
    from occupancy import ProcessIdentityMismatch, assert_same_process
    if not (birth or {}).get("pid"):
        # No usable birth evidence (the pre-stop observation failed) AND a process is present. We cannot
        # say whether it is the one we launched, so we say the only safe thing: it is still running.
        # Reaching assert_same_process here would raise KeyError, escape the stage entirely, and leave a
        # REQUESTED row with no confirmation row while the runtime kept trading.
        return _fail(si, "confirm_terminated", "process_still_running",
                     {"process_absent": False, "birth_evidence_missing": True}, observed_at)
    observed_identity = {
        "pid": ev["pid"], "created_at": ev["created_at_filetime"],
        "image_digest": ev.get("image_digest"),
        "executable_containment_verified": ev["image_containment_verified"],
        "user_sid": ev.get("user_sid"), "session_id": ev.get("session_id"), "slot": si.slot,
    }
    recorded = dict(birth or {})
    recorded["created_at"] = recorded.pop("created_at_filetime", recorded.get("created_at"))
    try:
        assert_same_process(recorded, observed_identity)
        return _fail(si, "confirm_terminated", "process_still_running",
                     {"process_absent": False}, observed_at)
    except ProcessIdentityMismatch:
        return _fail(si, "confirm_terminated", "unexpected_process_in_slot",
                     {"process_absent": False}, observed_at)


# ── stage 8: tombstone move ────────────────────────────────────────────────────────────────────────────
def assert_authorised_tombstone_dir(si: SlotInput, tombstone_dir) -> None:
    """The destination is caller-supplied, so it is VALIDATED, never trusted.

    Without this, ``tombstone`` would be a primitive that moves a directory to an arbitrary path — the one
    place in the mutating set where a caller could steer a write outside the beta namespace (or on top of
    the operator's estate). Containment is asserted against this slot's own tombstone directory, so a bug
    in the upper layer cannot make slot 2 write into slot 3's history either.
    """
    dest = str(tombstone_dir or "").replace("/", "\\")
    low = dest.lower()
    if any(part == ".." for part in low.split("\\")):
        raise UnauthorisedNamespace("path traversal component")
    if not is_beneath(dest, rf"{BETA_TOMBSTONES_ROOT}\{si.slot}"):
        raise UnauthorisedNamespace("tombstone_dir outside this slot's tombstone root")
    for frag in FORBIDDEN_FRAGMENTS:
        if frag in low:
            raise UnauthorisedNamespace("forbidden path fragment")


def tombstone(win, ctx: PrimitiveContext, si: SlotInput, *, tombstone_dir, observed_at=None) -> dict:
    """MOVE the slot runtime directory to the tombstone area. Never a delete, never cross-volume."""
    require_mutating(ctx, "tombstone")
    assert_authorised_slot_input(si)
    assert_authorised_tombstone_dir(si, tombstone_dir)
    try:
        if not win.path_exists(si.slot_path):
            return _ok(si, "tombstone", {"already_absent": True, "idempotent": True}, observed_at,
                       status=ALREADY_COMPLETED)
        if not win.same_volume(si.slot_path, tombstone_dir):
            return _fail(si, "tombstone", "cross_volume_move_refused", {}, observed_at, status=BLOCKED)
    except Exception:
        # Unwrapped, these escape the stage and become agent_internal_error with NO stage record —
        # the one state record_stage is supposed to make impossible.
        return _fail(si, "tombstone", "tombstone_precheck_unavailable", {}, observed_at, status=BLOCKED)
    try:
        win.move_dir(si.slot_path, tombstone_dir)
    except PermissionError:
        return _fail(si, "tombstone", "tombstone_move_permission_denied", {}, observed_at)
    except Exception:
        return _fail(si, "tombstone", "tombstone_move_incomplete", {}, observed_at)
    moved = not win.path_exists(si.slot_path)
    if not moved:
        return _fail(si, "tombstone", "tombstone_move_incomplete", {}, observed_at)
    return _ok(si, "tombstone", {"moved": True}, observed_at)


# ── stage 9: cleanup and rollback validation ───────────────────────────────────────────────────────────
CLEANUP_PROOFS = ("slot_directory_empty", "no_task_running", "no_runtime_process",
                  "no_runtime_handles", "audit_complete", "generation_unchanged")


#: Proofs that can be evaluated BEFORE the tombstone move, and therefore before anything irreversible has
#: happened. Running them first means a doomed teardown costs nothing.
PRE_MOVE_PROOFS = ("no_task_running", "no_runtime_process", "no_runtime_handles", "generation_unchanged")


def precheck_cleanup(win, ctx: PrimitiveContext, si: SlotInput, *, generation_before, generation_now,
                     observed_at=None) -> dict:
    """Evaluate the pre-move-provable cleanup proofs BEFORE the tombstone move.

    Without this the sequence was: move the directory (irreversible), then discover a proof cannot hold,
    then fail — leaving the slot moved, the operation errored and the runtime dir under the tombstone root.
    Since ``no_runtime_handles`` currently can never hold (no supported API answers it), that was not a
    corner case: it was every teardown.
    """
    require_mutating(ctx, "precheck_cleanup")
    assert_authorised_slot_input(si)
    proofs = _cleanup_proofs(win, si, generation_before, generation_now, observed_at)
    missing = [k for k in PRE_MOVE_PROOFS if not proofs.get(k)]
    evidence = {"proofs": {k: proofs[k] for k in PRE_MOVE_PROOFS}, "missing": missing, "phase": "PRE_MOVE"}
    if missing:
        return _fail(si, "precheck_cleanup", "cleanup_precheck_failed", evidence, observed_at,
                     status=BLOCKED)
    return _ok(si, "precheck_cleanup", evidence, observed_at)


def _cleanup_proofs(win, si, generation_before, generation_now, observed_at) -> dict:
    proofs = {}
    proofs["slot_directory_empty"] = not win.path_exists(si.slot_path)
    try:
        # BOTH tasks: a terminate task still running is as much "not finished" as a launch task is.
        proofs["no_task_running"] = not (bool(win.task_running(si.launch_task))
                                         or bool(win.task_running(si.terminate_task)))
    except Exception:
        proofs["no_task_running"] = False            # unreadable -> cannot claim clean
    obs = observe_process(win, si, observed_at=observed_at)
    proofs["no_runtime_process"] = obs["attestation"]["outcome"] == ABSENT
    try:
        proofs["no_runtime_handles"] = not bool(win.open_handles(si.slot_path))
    except Exception:
        proofs["no_runtime_handles"] = False
    proofs["generation_unchanged"] = int(generation_before) == int(generation_now)
    return proofs


def verify_cleanup(win, ctx: PrimitiveContext, si: SlotInput, *, generation_before, generation_now,
                   audit_complete, observed_at=None) -> dict:
    """Prove the slot is genuinely clean BEFORE release, and emit evidence either way.

    ``generation_unchanged`` is deliberately part of cleanup: the generation must NOT advance until the
    release protocol runs, so an advanced generation here means release happened out of order.
    """
    require_mutating(ctx, "verify_cleanup")
    assert_authorised_slot_input(si)
    proofs = _cleanup_proofs(win, si, generation_before, generation_now, observed_at)
    proofs["audit_complete"] = bool(audit_complete)
    missing = [k for k in CLEANUP_PROOFS if not proofs.get(k)]
    evidence = {"proofs": proofs, "missing": missing}
    if missing:
        return _fail(si, "verify_cleanup", "cleanup_incomplete", evidence, observed_at)
    return _ok(si, "verify_cleanup", evidence, observed_at)


# ── attestation helpers ────────────────────────────────────────────────────────────────────────────────
def _result(si, operation, *, status, reason_code, evidence, observed_at):
    """Build the one evidence record every stage emits, whatever the outcome (requirement 5).

    ``outcome`` stays the coarse success/failure signal existing callers consume; ``stage_status`` carries
    the finer lifecycle meaning, including the COMPLETED vs ALREADY_COMPLETED distinction that matters on a
    retry after an ambiguous failure.
    """
    outcome = "success" if status in (COMPLETED, ALREADY_COMPLETED, REQUESTED) else "failure"
    evidence = dict(evidence or {})
    evidence["stage_status"] = status
    return {"attestation": attest(slot=si.slot, operation=operation, outcome=outcome,
                                  reason_code=reason_code, evidence=evidence, observed_at=observed_at,
                                  stage_status=status),
            "evidence": evidence}


def _ok(si, operation, evidence, observed_at, status=COMPLETED):
    return _result(si, operation, status=status, reason_code="", evidence=evidence,
                   observed_at=observed_at)


def _fail(si, operation, reason_code, evidence, observed_at, status=FAILED):
    return _result(si, operation, status=status, reason_code=reason_code, evidence=evidence,
                   observed_at=observed_at)
