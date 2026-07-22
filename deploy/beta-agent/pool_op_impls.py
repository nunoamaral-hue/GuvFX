"""CVM-Inc-3 B3P-2 — POOL-AWARE operation implementations (slot-aware path resolution + lifecycle).

This is the layer the reviewer's ordering puts between the Windows primitives and the agent core:

    agent core  →  SlotResolver (UUID → occupancy → fixed slot path)
                →  PoolOpImplementations (lifecycle policy, integrity gate, evidence)
                →  win_mutations / win_primitives (stages)
                →  WindowsOps adapter (the only Win32 in the system)

Everything the primitive layer is forbidden to know lives HERE: runtime UUIDs, generations, occupancy ids,
the slot store, the integrity gate, sequencing and evidence recording. The primitives below receive a slot
number and nothing else.

Two properties are worth stating plainly because they are easy to lose in a refactor:

* **Only MATERIALISE may allocate a slot.** Every other operation resolves by lookup, so a stray or
  replayed request for an unknown runtime cannot consume pool capacity.
* **Every mutating operation passes the integrity gate first** — database, ownership marker, runtime UUID,
  slot and generation must agree, and the generation must be monotonic — and any disagreement quarantines
  the slot rather than repairing it.
"""
from lib.mgmt_agent_core import AgentError
from lifecycle import ALREADY_COMPLETED, COMPLETED, REQUESTED, assert_evidence_present
from stores import (build_verification_evidence, format_owner_marker, occupancy_id, remote_evidence)
from win_mutations import (BETA_TOMBSTONES_ROOT, confirm_launch, confirm_terminated, precheck_cleanup,
                           precheck_launch_task, request_launch, request_terminate, stage_copy, tombstone,
                           verify_cleanup)
from win_primitives import (ABSENT, BETA_SLOTS_ROOT, MUTATING_CONTEXT, PRESENT_VALID,
                            READ_ONLY_CONTEXT, inspect_filesystem, observe_process, resolve_slot_input)


def _default_sleep(seconds):
    import time
    time.sleep(seconds)


class RuntimeNotAssigned(AgentError):
    """An operation other than MATERIALISE named a runtime that holds no slot.

    Deliberately NOT an implicit allocation: silently assigning a slot here would let a replayed or
    out-of-order request consume pool capacity for a runtime the backend never provisioned.
    """

    def __init__(self):
        super().__init__("runtime_not_assigned")


class SlotResolver:
    """Resolves a runtime UUID to its occupancy and the FIXED path derived from the slot number.

    This is the "slot-aware path resolution" step: the path is a function of the slot alone. No caller
    value — not even the runtime UUID — appears in it, so a launch target can never be steered by anything
    that crossed the network.
    """

    ALLOCATING_OPERATIONS = frozenset({"MATERIALISE"})

    def __init__(self, slot_store, *, slots_root: str = BETA_SLOTS_ROOT, now_fn=None):
        self.slot_store = slot_store
        self.slots_root = slots_root
        self.now_fn = now_fn or (lambda: 0)

    def resolve(self, *, runtime_uuid: str, operation: str) -> dict:
        if operation in self.ALLOCATING_OPERATIONS:
            slot, generation = self.slot_store.assign(runtime_uuid, self.now_fn())
        else:
            pair = self.slot_store.lookup(runtime_uuid)
            if pair is None:
                raise RuntimeNotAssigned()
            slot, generation = pair
        si = resolve_slot_input(slot)            # derived from the NUMBER; validated against the namespace
        return {
            "slot": int(slot),
            "generation": int(generation),
            "occupancy_id": occupancy_id(slot, generation),
            "slot_input": si,
            "slot_path": si.slot_path,
            "slots_root": self.slots_root,
        }


