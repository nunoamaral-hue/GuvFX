"""CVM-Inc-3 B2 — local operation implementations for the beta provisioning agent.

These are the version-controlled implementation code the agent maps allowlisted operations to (the
network never carries any of this). They receive ONLY the agent-derived canonical dir + the runtime UUID
and enforce the op-specific security decisions on top of the agent-core's generic containment/idempotency:

 - ownership: a runtime dir is tagged with its owning UUID; a conflicting tag is refused;
 - START is idempotent (never launches a second terminal for a runtime already running);
 - STOP identifies the terminal by canonical image path + PID + session and REFUSES to stop a process
   whose image is not beneath the owned canonical runtime path (never by exe name);
 - TOMBSTONE verifies ownership, stops only the bound process, MOVES (never deletes) the canonical dir
   into C:\GuvFX\beta\tombstones\<uuid>\<timestamp>\, refuses cross-volume / non-canonical moves, and is
   idempotent.
"""
from lib.mgmt_agent_core import AgentError
from win_ops import WindowsOpsError, utc_stamp

DEFAULT_TOMBSTONE_BASE = r"C:\GuvFX\beta\tombstones"


class OpError(AgentError):
    """An op-specific security denial — an ``AgentError`` so the agent core returns it as a sanitised
    ``outcome=denied`` with this reason code."""


class OpImplementations:
    def __init__(self, win, *, tombstone_base: str = DEFAULT_TOMBSTONE_BASE):
        self.win = win
        self.tombstone_base = tombstone_base

    def as_dict(self) -> dict:
        return {"MATERIALISE": self.materialise, "START": self.start, "VERIFY": self.verify,
                "STOP": self.stop, "TOMBSTONE": self.tombstone}

    # ── helpers ──
    def _assert_owned_or_free(self, canonical_dir: str, runtime_uuid: str) -> str | None:
        """Return the existing owner tag; raise if a DIFFERENT runtime owns this path."""
        if not self.win.path_exists(canonical_dir):
            return None
        tag = self.win.read_owner_tag(canonical_dir)
        if tag is not None and tag != str(runtime_uuid):
            raise OpError("ownership_conflict")
        return tag

    def _assert_owned(self, canonical_dir: str, runtime_uuid: str) -> None:
        tag = self.win.read_owner_tag(canonical_dir) if self.win.path_exists(canonical_dir) else None
        if tag != str(runtime_uuid):
            raise OpError("not_owned")

    # ── operations ──
    def materialise(self, *, canonical_dir, runtime_uuid, base) -> dict:
        existing = self._assert_owned_or_free(canonical_dir, runtime_uuid)
        if existing == str(runtime_uuid):
            return {"materialised": True, "idempotent": True}    # already materialised + owned
        self.win.make_dirs(canonical_dir)
        # Defense-in-depth (S2): even after the agent-core's ancestor reparse check, re-verify the created
        # dir's REAL path is contained before writing the golden image — a junction must never redirect it.
        real = self.win.real_path(canonical_dir)
        if real is not None and not _beneath(real, base):
            raise OpError("reparse_escape_after_materialise")
        self.win.copy_golden(canonical_dir)
        self.win.write_owner_tag(canonical_dir, runtime_uuid)
        return {"materialised": True}

    def start(self, *, canonical_dir, runtime_uuid, base) -> dict:
        self._assert_owned(canonical_dir, runtime_uuid)          # must be materialised + owned first
        proc = self.win.find_runtime_process(canonical_dir)
        if proc is not None:
            # already running for THIS canonical dir → return its identity; NEVER launch a second terminal
            return {"pid": proc["pid"], "session_id": proc.get("session_id"), "idempotent": True}
        launched = self.win.launch_runtime(canonical_dir, runtime_uuid)
        return {"pid": launched.get("pid"), "session_id": launched.get("session_id")}

    def verify(self, *, canonical_dir, runtime_uuid, base) -> dict:
        proc = self.win.find_runtime_process(canonical_dir)      # image-beneath-canonical by construction
        if proc is None:
            return {"running": False, "logged_in": False}
        return {"running": True, "logged_in": False,
                "pid": proc["pid"], "session_id": proc.get("session_id")}

    def stop(self, *, canonical_dir, runtime_uuid, base) -> dict:
        self._assert_owned(canonical_dir, runtime_uuid)
        proc = self.win.find_runtime_process(canonical_dir)
        if proc is None:
            return {"running": False, "idempotent": True}         # nothing to stop
        # process identity: find_runtime_process only ever returns a process whose IMAGE is beneath the
        # canonical dir; re-assert here as defense-in-depth — never stop by exe name.
        image = (proc.get("image") or "")
        if not _beneath(image, canonical_dir):
            raise OpError("image_not_owned")
        self.win.stop_pid(proc["pid"])
        return {"running": False, "pid": proc["pid"], "session_id": proc.get("session_id")}

    def tombstone(self, *, canonical_dir, runtime_uuid, base) -> dict:
        dest = rf"{self.tombstone_base}\{runtime_uuid}\{utc_stamp()}"
        # Idempotent: the runtime dir is already gone (previously tombstoned) → nothing to move.
        if not self.win.path_exists(canonical_dir):
            return {"tombstoned": True, "idempotent": True}
        self._assert_owned(canonical_dir, runtime_uuid)          # ownership before any destructive move
        # cross-volume refusal — a tombstone must be a MOVE within the same volume, never a copy+delete.
        if not self.win.same_volume(canonical_dir, self.tombstone_base):
            raise OpError("cross_volume_move_refused")
        # stop the bound process first (image-beneath-canonical), if any
        proc = self.win.find_runtime_process(canonical_dir)
        if proc is not None and _beneath(proc.get("image") or "", canonical_dir):
            self.win.stop_pid(proc["pid"])
        self.win.make_dirs(rf"{self.tombstone_base}\{runtime_uuid}")
        self.win.move_dir(canonical_dir, dest)                    # MOVE, never delete
        return {"tombstoned": True, "tombstone_dir": dest}


def _beneath(path: str, root: str) -> bool:
    p = (path or "").replace("/", "\\").rstrip("\\").lower()
    r = (root or "").replace("/", "\\").rstrip("\\").lower()
    return p == r or p.startswith(r + "\\")
