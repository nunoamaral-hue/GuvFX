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
from win_mutations import (BETA_TOMBSTONES_ROOT, confirm_launch, confirm_terminated, request_launch,
                           request_terminate, stage_copy, tombstone, verify_cleanup)
from win_primitives import (BETA_SLOTS_ROOT, MUTATING_CONTEXT, PRESENT_VALID, READ_ONLY_CONTEXT,
                            observe_process, resolve_slot_input)


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

    def __init__(self, win, slot_store, *, golden_digest: str, golden_manifest_version: str,
                 now_fn=None, manifest_version: str = "", protocol_version=None):
        self.win = win
        self.slot_store = slot_store
        self.golden_digest = golden_digest
        self.golden_manifest_version = golden_manifest_version
        self.now_fn = now_fn or (lambda: 0)
        self.manifest_version = manifest_version
        self.protocol_version = protocol_version

    def as_dict(self) -> dict:
        return {"MATERIALISE": self.materialise, "START": self.start, "VERIFY": self.verify,
                "STOP": self.stop, "TOMBSTONE": self.tombstone}

    # ── gate + evidence plumbing ──
    def _binding(self, context) -> dict:
        if not context or "slot_input" not in context:
            raise AgentError("slot_binding_missing")
        return context

    def _marker(self, si):
        try:
            return self.win.read_owner_tag(si.slot_path) if self.win.path_exists(si.slot_path) else None
        except Exception:
            return None                      # unreadable marker is a MISMATCH below, never an implicit OK

    def _gate(self, context, runtime_uuid, *, require_marker: bool = True):
        """The pre-mutation integrity gate. ``require_marker=False`` applies ONLY to the first stage of a
        fresh occupancy, where the marker does not exist yet because stage-copy is what writes it; the slot
        must then be genuinely empty, and quarantine/monotonicity are still enforced."""
        b = self._binding(context)
        si, slot, generation = b["slot_input"], b["slot"], b["generation"]
        marker_raw = self._marker(si)
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

    def _record(self, slot, generation, result) -> dict:
        """Record one stage's evidence durably. Evidence completeness is asserted BEFORE the write, so an
        incomplete record is refused rather than stored."""
        att = assert_evidence_present(result)
        self.slot_store.record_stage(slot=slot, generation=generation, operation=att["operation"],
                                     attestation=att, now=self.now_fn())
        return att

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
                         expected_generation=generation, actual_generation=generation,
                         observed_at=self.now_fn())
        att = self._record(slot, generation, res)
        if att["stage_status"] not in (COMPLETED, ALREADY_COMPLETED):
            raise AgentError(att["reason_code"] or "stage_copy_refused")
        if att["stage_status"] == COMPLETED:
            # The ownership marker is written only after the copy is PROVEN complete, so a marker can never
            # vouch for a partial runtime.
            self.win.write_owner_tag(si.slot_path, format_owner_marker(runtime_uuid, slot, generation))
        marker_raw = self._marker(si)
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=True,
                              extra={"materialised": True,
                                     "idempotent": att["stage_status"] == ALREADY_COMPLETED})

    # ── START: trigger the fixed launch task, then PROVE the process exists ──
    def start(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b, si, slot, generation, marker_raw = self._gate(context, runtime_uuid)
        # Idempotency BEFORE triggering: a runtime already running is never launched a second time.
        already = observe_process(self.win, si, observed_at=self.now_fn())
        if already["attestation"]["outcome"] == PRESENT_VALID:
            ev = already["evidence"]
            return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                                  marker_raw=marker_raw, pid=ev["pid"], session_id=ev.get("session_id"),
                                  path_ok=True, exe_ok=ev["image_containment_verified"],
                                  extra={"running": True, "idempotent": True})
        requested = request_launch(self.win, MUTATING_CONTEXT, si, observed_at=self.now_fn())
        att = self._record(slot, generation, requested)
        if att["stage_status"] != REQUESTED:
            raise AgentError(att["reason_code"] or "launch_trigger_rejected")
        confirmed = confirm_launch(self.win, MUTATING_CONTEXT, si, observed_at=self.now_fn())
        catt = self._record(slot, generation, confirmed)
        if catt["stage_status"] != COMPLETED:
            # A trigger that was accepted but produced no process is NOT a started runtime.
            raise AgentError(catt["reason_code"] or "launch_not_observed")
        birth = confirmed["evidence"]["birth"]
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, pid=birth["pid"],
                              session_id=birth.get("session_id"), path_ok=True,
                              exe_ok=birth["executable_containment_verified"],
                              extra={"running": True, "logged_in": False})

    # ── VERIFY: read-only ──
    def verify(self, *, canonical_dir, runtime_uuid, base, context=None) -> dict:
        b = self._binding(context)
        si, slot, generation = b["slot_input"], b["slot"], b["generation"]
        obs = observe_process(self.win, si, observed_at=self.now_fn())
        running = obs["attestation"]["outcome"] == PRESENT_VALID
        ev = obs["evidence"] if running else {}
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=self._marker(si), pid=ev.get("pid"),
                              session_id=ev.get("session_id"), path_ok=True,
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
        confirmed = confirm_terminated(self.win, MUTATING_CONTEXT, si, birth=birth,
                                       observed_at=self.now_fn())
        catt = self._record(slot, generation, confirmed)
        if catt["stage_status"] != COMPLETED:
            raise AgentError(catt["reason_code"] or "termination_not_observed")
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=True, extra={"running": False})

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
        b, si, slot, generation, marker_raw = self._gate(context, runtime_uuid)
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
        return self._evidence(runtime_uuid=runtime_uuid, slot=slot, generation=generation, si=si,
                              marker_raw=marker_raw, path_ok=True,
                              extra={"tombstoned": True,
                                     "idempotent": matt["stage_status"] == ALREADY_COMPLETED})

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
