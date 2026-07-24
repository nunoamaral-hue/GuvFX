"""CVM-Inc-3 B3P-2 — the REAL Windows adapter for the slot-pool execution model.

Contract: ``docs/B3P2_WINDOWS_ADAPTER_CONTRACT.md``. Research basis and its limits:
``docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md``. **Nothing in this module has ever executed on a Windows host.**

Two structural choices make that admission survivable:

1. **Every decision is a module-level pure function** (`classify_robocopy_exit`, `select_slot_process`,
   `tree_digest`, `paths_equal`, …). Those are fully tested off-host. The class methods are thin
   Win32 plumbing around them, so the part that can be wrong on the box is the part with the least logic.
2. **Unresolved semantics fail closed.** Where the documentation is contradictory, silent, or archived, this
   module raises rather than guesses. A raised error degrades an observation to
   ``*_observation_unavailable`` upstream; a wrong return value would produce a confident false claim, which
   is the one outcome the whole design exists to prevent.

Only CONFIRMED documented facts are relied upon. Each one is cited inline as ``[R:<id>]`` against the
findings document, so a reviewer can check the basis of any line without reading the whole research set.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess

from win_ops import MultipleSlotProcesses, SlotWindowsOps, WindowsOpsError

#: Marker files. Both are GuvFX artefacts, not MetaTrader ones.
OWNER_FILE = ".guvfx_owner"
#: [R:mt5-portable-marker] MetaQuotes documents NO on-disk marker for portable mode — ``/portable`` is a
#: per-launch command-line property. So "is this runtime portable" cannot be read from the tree at all. The
#: operator places this file in the golden image at the install gate to record the intent, and the
#: authoritative check is that the launch task's arguments carry ``/portable`` (see ``portable_switch``).
PORTABLE_FILE = ".guvfx_portable"
#: Written by the operator beside the golden image; pins the approved image version.
GOLDEN_MANIFEST_FILE = ".guvfx_golden_manifest"
RUNTIME_EXECUTABLE = "terminal64.exe"

#: [R:toolhelp-bitness-and-retry], [R:enumeration-by-directory-algorithm]
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NO_MORE_FILES = 18                     # Process32NextW's clean end-of-enumeration signal
TH32CS_SNAPPROCESS = 0x00000002             # CreateToolhelp32Snapshot: processes only


def _make_processentry32w(ctypes, wintypes):
    """The Win32 ``PROCESSENTRY32W`` layout (10 fields, ``szExeFile`` = MAX_PATH wide chars). Defined via a
    factory (not inside ``_win32``) so the EXACT layout the host relies on can be pinned off-host without
    loading kernel32 (RULE 11 positive control).

    The numeric fields use FIXED-WIDTH types (``c_uint32`` for ``DWORD``, ``c_int32`` for ``LONG``) rather
    than ``wintypes.DWORD`` (= ``c_ulong``), because ``c_ulong`` is 8 bytes on LP64 (Linux/macOS) and 4 on
    Windows LLP64 — so ``wintypes`` would make the off-host layout differ from the host's and defeat the
    positive control. Fixed-width types are 4 bytes on every platform, matching Windows, so field OFFSETS
    are validatable off-host. ``th32DefaultHeapID`` is ``ULONG_PTR`` (``c_void_p``, pointer-sized): on 64-bit
    it forces 8-byte alignment; a regression to a 32-bit type shifts every later field and would make the
    enumeration misread pid/name (fail-open). ``szExeFile`` keeps ``c_wchar`` for the string decode; its
    per-char width is platform-dependent, but its OFFSET is not (every field before it is fixed-width)."""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32),
                    ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]
    return PROCESSENTRY32W


def _reraise(error):
    """``os.walk`` default ``onerror`` SWALLOWS every scandir error, so an unreadable subtree would simply
    be omitted from a digest and the digest would still "match". Contract sections 3.1/3.2 say raise."""
    raise error

#: [R:task-state-enum] TASK_STATE values, exact.
TASK_STATE_UNKNOWN, TASK_STATE_DISABLED, TASK_STATE_QUEUED, TASK_STATE_READY, TASK_STATE_RUNNING = 0, 1, 2, 3, 4

#: [R:robocopy-success-threshold] Microsoft documents only that ">= 8 indicates at least one failure". The
#: converse ("0-7 is success") is NOT documented, and codes 2/3/6/7 mean extra files exist in the
#: destination while 5/6/7 mean mismatched files — neither of which can legitimately happen when copying
#: into a destination the caller has already proven ABSENT. So the accepted set is deliberately narrower
#: than the folklore rule: 0 (nothing to do) and 1 (all files copied).
ROBOCOPY_ACCEPTED = (0, 1)


class WindowsApiUnavailable(WindowsOpsError):
    """The Windows API surface this adapter needs is not present (e.g. running off-host).

    Raised rather than degraded: an adapter that answered questions without the API would be inventing
    facts about a machine it cannot see.
    """

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__("windows_api_unavailable")


# ── pure decision logic (fully tested off-host) ────────────────────────────────────────────────────────
def classify_robocopy_exit(rc: int) -> str:
    """``accepted`` | ``failed``.

    [R:robocopy-success-threshold] ``subprocess.run(check=True)`` and any ``rc != 0`` gate REJECT the
    healthy fresh-copy path, because exit 1 is documented verbatim as "All files were copied successfully".
    Getting this backwards is the classic robocopy bug; getting it too loose is the dangerous one.
    """
    return "accepted" if int(rc) in ROBOCOPY_ACCEPTED else "failed"


def normalise(path: str) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").lower()


def paths_equal(a: str, b: str) -> bool:
    return normalise(a) == normalise(b)


def is_beneath_path(path: str, root: str) -> bool:
    p, r = normalise(path), normalise(root)
    return p == r or p.startswith(r + "\\")


def select_slot_process(candidates: list, slot_path: str):
    """Choose THE runtime process for a slot from the image-contained candidates.

    [R:cannot-multiple-processes-beneath-slot] It is unknown whether MT5 spawns helper executables from its
    own tree, so "return one or None" may be ambiguous on the real host. Resolution rules, in order:

    * none → ``None`` (genuinely not running);
    * exactly one → that one;
    * several, one of which is ``<slot_path>\\terminal64.exe`` → that one (deterministic and meaningful);
    * several, none of which is the runtime executable → **raise**. Picking arbitrarily would attach the
      whole termination and identity chain to a process chosen by enumeration order.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    target = normalise(os.path.join(slot_path, RUNTIME_EXECUTABLE))
    exact = [c for c in candidates if normalise(c.get("image") or "") == target]
    if len(exact) == 1:
        return exact[0]
    # Several fully-attributed slot processes with no single canonical one — a DISTINCT fail-closed state
    # (ADR-0015), never resolved by picking one by enumeration order.
    raise MultipleSlotProcesses("multiple_matching_processes")