class PoolOpImplementations:
    """The five allowlisted operations, expressed as sequences of the approved mutating stages."""

    #: How many times to re-observe after a trigger before concluding, and how long to wait between.
    #: A scheduler trigger is ASYNCHRONOUS ([R:run-is-request-not-proof]): observing in the statement after
    #: the trigger reports launch_not_observed for a normal launch while an orphan MT5 comes up that the
    #: platform believes never started. The real values belong to the viability trial (trial item 7); these
    #: are a starting point, and the poll distinguishes "not yet" from "not at the deadline".
    LAUNCH_ATTEMPTS, STOP_ATTEMPTS, POLL_SECONDS = 20, 30, 1.0

    def __init__(self, win, slot_store, *, golden_digest: str, golden_manifest_version: str,
                 approved_tasks: dict, now_fn=None, manifest_version: str = "", protocol_version=None,
                 sleep_fn=None):
        self.win = win
        self.slot_store = slot_store
        self.golden_digest = golden_digest
        self.golden_manifest_version = golden_manifest_version
        #: ``{task_name: approved 7-field definition}``, loaded at startup from the operator's approval
        #: file. A slot whose launch task has no approved definition can never launch.
        self.approved_tasks = dict(approved_tasks or {})
        self.now_fn = now_fn or (lambda: 0)
        self.manifest_version = manifest_version
        self.protocol_version = protocol_version
        self.sleep_fn = sleep_fn or _default_sleep

    def as_dict(self) -> dict:
        return {"MATERIALISE": self.materialise, "START": self.start, "VERIFY": self.verify,
                "STOP": self.stop, "TOMBSTONE": self.tombstone}

    # ── gate + evidence plumbing ──
    def _binding(self, context) -> dict:
        if not context or "slot_input" not in context:
            raise AgentError("slot_binding_missing")
        return context

    def _marker(self, si):
        """Returns the raw marker, or None when the slot directory is genuinely absent.

        An UNREADABLE marker raises instead of returning None. The difference matters: None flows into the
        integrity gate as a mismatch and QUARANTINES the slot, so a transient permission error on a
        perfectly healthy running runtime would be recorded as corruption and need operator recovery.
        """
        try:
            if not self.win.path_exists(si.slot_path):
                return None
            return self.win.read_owner_tag(si.slot_path)
        except Exception as exc:
            raise AgentError("owner_marker_unreadable") from exc

    def _gate(self, context, runtime_uuid, *, require_marker: bool = True,
              allow_torn_down: bool = False):
        """The pre-mutation integrity gate. ``require_marker=False`` applies ONLY to the first stage of a
        fresh occupancy, where the marker does not exist yet because stage-copy is what writes it; the slot
        must then be genuinely empty, and quarantine/monotonicity are still enforced."""
        b = self._binding(context)
        si, slot, generation = b["slot_input"], b["slot"], b["generation"]
        marker_raw = self._marker(si)
        if allow_torn_down and marker_raw is None:
            # Two recoverable states share this shape, and BOTH need TOMBSTONE to work:
            #  * the directory was moved and cleanup then failed — the marker is legitimately gone;
            #  * a MATERIALISE was interrupted between the copy and the marker write, leaving a populated
            #    but marker-less slot that no retry could otherwise recover (MATERIALISE refuses because
            #    the destination exists, TOMBSTONE refused because the marker is missing).
            # In both, identity is proven from DURABLE STATE rather than from a file: the store must still
            # record this runtime as holding exactly this (slot, generation).
            from stores import SlotIntegrityError
            if self.slot_store.is_quarantined(slot):
                raise SlotIntegrityError()       # every other mutating path checks this; so must this one
            if self.slot_store.lookup(runtime_uuid) != (slot, generation):
                raise SlotIntegrityError()
            self.slot_store.assert_generation_monotonic(slot, generation)
            return b, si, slot, generation, None
        if marker_raw is None and not require_marker:
            from stores import SlotIntegrityError
            if self.slot_store.is_quarantined(slot):
                raise SlotIntegrityError()
            self.slot_store.assert_generation_monotonic(slot, generation)
        else:
            self.slot_store.assert_occupancy_integrity(
                runtime_uuid=runtime_uuid, slot=slot, generation=generation, marker_raw=marker_raw,
                now=self.now_fn())
        return b, si, slot, generation, marker_raw

    def _await(self, si, wanted, attempts):
        """Re-observe until the slot reaches ``wanted`` or the attempts run out. Returns the LAST
        observation either way, so the caller records what was actually seen at the deadline."""
        obs = observe_process(self.win, si, observed_at=self.now_fn())
        for _ in range(max(0, attempts - 1)):
            if obs["attestation"]["outcome"] == wanted:
                return obs
            self.sleep_fn(self.POLL_SECONDS)
            obs = observe_process(self.win, si, observed_at=self.now_fn())
        return obs

    def _record(self, slot, generation, result) -> dict:
        """Record one stage's evidence durably. Evidence completeness is asserted BEFORE the write, so an
        incomplete record is refused rather than stored."""
        att = assert_evidence_present(result)
        self.slot_store.record_stage(slot=slot, generation=generation, operation=att["operation"],
                                     attestation=att, now=self.now_fn())
        return att

    def _containment_verified(self, si) -> bool:
        """Derive ``path_containment_verified`` from an ACTUAL filesystem observation.

        It was previously the literal ``True`` at every call site — an attestation the backend consumes,
        asserted rather than observed, and reported even for a directory that had just been moved away.
        ``inspect_filesystem`` is the only code that checks per-component reparse and containment, so this
        is also what finally gives it a caller.
        """
        try:
            fs = inspect_filesystem(self.win, si, observed_at=self.now_fn())
        except Exception:
            return False
        return bool(fs["evidence"].get("containment_verified"))

    def _evidence(self, *, runtime_uuid, slot, generation, si, marker_raw, pid=None, session_id=None,
                  path_ok=False, exe_ok=False, verified_at=None, extra=None) -> dict:
        local = build_verification_evidence(
            runtime_uuid=runtime_uuid, slot=slot, generation=generation, canonical_dir=si.slot_path,
            marker_raw=marker_raw, pid=pid, session_id=session_id,
            manifest_version=self.manifest_version, protocol_version=self.protocol_version,
            verified_at=verified_at, path_containment_verified=path_ok,
            executable_containment_verified=exe_ok)
        out = remote_evidence(local)          # the full path never leaves the host
        out.update(extra or {})
        return out

    # ── MATERIALISE: stage the golden runtime into the fixed slot directory ──
    def materialise(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b, si, slot, generation, _marker = self._gate(context, runtime_uuid, require_marker=False)
        res = stage_copy(self.win, MUTATING_CONTEXT, si,
                         expected_source_digest=self.golden_digest,
                         expected_source_manifest_version=self.golden_manifest_version,
                         # Two INDEPENDENT sources: the occupancy the resolver bound, and the value read
                         # back from the store now. Passing the same variable twice made the pre-check
                         # x == x — always true, and recorded in the report as a check that had passed.
                         expected_generation=generation,
                         actual_generation=self.slot_store.generation_of(slot),
                         owner_marker=format_owner_marker(runtime_uuid, slot, generation),
                         observed_at=self.now_fn())
        att = self._record(slot, generation, res)
        if att["stage_status"] not in (COMPLETED, ALREADY_COMPLETED):
            raise AgentError(att["reason_code"] or "stage_copy_refused")
        marker_raw = self._marker(si)
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=self._containment_verified(si),
                              extra={"materialised": True,
                                     "idempotent": att["stage_status"] == ALREADY_COMPLETED})

    # ── START: trigger the fixed launch task, then PROVE the process exists ──
    def start(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b, si, slot, generation, marker_raw = self._gate(context, runtime_uuid)
        # Idempotency BEFORE triggering: a runtime already running is never launched a second time.
        already = observe_process(self.win, si, observed_at=self.now_fn())
        outcome = already["attestation"]["outcome"]
        if outcome == PRESENT_VALID:
            ev = already["evidence"]
            return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                                  marker_raw=marker_raw, pid=ev["pid"], session_id=ev.get("session_id"),
                                  path_ok=self._containment_verified(si), exe_ok=ev["image_containment_verified"],
                                  extra={"running": True, "idempotent": True})
        if outcome != ABSENT:
            # Unobservable, denied, or present-but-invalid. Falling through to a trigger here would apply
            # the design's forbidden "unobservable treated as absent" to START: a running-but-unreadable
            # runtime gets a SECOND terminal, after which two processes match the slot executable and
            # select_slot_process raises ambiguous_slot_process forever — STOP, TOMBSTONE and VERIFY all
            # permanently broken for a slot running two live terminals.
            raise AgentError(already["attestation"]["reason_code"] or "launch_precondition_unobservable")
        # F3 — task-definition verification is a GATE, not a capability that merely exists. A launch may
        # not proceed unless it has executed successfully for THIS occupancy, so it is recorded as a stage
        # of this occupancy like any other.
        approved = self.approved_tasks.get(si.launch_task)
        if not approved:
            raise AgentError("approved_task_definition_missing")
        checked = precheck_launch_task(self.win, MUTATING_CONTEXT, si, approved_definition=approved,
                                       observed_at=self.now_fn())
        chatt = self._record(slot, generation, checked)
        if chatt["stage_status"] != COMPLETED:
            raise AgentError(chatt["reason_code"] or "task_definition_drift")
        requested = request_launch(self.win, MUTATING_CONTEXT, si, observed_at=self.now_fn())
        att = self._record(slot, generation, requested)
        if att["stage_status"] != REQUESTED:
            raise AgentError(att["reason_code"] or "launch_trigger_rejected")
        self._await(si, PRESENT_VALID, self.LAUNCH_ATTEMPTS)      # settle window, then the real record
        confirmed = confirm_launch(self.win, MUTATING_CONTEXT, si, observed_at=self.now_fn())
        catt = self._record(slot, generation, confirmed)
        if catt["stage_status"] != COMPLETED:
            # A trigger that was accepted but produced no process is NOT a started runtime.
            raise AgentError(catt["reason_code"] or "launch_not_observed")
        birth = confirmed["evidence"]["birth"]
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, pid=birth["pid"],
                              session_id=birth.get("session_id"), path_ok=self._containment_verified(si),
                              exe_ok=birth["executable_containment_verified"],
                              extra={"running": True, "logged_in": False})

    # ── VERIFY: read-only ──
    def verify(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b = self._binding(context)
        si, slot, generation = b["slot_input"], b["slot"], b["generation"]
        obs = observe_process(self.win, si, observed_at=self.now_fn())
        outcome = obs["attestation"]["outcome"]
        if outcome not in (PRESENT_VALID, ABSENT):
            # VERIFY's whole job is to answer "is it running?", and it is the operation a worker calls to
            # reconcile after an ambiguous STOP. Reporting running=False for an observation that FAILED
            # would be the design's forbidden collapse in the one place it does the most damage: the
            # backend would conclude the runtime is stopped and proceed to tombstone a live terminal.
            raise AgentError(obs["attestation"]["reason_code"] or "process_observation_unavailable")
        running = outcome == PRESENT_VALID
        ev = obs["evidence"] if running else {}
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=self._marker(si), pid=ev.get("pid"),
                              session_id=ev.get("session_id"), path_ok=self._containment_verified(si),
                              exe_ok=bool(ev.get("image_containment_verified")),
                              verified_at=self.now_fn(),
                              extra={"running": running, "logged_in": False})

    # ── STOP: trigger the fixed terminate task, then PROVE the process is absent ──
    def stop(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b, si, slot, generation, marker_raw = self._gate(context, runtime_uuid)
        birth = self._birth_from_observation(si)
        requested = request_terminate(self.win, MUTATING_CONTEXT, si, observed_at=self.now_fn())
        att = self._record(slot, generation, requested)
        if att["stage_status"] != REQUESTED:
            raise AgentError(att["reason_code"] or "terminate_trigger_rejected")
        self._await(si, ABSENT, self.STOP_ATTEMPTS)
        confirmed = confirm_terminated(self.win, MUTATING_CONTEXT, si, birth=birth,
                                       observed_at=self.now_fn())
        catt = self._record(slot, generation, confirmed)
        if catt["stage_status"] != COMPLETED:
            raise AgentError(catt["reason_code"] or "termination_not_observed")
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=self._containment_verified(si), extra={"running": False})

    def _birth_from_observation(self, si) -> dict:
        """Best available identity of the process we are about to stop. An absent/unreadable process yields
        an empty birth record — ``confirm_terminated`` then relies on ABSENCE alone, which is the safe
        direction: it can only *withhold* a success claim, never manufacture one."""
        obs = observe_process(self.win, si, observed_at=self.now_fn())
        if obs["attestation"]["outcome"] != PRESENT_VALID:
            return {}
        ev = obs["evidence"]
        return {"pid": ev["pid"], "created_at_filetime": ev["created_at_filetime"],
                "image_digest": ev.get("image_digest"),
                "executable_containment_verified": ev["image_containment_verified"],
                "user_sid": ev.get("user_sid"), "session_id": ev.get("session_id"), "slot": si.slot}

    # ── TOMBSTONE: move the runtime aside, prove the slot is clean, then release it ──
    def tombstone(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b, si, slot, generation, marker_raw = self._gate(context, runtime_uuid, allow_torn_down=True)
        # Everything provable before the move is proved before the move. A teardown that cannot complete
        # then costs nothing, instead of leaving the runtime directory under the tombstone root with the
        # operation errored.
        pre = precheck_cleanup(self.win, MUTATING_CONTEXT, si, generation_before=generation,
                               generation_now=self.slot_store.generation_of(slot),
                               observed_at=self.now_fn())
        patt = self._record(slot, generation, pre)
        if patt["stage_status"] != COMPLETED:
            raise AgentError(patt["reason_code"] or "cleanup_precheck_failed")
        dest = rf"{BETA_TOMBSTONES_ROOT}\{slot}\{occupancy_id(slot, generation)}"
        moved = tombstone(self.win, MUTATING_CONTEXT, si, tombstone_dir=dest, observed_at=self.now_fn())
        matt = self._record(slot, generation, moved)
        if matt["stage_status"] not in (COMPLETED, ALREADY_COMPLETED):
            raise AgentError(matt["reason_code"] or "tombstone_move_incomplete")
        cleanup = verify_cleanup(self.win, MUTATING_CONTEXT, si, generation_before=generation,
                                 generation_now=self.slot_store.generation_of(slot),
                                 audit_complete=self._audit_complete(slot, generation),
                                 observed_at=self.now_fn())
        catt = self._record(slot, generation, cleanup)
        if catt["stage_status"] != COMPLETED:
            raise AgentError(catt["reason_code"] or "cleanup_incomplete")
        # RELEASE IS NOT PERFORMED HERE, and that is deliberate rather than an oversight.
        # ``no_mutation_lock_held`` is one of the seven release proofs, and this method runs INSIDE the
        # per-runtime mutation lock — so a release issued from here could only ever satisfy that proof by
        # lying about it. Release therefore needs its own step outside the lock; :meth:`release` below
        # implements it, but wiring it to a lifecycle operation changes the protocol surface, which is an
        # Amber decision and not mine to take. Until then TOMBSTONE reports the gap explicitly instead of
        # implying the slot is free: the slot stays assigned, so the pool exhausts after ``pool_size``
        # tombstones and the generation never advances.
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=self._containment_verified(si),
                              extra={"tombstoned": True, "released": False, "release_pending": True,
                                     "idempotent": matt["stage_status"] == ALREADY_COMPLETED})

    def release(self, *, runtime_uuid, slot, generation, no_ambiguous_provisioning_job,
                no_mutation_lock_held) -> dict:
        """Advance the generation and free the slot. **Must run OUTSIDE the mutation lock.**

        The two proofs this layer cannot observe for itself are passed in explicitly rather than assumed:
        whether the backend still holds an ambiguous ProvisioningJob for this runtime, and whether the
        caller is genuinely outside the lock. Making them parameters means a caller has to state them; a
        default of ``True`` would have let the release protocol be satisfied by omission.
        """
        evidence = {e["operation"]: e for e in self.slot_store.stage_evidence_for_occupancy(slot,
                                                                                            generation)}
        cleanup, terminated = evidence.get("verify_cleanup"), evidence.get("confirm_terminated")
        self.slot_store.assert_evidence_complete(slot, generation)
        self.slot_store.record_audit(event="tombstone_completed", runtime_uuid=runtime_uuid, slot=slot,
                                     generation=generation, operation="release", now=self.now_fn())
        moved = evidence.get("tombstone")
        proofs = {
            "runtime_process_stopped": bool(terminated and terminated["stage_status"] == COMPLETED),
            # NOT the same proof as the line above wearing a different name: confirm_terminated reaches
            # COMPLETED on the ABSENT branch, where the identity comparison never runs. Identity is proven
            # by the launch having recorded birth evidence for THIS occupancy.
            "process_identity_verified": bool(evidence.get("confirm_launch", {}).get("stage_status")
                                              == COMPLETED),
            # A FAILED tombstone stage used to satisfy this simply by existing.
            "canonical_directory_tombstoned": bool(
                moved and moved["stage_status"] in (COMPLETED, ALREADY_COMPLETED)),
            "tombstone_evidence_persisted": bool(cleanup and cleanup["stage_status"] == COMPLETED),
            "no_ambiguous_provisioning_job": bool(no_ambiguous_provisioning_job),
            "no_mutation_lock_held": bool(no_mutation_lock_held),
            "slot_release_audit_persisted": True,
        }
        self.slot_store.release_after_tombstone(runtime_uuid=runtime_uuid, slot=slot,
                                                generation=generation, proofs=proofs, now=self.now_fn())
        return {"released": True, "slot": slot, "generation": generation}

    def _audit_complete(self, slot, generation) -> bool:
        """Cleanup's ``audit_complete`` proof: this occupancy's sequence and evidence must reconcile."""
        try:
            self.slot_store.assert_evidence_complete(slot, generation)
            return True
        except Exception:
            return False


def read_only_context():
    """Exposed so callers that only observe can state so explicitly rather than passing a mutating one."""
    return READ_ONLY_CONTEXT
