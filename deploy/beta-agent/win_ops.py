"""CVM-Inc-3 B2 — Windows primitive layer for the beta provisioning agent.

The op implementations perform the SECURITY decisions (containment, ownership, process-image checks,
tombstone path, cross-volume refusal); this module supplies the mechanical Windows primitives behind an
interface so those decisions are fully unit-testable with a fake. ``RealWindowsOps`` is the box
implementation (robocopy / scheduled task / process enumeration / directory move); it is intentionally
thin and gets hardened + proven on the host during B3.
"""
from __future__ import annotations

import os
import time


class WindowsOpsError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class WindowsOps:
    """Interface. Every method is a mechanical primitive with NO policy — policy lives in op_impls."""
    def path_exists(self, path: str) -> bool: raise NotImplementedError
    def real_path(self, path: str) -> str | None: raise NotImplementedError          # reparse-resolved
    def read_owner_tag(self, canonical_dir: str) -> str | None: raise NotImplementedError
    def write_owner_tag(self, canonical_dir: str, runtime_uuid: str) -> None: raise NotImplementedError
    def make_dirs(self, path: str) -> None: raise NotImplementedError
    def copy_golden(self, canonical_dir: str) -> None: raise NotImplementedError      # MATERIALISE
    def launch_runtime(self, canonical_dir: str, runtime_uuid: str) -> dict: raise NotImplementedError  # START
    def find_runtime_process(self, canonical_dir: str) -> dict | None: raise NotImplementedError  # {pid, session_id, image}
    def stop_pid(self, pid: int) -> None: raise NotImplementedError
    def same_volume(self, a: str, b: str) -> bool: raise NotImplementedError
    def move_dir(self, src: str, dest: str) -> None: raise NotImplementedError        # TOMBSTONE


class RealWindowsOps(WindowsOps):
    """Box implementation. Thin wrappers around real Windows tools; the ``.owner`` tag is a sentinel file
    inside the runtime dir recording the owning runtime UUID. Hardened + proven on the host in B3."""
    OWNER_FILE = ".guvfx_owner"

    def path_exists(self, path):
        return os.path.exists(path)

    def real_path(self, path):
        try:
            return os.path.realpath(path) if os.path.exists(path) else None
        except OSError:
            return None

    def read_owner_tag(self, canonical_dir):
        f = os.path.join(canonical_dir, self.OWNER_FILE)
        try:
            with open(f, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def write_owner_tag(self, canonical_dir, runtime_uuid):
        with open(os.path.join(canonical_dir, self.OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(str(runtime_uuid))

    def make_dirs(self, path):
        os.makedirs(path, exist_ok=True)

    def copy_golden(self, canonical_dir):
        # B3 on the box: robocopy the checksummed golden MT5 image into canonical_dir /portable.
        raise WindowsOpsError("copy_golden_not_available_off_box")

    def launch_runtime(self, canonical_dir, runtime_uuid):
        # B3 on the box: launch via a per-runtime LogonType-Interactive scheduled task; capture pid+session.
        raise WindowsOpsError("launch_not_available_off_box")

    def find_runtime_process(self, canonical_dir):
        # B3 on the box: enumerate processes whose IMAGE PATH is beneath canonical_dir; return one or None.
        raise WindowsOpsError("process_enum_not_available_off_box")

    def stop_pid(self, pid):
        raise WindowsOpsError("stop_not_available_off_box")

    def same_volume(self, a, b):
        return os.path.splitdrive(os.path.abspath(a))[0].lower() == \
               os.path.splitdrive(os.path.abspath(b))[0].lower()

    def move_dir(self, src, dest):
        # NOT shutil.move: it catches every OSError from os.rename and falls back to copytree+rmtree, so a
        # tombstone silently becomes the copy-and-delete this design forbids (verified in CPython's
        # Lib/shutil.py). os.rename calls MoveFileExW with dwFlags=0 - no MOVEFILE_COPY_ALLOWED - and so
        # cannot degrade. Found while researching the B3P-2 adapter and fixed here too.
        os.rename(src, dest)


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# ── B3P-2: the SLOT-POOL adapter surface ───────────────────────────────────────────────────────────────
class SlotWindowsOps:
    """The complete Windows surface the slot-pool execution model needs — and nothing more.

    Contract: ``docs/B3P2_WINDOWS_ADAPTER_CONTRACT.md``. The fake adapter used by the test suite and the
    real one below satisfy the SAME contract, which is what makes off-host tests evidence about the real
    system rather than about a mock.

    Three absences are deliberate and are the interface's main security property:

    * **no process-kill method** — termination goes through the fixed per-slot terminate task, so the agent
      never needs the right to kill another account's process;
    * **no process-launch method** — launching goes through the fixed per-slot launch task, so the agent
      holds no runtime credential;
    * **no delete, no ACL write, no task registration, no user creation** — removal is a MOVE to tombstone,
      and every OS object is created once by a human at the install-only gate.

    Every method receives a slot-derived path or a fixed task name. None receives a runtime UUID,
    generation, occupancy id or job id: a primitive that needed one would be a design error.

    OBSERVATION RULE (the one that matters most): a method that cannot observe must RAISE. Returning
    ``None``/``False`` for an unreadable state would let the caller honestly report an unproven claim — for
    example a live runtime recorded as terminated. Absence and unavailability are different answers.
    """

    # reads
    def golden_source_info(self) -> dict: raise NotImplementedError
    def destination_info(self, slot_path: str) -> dict: raise NotImplementedError
    def path_exists(self, path: str) -> bool: raise NotImplementedError
    def real_path(self, path: str) -> str | None: raise NotImplementedError
    def read_owner_tag(self, slot_path: str) -> str | None: raise NotImplementedError
    def read_acl(self, path: str): raise NotImplementedError
    def same_volume(self, a: str, b: str) -> bool: raise NotImplementedError
    def query_task(self, task_name: str) -> dict | None: raise NotImplementedError
    def task_running(self, task_name: str) -> bool: raise NotImplementedError
    def query_slot_process(self, slot_path: str) -> dict | None: raise NotImplementedError
    def open_handles(self, path: str) -> bool: raise NotImplementedError

    # writes (the entire mutating surface — four methods)
    def copy_golden(self, slot_path: str) -> None: raise NotImplementedError
    def write_owner_tag(self, slot_path: str, marker_raw: str) -> None: raise NotImplementedError
    def run_task(self, task_name: str) -> bool: raise NotImplementedError
    def move_dir(self, src: str, dest: str) -> None: raise NotImplementedError
