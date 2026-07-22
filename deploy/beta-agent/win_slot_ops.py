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

from win_ops import SlotWindowsOps, WindowsOpsError

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
    raise WindowsOpsError("ambiguous_slot_process")


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


class RealSlotWindowsOps(SlotWindowsOps):
    """Box implementation. Untested on Windows; every method fails closed when it cannot be certain.

    Windows modules are imported lazily so this file is importable (and testable) anywhere. Off-host, every
    method raises :class:`WindowsApiUnavailable` — it never returns a plausible-looking answer.
    """

    def __init__(self, *, golden_dir: str, slots_root: str, hash_chunk: int = 1 << 16):
        self.golden_dir = golden_dir
        self.slots_root = slots_root
        self.hash_chunk = hash_chunk

    # ── lazy Win32 access ──
    @staticmethod
    def _ctypes():
        if os.name != "nt":
            raise WindowsApiUnavailable("not running on Windows")
        import ctypes
        return ctypes

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
        ctypes = self._ctypes()
        probe = self._nearest_existing(path)
        buf = ctypes.create_unicode_buffer(260)
        if not ctypes.windll.kernel32.GetVolumePathNameW(probe, buf, 260):
            raise WindowsOpsError("volume_path_unavailable")
        mount = buf.value
        if not mount.endswith("\\"):
            mount += "\\"                                # GetVolumeNameForVolumeMountPointW REQUIRES this
        guid = ctypes.create_unicode_buffer(60)
        if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(mount, guid, 60):
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
        return {"digest": self._tree_digest(self.golden_dir),
                "manifest_version": self._read_marker(
                    os.path.join(self.golden_dir, GOLDEN_MANIFEST_FILE)) or ""}

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
        entries = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            for name in list(dirnames):
                if self._is_reparse(os.path.join(dirpath, name)):
                    raise WindowsOpsError("reparse_point_in_tree")
            for name in filenames:
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
            # The exit code is the only thing recorded: robocopy output can contain full paths.
            raise WindowsOpsError(f"golden_copy_failed_rc_{completed.returncode}")

    # ── task scheduler ───────────────────────────────────────────────────────────────────────────────
    def _folder(self):
        client, _pywintypes = self._com()
        service = client.Dispatch("Schedule.Service")        # [R:progid-schedule-service]
        service.Connect()
        return service.GetFolder("\\")

    def _registered_task(self, task_name: str):
        """[R:cannot-missing-task-hresult] No Microsoft source maps a specific HRESULT to an absent task, so
        a failed ``GetTask`` cannot be read as "not there". Enumeration is used instead: absence from the
        folder listing is POSITIVE evidence of absence; anything else propagates as unreadable."""
        folder = self._folder()
        for task in folder.GetTasks(0):
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
        task.Run(None)                                   # pywin32 raises com_error on a FAILED hresult
        return True

    # ── process observation ──────────────────────────────────────────────────────────────────────────
    def query_slot_process(self, slot_path: str):
        """Find the runtime process by IMAGE-PATH CONTAINMENT.

        [R:toolhelp-name-not-path] Never by executable name: ``PROCESSENTRY32W.szExeFile`` is a bare name,
        and matching ``terminal64.exe`` by name would match the operator's production MT5.

        [R:creationtime-filetime-units] Creation time is read as a raw 64-bit FILETIME (100-ns ticks since
        1601). [R:psutil-createtime-loses-precision] psutil's ``create_time()`` is a float of seconds since
        1970 and 58% of tick values do not round-trip through it — unusable for process-birth identity.
        """
        ctypes = self._ctypes()
        from ctypes import wintypes
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        canonical_slot = self._long_path(slot_path)
        candidates = []
        for pid in self._enum_pids(ctypes, psapi):
            handle, rights = self._open_process(ctypes, k32, pid)
            if handle is None:
                continue
            try:
                image = self._image_path(ctypes, wintypes, k32, handle)
                if image is None or not is_beneath_path(self._long_path(image), canonical_slot):
                    continue
                candidates.append({
                    "pid": pid,
                    "created_at_filetime": self._creation_filetime(ctypes, wintypes, k32, handle),
                    "image": image,
                    "image_digest": self._file_digest(image) if self.path_exists(image) else None,
                    "user_sid": self._user_sid(pid),
                    "session_id": self._session_id(ctypes, wintypes, k32, pid, rights),
                })
            finally:
                k32.CloseHandle(handle)
        return select_slot_process(candidates, slot_path)

    def _long_path(self, path: str) -> str:
        """[R:short-name-83-aliasing] 8.3 aliasing may or may not be enabled on the target volume — it is
        per-volume configurable and unknowable off-host. Both sides of a containment comparison are
        normalised to their long form; if normalisation fails we RAISE, because a containment verdict
        computed from possibly-aliased paths is worse than no verdict."""
        ctypes = self._ctypes()
        buf = ctypes.create_unicode_buffer(32768)
        written = ctypes.windll.kernel32.GetLongPathNameW(path, buf, 32768)
        if not written:
            raise WindowsOpsError("path_normalisation_failed")
        return buf.value

    @staticmethod
    def _enum_pids(ctypes, psapi):
        from ctypes import wintypes
        size = 1024
        while True:
            arr = (wintypes.DWORD * size)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
                raise WindowsOpsError("process_enumeration_failed")
            if needed.value < ctypes.sizeof(arr):
                return [p for p in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)] if p]
            size *= 2                                    # grow-and-retry: the documented pattern

    @staticmethod
    def _open_process(ctypes, k32, pid):
        """[R:openprocesstoken-access-right] Microsoft's own pages disagree on whether
        PROCESS_QUERY_INFORMATION or the LIMITED variant suffices, and ProcessIdToSessionId documents the
        FULL right. The stronger right is requested first and the weaker used as a fallback, with the
        granted level returned so callers know what they may attempt."""
        for rights in (PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION):
            handle = k32.OpenProcess(rights, False, pid)
            if handle:
                return handle, rights
        code = ctypes.get_last_error() or k32.GetLastError()
        if classify_open_process_error(code) == "gone":
            return None, 0                               # raced with exit — genuinely not there
        if classify_open_process_error(code) == "denied":
            return None, 0                               # exists but opaque; never counted as absent
        raise WindowsOpsError("process_open_failed")

    @staticmethod
    def _image_path(ctypes, wintypes, k32, handle):
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value

    @staticmethod
    def _creation_filetime(ctypes, wintypes, k32, handle) -> int:
        creation, exit_t, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            raise WindowsOpsError("process_times_unavailable")
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime

    @staticmethod
    def _session_id(ctypes, wintypes, k32, pid, rights):
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
    def open_handles(self, path: str) -> bool:
        """**Fails closed, by design.** [R:no-fully-reliable-handle-check]

        Every documented route is disqualified: Restart Manager rejects directories outright
        (``ERROR_ACCESS_DENIED`` at ``RmGetList``) *and* cannot act on another session from a LocalSystem
        service; ``NtQuerySystemInformation`` is documented as internal and subject to change; ``openfiles``
        needs a reboot to enable.

        Returning ``False`` here would manufacture a cleanup proof out of nothing, so this raises and the
        ``no_runtime_handles`` proof is recorded UNMET — which blocks slot release. That is deliberate: see
        ``docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md`` §5, which records this as a decision for Nuno rather than
        one for me.
        """
        raise WindowsOpsError("handle_enumeration_unsupported")