def classify_open_process_error(winerror: int) -> str:
    """``gone`` | ``denied`` | ``unknown``.

    [R:enumeration-by-directory-algorithm] The distinction is the whole point: ``denied`` must NEVER be
    read as "no process", or the agent will report a live runtime as terminated. Note that the mapping of
    87 to a dead PID is empirical, not documented — hence ``unknown`` for everything else, which the caller
    treats as unreadable rather than absent.
    """
    if int(winerror) == ERROR_INVALID_PARAMETER:
        return "gone"
    if int(winerror) == ERROR_ACCESS_DENIED:
        return "denied"
    return "unknown"


def manifest_line(relpath: str, size: int, digest: str) -> str:
    return f"{normalise(relpath)}|{int(size)}|{digest}\n"


def tree_digest(entries) -> str:
    """Digest a directory tree from ``(relpath, size, sha256hex)`` records.

    [R:manifest-digest-design] Two-level: per-file content digests, then one digest over a canonically
    serialised, sorted manifest. Sorting and separator normalisation are done HERE rather than relying on
    filesystem enumeration order, which is not guaranteed.
    """
    body = "".join(manifest_line(*e) for e in sorted(entries, key=lambda e: normalise(e[0])))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def portable_switch_present(arguments) -> bool:
    """[R:mt5-portable-switch] Portable mode is decided by the ``/portable`` command-line key at launch —
    it is not on-disk state. The launch task's arguments are therefore the authoritative signal."""
    return "/portable" in str(arguments or "").lower().split()


#: HRESULT/winerror values that mean "denied" rather than "broken".
_ACCESS_DENIED_CODES = (5, -2147024891)                  # ERROR_ACCESS_DENIED, E_ACCESSDENIED (0x80070005)


def translate_denial(exc):
    """Re-raise a COM/pywin32 access denial as ``PermissionError``.

    The stages branch on ``PermissionError`` SPECIFICALLY to produce ``*_permission_denied`` (category
    OBSERVATION / WINDOWS-denial) rather than ``*_unavailable`` (a retryable host fault). Without this
    translation an ACL misconfiguration is filed as a transient error and retried forever, and three
    reason codes in the classification map are unreachable.
    """
    for value in (getattr(exc, "winerror", None), getattr(exc, "hresult", None),
                  (exc.args[0] if getattr(exc, "args", None) else None)):
        if isinstance(value, int) and value in _ACCESS_DENIED_CODES:
            raise PermissionError(str(exc.__class__.__name__)) from exc
    raise exc


class RealSlotWindowsOps(SlotWindowsOps):
    """Box implementation. Untested on Windows; every method fails closed when it cannot be certain.

    Windows modules are imported lazily so this file is importable (and testable) anywhere. Off-host, every
    method raises :class:`WindowsApiUnavailable` — it never returns a plausible-looking answer.
    """

    def __init__(self, *, golden_dir: str, slots_root: str, hash_chunk: int = 1 << 16):
        self.golden_dir = golden_dir
        self.slots_root = slots_root
        self.hash_chunk = hash_chunk
        self._api = None
        self._sid_cache = {}

    # ── lazy Win32 access ──
    @staticmethod
    def _ctypes():
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        import ctypes
        return ctypes

    def _win32(self):
        """Load kernel32 with EXPLICIT restype and argtypes.

        Two defects this avoids, both silent and both 64-bit-only — exactly the kind that would have been
        found on the box rather than in review:

        * without ``restype = HANDLE``, ctypes defaults to C ``int`` and TRUNCATES a 64-bit process handle
          to 32 bits, so every subsequent call on that handle addresses the wrong object;
        * without ``use_last_error=True`` on the library, ``ctypes.get_last_error()`` always returns 0, so
          the denied-vs-gone classification — the distinction the whole design depends on — would read
          every failure as "unknown".
        """
        if self._api is not None:
            return self._api
        ctypes = self._ctypes()
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PDWORD = ctypes.POINTER(wintypes.DWORD)
        PFILETIME = ctypes.POINTER(wintypes.FILETIME)

        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   PDWORD]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE, PFILETIME, PFILETIME, PFILETIME, PFILETIME]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, PDWORD]
        k32.ProcessIdToSessionId.restype = wintypes.BOOL
        k32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetLongPathNameW.restype = wintypes.DWORD
        k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetVolumePathNameW.restype = wintypes.BOOL
        k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                                          wintypes.DWORD]
        k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL

        # Toolhelp process enumeration (ADR-0015): UNPRIVILEGED — works from the low-privilege beta service
        # account, unlike WTSEnumerateProcesses which that account is denied. It yields (pid, name, ppid)
        # WITHOUT opening any process; identity (path/SID/session) is then resolved per-candidate under the
        # weakest access that works. The struct layout is defined once, at module level, so it is pinnable
        # off-host (RULE 11) without loading kernel32.
        PROCESSENTRY32W = _make_processentry32w(ctypes, wintypes)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL

        self._api = {"ctypes": ctypes, "wintypes": wintypes, "k32": k32,
                     "PROCESSENTRY32W": PROCESSENTRY32W}
        return self._api

    @staticmethod
    def _com():
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        try:
            import pywintypes
            import win32com.client
        except ImportError as exc:                      # pywin32 absent — a deployment fault, not a state
            raise WindowsApiUnavailable("pywin32 not installed") from exc
        return win32com.client, pywintypes



    # ── filesystem ───────────────────────────────────────────────────────────────────────────────────
    def path_exists(self, path: str) -> bool:
        """[R:path-exists-must-not-swallow] ``os.path.exists`` returns False on an access denial, which
        would report a directory that exists as absent. ``os.lstat`` distinguishes the two."""
        try:
            os.lstat(path)
            return True
        except FileNotFoundError:
            return False
        except NotADirectoryError:
            return False                                # a component is a file: the path cannot exist
        except OSError:
            raise                                       # permission/IO: unreadable, never "absent"

    def real_path(self, path: str):
        """[R:realpath-nonstrict-swallows-errors] ``strict=True`` is mandatory: the default mode falls back
        to a partial resolution that silently tolerates errors, which would defeat the junction-escape
        guard this method exists to feed."""
        try:
            return os.path.realpath(path, strict=True)
        except FileNotFoundError:
            return None
        except OSError:
            raise

    def same_volume(self, a: str, b: str) -> bool:
        """[R:drive-letter-comparison-is-wrong] Compare volume GUIDs, not drive letters: a directory can be
        a mounted folder for a different volume. For a destination that does not exist yet, the nearest
        EXISTING ancestor is used — a documented-behaviour choice, not a guess about non-existent paths."""
        return self._volume_guid(a) == self._volume_guid(b)

    def _volume_guid(self, path: str) -> str:
        api = self._win32()
        ctypes, k32 = api["ctypes"], api["k32"]
        probe = self._nearest_existing(path)
        buf = ctypes.create_unicode_buffer(260)
        if not k32.GetVolumePathNameW(probe, buf, 260):
            raise WindowsOpsError("volume_path_unavailable")
        mount = buf.value
        if not mount.endswith("\\"):
            mount += "\\"                                # GetVolumeNameForVolumeMountPointW REQUIRES this
        guid = ctypes.create_unicode_buffer(60)
        if not k32.GetVolumeNameForVolumeMountPointW(mount, guid, 60):
            raise WindowsOpsError("volume_identity_unavailable")
        return guid.value.lower()

    def _nearest_existing(self, path: str) -> str:
        current = path
        while current and not self.path_exists(current):
            parent = current.rsplit("\\", 1)[0]
            if parent == current:
                raise WindowsOpsError("no_existing_ancestor")
            current = parent
        return current

    def move_dir(self, src: str, dest: str) -> None:
        """[R:python-rename-does-not-set-copy-allowed] ``os.rename`` calls ``MoveFileExW`` with
        ``dwFlags=0`` — no ``MOVEFILE_COPY_ALLOWED`` — so it CANNOT degrade into copy-plus-delete.

        [R:shutil-move-silently-degrades] ``shutil.move`` catches every ``OSError`` from ``os.rename`` and
        falls back to ``copytree`` + ``rmtree``, turning a tombstone into exactly the copy-and-delete the
        design forbids. It must never be used here, and the AST boundary test forbids importing ``shutil``.
        """
        parent = dest.rsplit("\\", 1)[0]
        if parent and not self.path_exists(parent):
            os.makedirs(parent)                         # only the given destination's parent, nothing else
        os.rename(src, dest)                            # FileExistsError if dest exists — fail closed

    def read_owner_tag(self, slot_path: str):
        return self._read_marker(os.path.join(slot_path, OWNER_FILE))

    def write_owner_tag(self, slot_path: str, marker_raw: str) -> None:
        with open(os.path.join(slot_path, OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(str(marker_raw))

    @staticmethod
    def _read_marker(path: str):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return None
        except OSError:
            raise                                       # unreadable != absent

    def read_acl(self, path: str):
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        try:
            import win32security
        except ImportError as exc:
            raise WindowsApiUnavailable("pywin32 not installed") from exc
        sd = win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        return {"owner_sid": str(sd.GetSecurityDescriptorOwner()),
                "ace_count": dacl.GetAceCount() if dacl is not None else None}

    # ── golden image + destination integrity ─────────────────────────────────────────────────────────
    def golden_source_info(self) -> dict:
        # None, not "": an absent manifest compared as "" == "" would make
        # source_manifest_version_matches pass on an unversioned golden image.
        return {"digest": self._tree_digest(self.golden_dir),
                "manifest_version": self._read_marker(
                    os.path.join(self.golden_dir, GOLDEN_MANIFEST_FILE))}

    def destination_info(self, slot_path: str) -> dict:
        exe = os.path.join(slot_path, RUNTIME_EXECUTABLE)
        return {
            "digest": self._tree_digest(slot_path),
            "executable_digest": self._file_digest(exe) if self.path_exists(exe) else None,
            "portable_marker": self.path_exists(os.path.join(slot_path, PORTABLE_FILE)),
            "ownership_marker": self.path_exists(os.path.join(slot_path, OWNER_FILE)),
        }

    def _file_digest(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(self.hash_chunk), b""):
                h.update(chunk)
        return h.hexdigest()

    def _tree_digest(self, root: str) -> str:
        """[R:digest-pitfall-reparse-points] ``os.walk(followlinks=False)`` is NOT enough on Windows: it
        gates recursion on ``os.path.islink``, which returns False for directory junctions, so junctions are
        walked into by default. Every entry is classified with ``os.lstat`` and a reparse point anywhere in
        a runtime tree is an integrity failure, not something to digest around."""
        os.lstat(root)                       # a missing/unreadable root must raise, never digest to sha256(b"")
        entries = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_reraise):
            for name in list(dirnames):
                if self._is_reparse(os.path.join(dirpath, name)):
                    raise WindowsOpsError("reparse_point_in_tree")
            for name in filenames:
                if dirpath == root and name == OWNER_FILE:
                    # The occupancy marker is written INTO the staged tree after the copy, so digesting it
                    # would guarantee the destination digest could never equal the golden one.
                    continue
                full = os.path.join(dirpath, name)
                if self._is_reparse(full):
                    raise WindowsOpsError("reparse_point_in_tree")
                st = os.lstat(full)
                entries.append((os.path.relpath(full, root), st.st_size, self._file_digest(full)))
        return tree_digest(entries)

    @staticmethod
    def _is_reparse(path: str) -> bool:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    def copy_golden(self, slot_path: str) -> None:
        """[R:robocopy-mir-purge-hazard] ``/MIR`` implies ``/PURGE``, which really deletes destination
        content. It is never used. ``/E`` copies subdirectories including empty ones; ``/COPY:DAT`` takes
        data, attributes and timestamps but not ACLs — the slot's ACL is the operator's, set at install, and
        must not be overwritten by a copy."""
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        cmd = ["robocopy", self.golden_dir, slot_path, "/E", "/COPY:DAT",
               "/R:2", "/W:2", "/NFL", "/NDL", "/NP", "/NJH", "/NJS"]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if classify_robocopy_exit(completed.returncode) != "accepted":
            # A FIXED reason code: an interpolated one cannot be classified, and would be invisible to the
            # test that proves every reason code maps to a failure category. The exit code travels as an
            # attribute instead. robocopy's output is never recorded — it can contain full paths.
            error = WindowsOpsError("golden_copy_failed")
            error.exit_code = completed.returncode
            raise error

    # ── task scheduler ───────────────────────────────────────────────────────────────────────────────
    def _folder(self):
        client, _pywintypes = self._com()
        try:
            service = client.Dispatch("Schedule.Service")    # [R:progid-schedule-service]
            service.Connect()
            return service.GetFolder("\\")
        except Exception as exc:                             # noqa: BLE001 — denial vs fault, see below
            translate_denial(exc)

    def _registered_task(self, task_name: str):
        """[R:cannot-missing-task-hresult] No Microsoft source maps a specific HRESULT to an absent task, so
        a failed ``GetTask`` cannot be read as "not there". Enumeration is used instead: absence from the
        folder listing is POSITIVE evidence of absence; anything else propagates as unreadable."""
        folder = self._folder()
        try:
            tasks = list(folder.GetTasks(0))
        except Exception as exc:                             # noqa: BLE001
            translate_denial(exc)
        for task in tasks:
            if str(task.Name).lower() == str(task_name).lower():
                return task
        return None

    def query_task(self, task_name: str):
        task = self._registered_task(task_name)
        if task is None:
            return None
        definition = task.Definition
        principal = definition.Principal
        action = definition.Actions.Item(1) if definition.Actions.Count else None
        arguments = getattr(action, "Arguments", "") if action is not None else ""
        return {
            "task_name": str(task.Name),
            "run_as_identity": str(getattr(principal, "UserId", "") or ""),
            "run_as_sid": None,                          # not exposed by the COM surface
            "executable": str(getattr(action, "Path", "") or "") if action is not None else "",
            "working_directory": str(getattr(action, "WorkingDirectory", "") or "")
                                 if action is not None else "",
            "arguments": str(arguments or ""),
            "portable_switch": portable_switch_present(arguments),
            "logon_type": int(getattr(principal, "LogonType", TASK_STATE_UNKNOWN)),
            "run_level": int(getattr(principal, "RunLevel", 0)),
            "enabled": bool(task.Enabled),
            "last_result": int(task.LastTaskResult),
        }

    def task_running(self, task_name: str) -> bool:
        task = self._registered_task(task_name)
        if task is None:
            raise WindowsOpsError("task_absent")         # absence of a task is not "not running"
        return int(task.State) == TASK_STATE_RUNNING     # [R:task-state-enum]

    def run_task(self, task_name: str) -> bool:
        """Trigger the fixed per-slot task.

        [R:run-vs-runex-disabled-asymmetry] Microsoft's pages are mutually inconsistent about whether
        ``Run`` or ``RunEx`` reports a disabled task, so neither error path is relied on: the task's
        ``Enabled``/``State`` are checked explicitly first.

        [R:run-is-request-not-proof] A successful return proves only that the scheduler ACCEPTED the
        request. This function's ``True`` therefore means "trigger accepted" and nothing more — which is
        exactly what ``request_launch``/``request_terminate`` record.

        [R:enginepid-is-not-the-app-pid] The ``IRunningTask`` optionally returned here is deliberately
        discarded: its ``EnginePID`` is the task engine's PID, not the runtime's.
        """
        task = self._registered_task(task_name)
        if task is None:
            raise WindowsOpsError("task_absent")
        if not bool(task.Enabled) or int(task.State) == TASK_STATE_DISABLED:
            return False                                 # rejected — the caller records *_trigger_rejected
        try:
            task.Run(None)                               # pywin32 raises com_error on a FAILED hresult
        except Exception as exc:                         # noqa: BLE001
            translate_denial(exc)
        return True

    # ── process observation ──────────────────────────────────────────────────────────────────────────
    def query_slot_process(self, slot_path: str, runtime_identity: str = ""):
        """Find THE runtime process for this slot from an UNPRIVILEGED snapshot (ADR-0015), scoped by the
        slot's fixed identity SID, executable path and session — NEVER by executable name alone.

        [R:toolhelp-name-not-path] Name is not a match. The golden image is a copy of MetaTrader 5, so a
        materialised slot contains ``terminal64.exe`` — the SAME name as the operator's production terminal
        in an interactive session. Name only SCOPES which processes are worth inspecting; a candidate is
        MATCHED only when its owner SID, executable path and session all agree with this slot.

        Four outcomes, all fail-closed (``win_primitives.observe_process`` maps them):
          * ``None``                    -> ABSENT   — no attributed process, and every plausible candidate
                                                      was positively excluded by session or by evidence;
          * one dict                    -> PRESENT;
          * ``raise MultipleSlotProcesses`` -> MULTIPLE_MATCHING;
          * ``raise WindowsOpsError``   -> OBSERVATION UNAVAILABLE — a plausible in-slot candidate whose
                                            mandatory identity/path evidence could not be resolved (access
                                            denied, or path/owner/start-time unreadable), or an enumeration
                                            failure. A denial is NEVER read as absence.

        Session is EVIDENCE, not a gate on real candidates. The authoritative match is owner SID + path,
        both read from an OPEN handle; a candidate we can open is matched (or excluded) on those alone,
        regardless of its session — so the session logic can never turn a live slot runtime into a false
        ABSENT. Session is used in exactly ONE place: to exclude an UNOPENABLE same-name candidate whose
        session is known and differs from the slot's expected (batch-logon = observer Session 0) one — i.e.
        the operator's interactive terminal, which the low-privilege account is denied a handle to. A
        same/unknown-session unopenable candidate stays UNRESOLVED (fail closed), never absence.

        [R:creationtime-filetime-units] Creation time is a raw 64-bit FILETIME.
        """
        if not self.path_exists(slot_path):
            return None                          # nothing can run from a directory that does not exist
        api = self._win32()
        k32 = api["k32"]
        canonical_slot = self._long_path(slot_path)
        expected_sid = self._identity_sid(runtime_identity)
        expected_session = self._session_id(api, os.getpid())     # the observer's own (batch/service) session
        runtime_exe = RUNTIME_EXECUTABLE.lower()
        candidates, unresolved = [], []
        for pid, name, _ppid in self._enumerate_process_entries():
            if name.lower() != runtime_exe:
                continue                         # not the runtime executable -> cannot BE the slot runtime
            handle, state = self._open_process(api, pid)
            if handle is None:
                if state == "gone":
                    continue                     # exited during enumeration -> not present
                # UNOPENABLE same-name candidate: owner and path CANNOT be read, so the ONLY discriminator
                # left is the handle-less session. Exclude it as definitively-not-this-slot ONLY when its
                # session is known AND differs from the slot's expected (batch-logon = observer's Session 0)
                # session — that is the operator's interactive terminal, which the account cannot open. A
                # same-session OR unknown-session unopenable candidate MIGHT be this slot's runtime, so it is
                # UNRESOLVED (fail closed), never absence. Session here EXCLUDES a non-candidate; it NEVER
                # gates a process we could actually attribute (see the open path below).
                sess = self._session_id(api, pid)
                if expected_session is not None and sess is not None and sess != expected_session:
                    continue
                unresolved.append(pid)           # denied/unknown on a PLAUSIBLE candidate -> NOT absence
                continue
            try:
                # OPENABLE candidate: owner SID + executable path are AUTHORITATIVE. Session is recorded as
                # evidence only and NEVER excludes here — a slot process we can open in a surprise session is
                # still matched by owner+path, so the session gate can never turn a live slot runtime into a
                # false ABSENT (the fail-open this ordering exists to prevent).
                image = self._image_path(api, handle)
                sid = self._user_sid(pid)
                if image is None or sid is None:
                    unresolved.append(pid)       # path or owner unresolved on a plausible candidate
                    continue
                if sid != expected_sid:
                    continue                     # openable, WRONG OWNER -> another account (never the slot)
                if not is_beneath_path(self._long_path(image), canonical_slot):
                    continue                     # openable, right owner, running from ELSEWHERE
                try:
                    created = self._creation_filetime(api, handle)
                except WindowsOpsError:
                    unresolved.append(pid)       # start-time evidence unreadable on a match -> fail closed
                    continue
                candidates.append({
                    "pid": pid,
                    "created_at_filetime": created,
                    "image": image,
                    "image_digest": self._file_digest(image) if self.path_exists(image) else None,
                    "user_sid": sid,
                    "session_id": self._session_id(api, pid),      # evidence only, not a gate
                })
            finally:
                k32.CloseHandle(handle)
        if unresolved:
            # A plausible in-slot candidate could not be attributed -> observation UNAVAILABLE, block the
            # lifecycle. Never silently skipped, never read as absence (ADR-0015 fail-closed rule).
            raise WindowsOpsError("process_attribution_incomplete")
        return select_slot_process(candidates, slot_path)   # None | the one | raise MultipleSlotProcesses

    def _identity_sid(self, runtime_identity: str) -> str:
        """Resolve the slot's fixed account name to its SID, once, cached.

        An unresolvable identity RAISES: the account is created by the operator at the install gate, so its
        absence is a deployment fault. Continuing with an empty scope would match nothing and report every
        slot empty — the fail-open this method exists to prevent.
        """
        if not runtime_identity:
            raise WindowsOpsError("runtime_identity_required")
        if runtime_identity in self._sid_cache:
            return self._sid_cache[runtime_identity]
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        try:
            import win32security
        except ImportError as exc:
            raise WindowsApiUnavailable("pywin32 not installed") from exc
        try:
            sid, _domain, _use = win32security.LookupAccountName(None, runtime_identity)
            resolved = str(win32security.ConvertSidToStringSid(sid))
        except Exception as exc:                             # noqa: BLE001
            raise WindowsOpsError("runtime_identity_unresolvable") from exc
        self._sid_cache[runtime_identity] = resolved
        return resolved

    def _enumerate_process_entries(self):
        """``(pid, name, ppid)`` for every process via the Toolhelp snapshot — UNPRIVILEGED (ADR-0015).

        Replaces the WTS enumeration (``WTSEnumerateProcesses``), which the low-privilege beta service
        account is DENIED — the whole reason the observe layer failed under the deployed service identity
        while working as an administrator. Toolhelp opens no process and needs no session-query privilege.

        Unlike WTS, Toolhelp yields NO owner SID (it is not in ``PROCESSENTRY32W``): identity — path, owner
        SID and session — is resolved per-candidate later, under the weakest access that works.

        Failure RAISES rather than yielding a short list: a partial enumeration silently narrows the scope
        and would report a running slot as empty (fail-open — the exact failure this layer must never make).
        """
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        api = self._win32()
        ctypes, k32, PE = api["ctypes"], api["k32"], api["PROCESSENTRY32W"]
        invalid = ctypes.c_void_p(-1).value                  # INVALID_HANDLE_VALUE, bitness-correct
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == invalid:
            raise WindowsOpsError("process_snapshot_failed")
        try:
            entry = PE()
            entry.dwSize = ctypes.sizeof(PE)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                # An empty snapshot is impossible on a live host (this process is always present); treat a
                # failed first read as an enumeration failure, never as "no processes".
                raise WindowsOpsError("process_snapshot_empty")
            rows = []
            while True:
                rows.append((int(entry.th32ProcessID), str(entry.szExeFile),
                             int(entry.th32ParentProcessID)))
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    if ctypes.get_last_error() == ERROR_NO_MORE_FILES:
                        break                                # clean end of enumeration
                    raise WindowsOpsError("process_snapshot_iteration_failed")
            return rows
        finally:
            k32.CloseHandle(snap)

    def _long_path(self, path: str) -> str:
        """[R:short-name-83-aliasing] 8.3 aliasing may or may not be enabled on the target volume — it is
        per-volume configurable and unknowable off-host. Both sides of a containment comparison are
        normalised to their long form; if normalisation fails we RAISE, because a containment verdict
        computed from possibly-aliased paths is worse than no verdict."""
        api = self._win32()
        buf = api["ctypes"].create_unicode_buffer(32768)
        written = api["k32"].GetLongPathNameW(path, buf, 32768)
        if not written:
            raise WindowsOpsError("path_normalisation_failed")
        return buf.value

    def _open_process(self, api, pid):
        """Returns ``(handle_or_None, state)`` where state is ``opened`` | ``denied`` | ``gone`` |
        ``unknown``. The GRANTED access level is deliberately NOT surfaced: the handle is used only for the
        path (``QueryFullProcessImageNameW``, which the LIMITED right satisfies), and the owner SID is read
        from a SEPARATELY-opened token (``_user_sid``), so callers never need to know which right was granted
        here. Microsoft's own pages disagree on whether ``PROCESS_QUERY_INFORMATION`` or the LIMITED variant
        suffices, so the stronger right is requested first and the weaker used as a fallback."""
        ctypes, k32 = api["ctypes"], api["k32"]
        for rights in (PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION):
            handle = k32.OpenProcess(rights, False, pid)
            if handle:
                return handle, "opened"
        # Returned, never stashed on self: one adapter instance is shared by concurrent requests, so an
        # instance attribute used as an out-parameter can be overwritten by another thread between write
        # and read — reintroducing exactly the false-ABSENT this classification exists to prevent.
        return None, classify_open_process_error(ctypes.get_last_error())

    @staticmethod
    def _image_path(api, handle):
        ctypes, wintypes, k32 = api["ctypes"], api["wintypes"], api["k32"]
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value

    @staticmethod
    def _creation_filetime(api, handle) -> int:
        ctypes, wintypes, k32 = api["ctypes"], api["wintypes"], api["k32"]
        creation, exit_t, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            raise WindowsOpsError("process_times_unavailable")
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime

    @staticmethod
    def _session_id(api, pid):
        """[R:processidtosessionid-requires-pqi] Documented to need the FULL PROCESS_QUERY_INFORMATION, but
        it takes a PID rather than a handle, so the granted right cannot be passed in. Failure yields None:
        session is evidence, never a gate."""
        ctypes, wintypes, k32 = api["ctypes"], api["wintypes"], api["k32"]
        session = wintypes.DWORD()
        if not k32.ProcessIdToSessionId(pid, ctypes.byref(session)):
            return None                                  # evidence field, not a gate
        return session.value

    def _user_sid(self, pid):
        try:
            import win32api
            import win32con
            import win32security
        except ImportError:
            return None
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
            token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
            sid, _attr = win32security.GetTokenInformation(token, win32security.TokenUser)
            return str(win32security.ConvertSidToStringSid(sid))
        except Exception:
            return None                                  # evidence field: absent, never wrong

    # ── the one method with no supported implementation ──────────────────────────────────────────────
    def _enumerate_slot_files(self, canonical_root: str) -> list:
        """Every REGULAR file beneath the slot tree, long-path form.

        Any reparse point (junction/symlink) on a directory OR a file raises: a reparse could redirect the
        probe to a target OUTSIDE the slot (e.g. the production tree), so the whole check fails closed rather
        than register an out-of-slot resource or silently skip a real holder. An enumeration/stat failure
        also raises — a partial file list would under-probe and could manufacture a false 'clear'.
        """
        REPARSE = 0x400   # FILE_ATTRIBUTE_REPARSE_POINT
        def _is_reparse(st, link_probe):
            # On Windows a junction OR symlink carries FILE_ATTRIBUTE_REPARSE_POINT (st_file_attributes,
            # Windows-only). is_symlink()/islink() supplement it so the rejection is also exercisable on a
            # POSIX test host (where st_file_attributes is absent and getattr(...,0) is a no-op).
            return bool((getattr(st, "st_file_attributes", 0) & REPARSE) or link_probe)
        # The ROOT itself must be checked — the per-entry loop only guards children. A junction AT the slot
        # path (to production OR to another slot) is refused rather than walked through.
        try:
            root_st = os.lstat(canonical_root)
        except OSError as exc:
            raise WindowsOpsError("handle_observation_unavailable") from exc
        if _is_reparse(root_st, os.path.islink(canonical_root)):
            raise WindowsOpsError("reparse_point_in_tree")
        out, stack = [], [canonical_root]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError as exc:
                raise WindowsOpsError("handle_observation_unavailable") from exc
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError as exc:
                    raise WindowsOpsError("handle_observation_unavailable") from exc
                if _is_reparse(st, e.is_symlink()):
                    raise WindowsOpsError("reparse_point_in_tree")
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    out.append(self._long_path(e.path))
        return out

    def open_handles(self, path: str) -> bool:
        """Whether any live process holds a handle to a file inside the slot runtime tree.

        Uses the **Restart Manager** (``rstrtmgr.dll``): register the slot's FILES as resources and ask
        ``RmGetList`` which processes must be shut down to modify them — precisely the set of processes
        holding a handle open beneath the slot. RM was previously disqualified for *directories* (``RmGetList``
        rejects a bare directory); registering **files** is supported, and the service now runs as
        ``NT SERVICE\\GuvFXBetaAgent`` rather than LocalSystem. Bounded and synchronous — no ``NtQueryObject``
        hang path.

        Returns ``False`` (no holder) or ``True`` (>=1 holder). **Fails closed** — RAISES, which the cleanup
        precheck maps to *observation unavailable* and BLOCKS release — on any RM error, an enumeration
        failure, a reparse point in the tree, a path outside the slots root, or an empty tree it cannot
        probe. It never manufactures a 'clear'.

        Scope: only files enumerated from THIS slot path are registered, so a handle into another slot (or
        the production tree) can never match, and production paths are never inspected. (Limitation: a raw
        handle to a *directory* object — as opposed to a file within it — is not registerable with RM and is
        not separately detected; the mutating move that follows would itself fail on such a handle.)
        """
        # EXISTENCE FIRST — before any canonicalisation. GetLongPathNameW (self._long_path) returns 0 for a
        # path whose components are not all on disk, so canonicalising a GONE directory RAISES. verify_cleanup
        # calls open_handles(slot_path) AFTER the tombstone move, when the path is absent: a gone directory
        # holds nothing, so it must return False here, mirroring query_slot_process's path_exists-then-canon
        # order. (Off-host the fake path_exists governs this; on-host GetLongPathNameW is the reason.)
        if not self.path_exists(path):
            return False
        canonical = self._long_path(path)
        # SCOPE + REPARSE, via the FULLY RESOLVED real path: os.path.realpath dereferences any junction /
        # symlink in the slot path (which GetLongPathNameW does NOT), so a slot directory that is itself a
        # junction into the production tree resolves outside the slots root and is refused here — before
        # _enumerate_slot_files could ever scandir THROUGH it and inspect production files. The per-entry
        # reparse guard in _enumerate_slot_files then covers junctions on descendants.
        real = self._long_path(os.path.realpath(path))
        if not is_beneath_path(real, self._long_path(os.path.realpath(self.slots_root))):
            raise WindowsOpsError("open_handles_path_outside_slots_root")
        files = self._enumerate_slot_files(canonical)
        if not files:
            # RM cannot register a bare directory, so with zero files we cannot PROVE 'clear' — fail closed.
            raise WindowsOpsError("handle_observation_unavailable")
        ctypes = self._ctypes()
        from ctypes import wintypes
        rm = ctypes.WinDLL("rstrtmgr", use_last_error=True)
        rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
        rm.RmStartSession.restype = wintypes.DWORD
        rm.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
                                           wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
                                           ctypes.POINTER(wintypes.LPCWSTR)]
        rm.RmRegisterResources.restype = wintypes.DWORD
        rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
                                 ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        rm.RmGetList.restype = wintypes.DWORD
        rm.RmEndSession.argtypes = [wintypes.DWORD]
        rm.RmEndSession.restype = wintypes.DWORD
        ERROR_SUCCESS, ERROR_MORE_DATA = 0, 234
        session = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(33)     # CCH_RM_SESSION_KEY (32) + 1
        rc = rm.RmStartSession(ctypes.byref(session), 0, key)
        if rc != ERROR_SUCCESS:
            raise WindowsOpsError("handle_observation_unavailable") from RuntimeError("RmStartSession rc=%d" % rc)
        try:
            CHUNK = 512                            # accumulate all files across several register calls
            for i in range(0, len(files), CHUNK):
                chunk = files[i:i + CHUNK]
                arr = (wintypes.LPCWSTR * len(chunk))(*chunk)
                rc = rm.RmRegisterResources(session.value, len(chunk), arr, 0, None, 0, None)
                if rc != ERROR_SUCCESS:
                    raise WindowsOpsError("handle_observation_unavailable") from RuntimeError("RmRegisterResources rc=%d" % rc)
            needed = wintypes.UINT(0); have = wintypes.UINT(0); reasons = wintypes.DWORD(0)
            rc = rm.RmGetList(session.value, ctypes.byref(needed), ctypes.byref(have), None,
                              ctypes.byref(reasons))
            if rc not in (ERROR_SUCCESS, ERROR_MORE_DATA):
                raise WindowsOpsError("handle_observation_unavailable") from RuntimeError("RmGetList rc=%d" % rc)
            return needed.value > 0                # >=1 process must close to modify a slot file => held
        finally:
            rm.RmEndSession(session.value)
