"""CVM-Inc-3 B3P-2 — READ-ONLY Windows primitives (stages 1–3).

Scope, deliberately narrow (architecture rule): a primitive knows a **slot number, a fixed slot directory
and fixed task names**. It never learns a runtime UUID, generation, occupancy id, ProvisioningJob, tenant,
entitlement or allocation policy — those are structurally absent from its input.

This module contains ONLY the three read-only stages:

1. task-definition inspection
2. process observation
3. filesystem containment and reparse-point validation

Nothing here creates, writes, starts, stops, repairs or alters anything. Mutating stages (stage copy,
launch, terminate, tombstone) are a later increment and are not present.

ABSENCE IS NOT SUCCESS — every observation reports one of :data:`OBSERVATION_STATUSES`, so "the task is
missing" can never be collapsed into "the task is invalid", and "I could not look" can never be collapsed
into "it is not running".
"""
import hashlib
import json
import re
from dataclasses import dataclass

from lib.mgmt_agent_core import AgentError, is_beneath
from win_ops import MultipleSlotProcesses, WindowsOpsError

PRIMITIVE_VERSION = "win-primitives-1.0.0"

#: The ONLY namespace a slot path may ever be derived from.
BETA_SLOTS_ROOT = r"C:\GuvFX\beta\slots"

#: The fixed launch wrapper (ADR-0016). The launch task runs powershell.exe against THIS file (admin-only,
#: hash-pinned by install_pool.ps1); the F3 launch gate asserts the task invokes exactly it via -File.
BETA_LAUNCHER_WRAPPER = r"C:\GuvFX\beta\launcher\slot_launch.ps1"
def _is_inline_command_switch(token: str) -> bool:
    """True if ``token`` is a powershell.exe switch that would run an INLINE command instead of the fixed
    ``-File`` wrapper. powershell.exe resolves any UNAMBIGUOUS prefix of a parameter name, so ``-com``/``-en``
    invoke ``-Command``/``-EncodedCommand`` just as the full names do — an exact-token blocklist would miss
    them. ``-ExecutionPolicy`` (which the wrapper invocation legitimately carries) must NEVER be caught:
    ``-encodedcommand`` and ``-executionpolicy`` diverge at the third character (``-en`` vs ``-ex``), so any
    prefix of length >= 3 is unambiguous, and bare ``-e`` resolves to ``-EncodedCommand`` (not
    ``-ExecutionPolicy``) at the powershell.exe CLI."""
    t = token.lower()
    if len(t) < 2 or not t.startswith("-"):
        return False
    if "-command".startswith(t):                          # -c, -co, ..., -command
        return True
    if t in ("-e", "-ec"):                                # documented powershell.exe -EncodedCommand aliases
        return True
    if len(t) >= 3 and "-encodedcommand".startswith(t):   # -enc, ..., -encodedcommand (never -ex...)
        return True
    return False

#: Observation outcomes. These are distinct states, never conflated. MULTIPLE_MATCHING (ADR-0015) is a
#: fail-closed state of its own — several fully-attributed slot processes — never collapsed into
#: UNAVAILABLE nor resolved by picking one.
PRESENT_VALID = "present_valid"
ABSENT = "absent"
PRESENT_INVALID = "present_invalid"
UNAVAILABLE = "observation_unavailable"
PERMISSION_DENIED = "permission_denied"
MULTIPLE_MATCHING = "multiple_matching_processes"
OBSERVATION_STATUSES = (PRESENT_VALID, ABSENT, PRESENT_INVALID, UNAVAILABLE, PERMISSION_DENIED,
                        MULTIPLE_MATCHING)

#: Paths/identities/ports the beta primitives must never derive or inspect. Matched case-insensitively as
#: substrings of a normalised path, so a resolver bug cannot quietly point at the operator's estate.
FORBIDDEN_FRAGMENTS = (
    r"c:\guvfx\accounts",
    r"c:\guvfx\terminals",
    r"program files\is6",
    r"metatrader",
    r"c:\guvfx\mt5_signal_bridge",
    "8788",
    "8787",
)
FORBIDDEN_IDENTITIES = ("administrator", "system", "guvfx-rdp")
#: Fixed per-slot runtime accounts. Pre-created by the operator; the agent never mints an identity.
RUNTIME_IDENTITY_PREFIX = "guvfx_b_slot"
FORBIDDEN_TASK_NAMES = ("guvfx_autostart", "guvfx_signalbridge", "guvfx_bridgewatchdog",
                        "guvfx_launchmt5", "gfx_launchis6", "guvfx_mt5test")


class UnauthorisedNamespace(AgentError):
    """A path, identity or task outside the authorised fixed beta-slot namespace was requested."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("unauthorised_namespace")


# ── primitive capability declaration ───────────────────────────────────────────────────────────────────
READ_ONLY = "read_only"
MUTATING = "mutating"
CAPABILITIES = (READ_ONLY, MUTATING)


class CapabilityViolation(AgentError):
    """A mutating helper was invoked from a READ_ONLY primitive context."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("capability_violation")


@dataclass(frozen=True)
class PrimitiveContext:
    """Declared capability of the primitive currently executing.

    A mechanical safeguard against regression: every mutating helper calls :func:`require_mutating`, so a
    future edit that calls one from an observation path fails loudly instead of quietly writing to the
    operator's host.
    """
    capability: str = READ_ONLY

    def __post_init__(self):
        if self.capability not in CAPABILITIES:
            raise ValueError(f"unknown capability: {self.capability}")


READ_ONLY_CONTEXT = PrimitiveContext(READ_ONLY)
MUTATING_CONTEXT = PrimitiveContext(MUTATING)


def require_mutating(ctx: PrimitiveContext, operation: str) -> None:
    """Refuse a mutating helper unless the caller declared MUTATING capability."""
    if ctx is None or ctx.capability != MUTATING:
        raise CapabilityViolation(operation)


# ── requirement 1: immutable primitive input ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SlotInput:
    """IMMUTABLE slot-scoped primitive input.

    ``frozen=True`` means a primitive cannot mutate ``slot``, ``slot_path``, ``launch_task`` or
    ``terminate_task``, and the upper layer cannot alter an in-flight operation's view by mutating a shared
    object — :meth:`from_scoped_view` takes a defensive copy of the caller's dict at construction.
    """
    slot: int
    slot_path: str
    launch_task: str
    terminate_task: str
    #: The fixed non-admin account this slot's runtime executes as, created once by the operator at the
    #: install gate. Derived from the slot number, never supplied by a caller.
    runtime_identity: str = ""

    @classmethod
    def from_scoped_view(cls, view: dict) -> "SlotInput":
        v = dict(view)                                  # defensive copy: later caller mutation is inert
        obj = cls(slot=int(v["slot"]), slot_path=str(v["slot_path"]),
                  launch_task=str(v["launch_task"]), terminate_task=str(v["terminate_task"]),
                  runtime_identity=str(v.get("runtime_identity") or ""))
        assert_authorised_slot_input(obj)
        return obj


def resolve_slot_input(slot: int) -> SlotInput:
    """Derive the fixed slot paths and task names from the slot NUMBER ALONE.

    No caller-supplied path or name is accepted, so a primitive target can never be steered and can never
    address anything outside the authorised beta namespace.
    """
    n = int(slot)
    if n < 1:
        raise UnauthorisedNamespace("slot must be >= 1")
    obj = SlotInput(slot=n,
                    slot_path=rf"{BETA_SLOTS_ROOT}\{n}\terminal",
                    launch_task=f"GuvFXBetaRuntime-{n}",
                    terminate_task=f"GuvFXBetaRuntimeStop-{n}",
                    runtime_identity=f"{RUNTIME_IDENTITY_PREFIX}{n}")
    assert_authorised_slot_input(obj)
    return obj


def _account_component(identity) -> str:
    name = str(identity or "").strip().lower()
    return name.rsplit("\\", 1)[-1].split("@", 1)[0]


def assert_authorised_slot_input(si: SlotInput) -> None:
    """Refuse anything outside the authorised fixed beta-slot namespace (production-exclusion guard)."""
    low = si.slot_path.replace("/", "\\").lower()
    # ``is_beneath`` is a LEXICAL prefix test: it does not resolve ".." traversal, so a path such as
    # ``C:\GuvFX\beta\slots\..\..\accounts`` would otherwise pass containment while escaping the namespace.
    # Reject traversal outright rather than trying to normalise it (found by the production-exclusion test).
    if any(part == ".." for part in low.split("\\")):
        raise UnauthorisedNamespace("path traversal component")
    if not is_beneath(si.slot_path, BETA_SLOTS_ROOT):
        raise UnauthorisedNamespace("slot_path outside beta slots root")
    for frag in FORBIDDEN_FRAGMENTS:
        if frag in low:
            raise UnauthorisedNamespace("forbidden path fragment")
    identity = _account_component(si.runtime_identity)
    if identity:
        if identity in FORBIDDEN_IDENTITIES:
            raise UnauthorisedNamespace("forbidden runtime identity")
        if not identity.startswith(RUNTIME_IDENTITY_PREFIX):
            raise UnauthorisedNamespace("identity outside the beta identity namespace")
    for task in (si.launch_task, si.terminate_task):
        t = task.lower()
        if not t.startswith("guvfxbetaruntime"):
            raise UnauthorisedNamespace("task outside beta task namespace")
        for forbidden in FORBIDDEN_TASK_NAMES:
            if forbidden in t:
                raise UnauthorisedNamespace("production task name")


# ── requirement 2: minimal local attestation ───────────────────────────────────────────────────────────
#: Keys a local attestation may carry. Occupancy identity is deliberately absent — the UPPER layer binds
#: this local attestation to the full occupancy.
#: B3P-2 requirements 1/3/5 add three fields: the lifecycle STATUS of the stage, the failure CATEGORY the
#: reason code maps to, and whether that classification was authoritative. They carry no occupancy identity,
#: so the boundary below is unchanged.
ATTESTATION_FIELDS = ("slot", "operation", "observed_at", "primitive_version", "outcome",
                      "reason_code", "evidence_digest", "stage_status", "failure_category",
                      "classification_complete")
FORBIDDEN_ATTESTATION_FIELDS = ("runtime_uuid", "generation", "occupancy_id", "provisioning_job_id",
                                "tenant", "entitlement", "owner_email", "owner_user_id")


def evidence_digest(evidence: dict) -> str:
    return hashlib.sha256(
        json.dumps(evidence or {}, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()[:16]


def attest(*, slot: int, operation: str, outcome: str, reason_code: str, evidence: dict,
           observed_at, stage_status: str = "") -> dict:
    """Build the minimal local attestation envelope wrapping a primitive observation.

    ``stage_status`` is empty for the read-only observation primitives — they are not lifecycle stages and
    saying otherwise would misrepresent them. Every mutating stage supplies one.

    The failure category is derived here rather than passed in, so a reason code can never be filed under
    two categories by two different call sites. Classification uses the non-strict mode: an unknown code
    degrades to INTEGRITY (the conservative reading) and ``classification_complete=False`` records that the
    category was a fallback rather than an authoritative mapping.
    """
    from lifecycle import STAGE_STATUSES, classify, is_classified
    if stage_status and stage_status not in STAGE_STATUSES:
        raise ValueError(f"unknown stage status: {stage_status}")
    return {
        "slot": int(slot),
        "operation": operation,
        "observed_at": observed_at,
        "primitive_version": PRIMITIVE_VERSION,
        "outcome": outcome,
        "reason_code": reason_code,
        "evidence_digest": evidence_digest(evidence),
        "stage_status": stage_status,
        "failure_category": classify(reason_code, strict=False),
        "classification_complete": is_classified(reason_code),
    }


# ── requirement 3: explicit time representation ────────────────────────────────────────────────────────
#: Windows reports process creation time at 100-ns FILETIME resolution. Equality is the rule; this
#: tolerance exists only for the documented API precision boundary, never for clock skew.
CREATION_TIME_TOLERANCE_TICKS = 0


def creation_time_matches(recorded, observed, tolerance_ticks: int = CREATION_TIME_TOLERANCE_TICKS) -> bool:
    """Compare process creation times as STABLE MACHINE-READABLE values (integer FILETIME ticks).

    Never compares locale-formatted or low-resolution strings, and never infers identity from clock skew:
    the comparison is equality (optionally within a documented Windows API precision boundary), not a
    comparison against backend wall-clock expectations.
    """
    if recorded is None or observed is None:
        return False
    if not isinstance(recorded, int) or not isinstance(observed, int):
        return False                                    # refuse string/locale comparison outright
    return abs(recorded - observed) <= max(0, int(tolerance_ticks))


# ── stage 1: task-definition inspection (READ ONLY) ────────────────────────────────────────────────────
def inspect_task(win, si: SlotInput, *, which: str = "launch", observed_at=None) -> dict:
    """Inspect ONE fixed per-slot task. Performs no repair, no creation, no state change.

    Distinguishes: task present and parseable; task **absent**; task present but invalid; observation
    unavailable; permission denied.
    """
    assert_authorised_slot_input(si)
    task_name = si.launch_task if which == "launch" else si.terminate_task
    operation = f"inspect_task:{which}"
    try:
        raw = win.query_task(task_name)
    except PermissionError:
        return _wrap(si, operation, PERMISSION_DENIED, "task_permission_denied", {}, observed_at)
    except Exception:
        return _wrap(si, operation, UNAVAILABLE, "task_observation_unavailable", {}, observed_at)
    if raw is None:
        # ABSENCE IS NOT INVALIDITY: a missing task is reported as absent, never as "valid = false".
        return _wrap(si, operation, ABSENT, "task_absent", {"task_name": task_name}, observed_at)

    from occupancy import task_definition_digest
    required = ("task_name", "run_as_identity", "executable", "working_directory", "arguments",
                "logon_type", "run_level", "enabled")
    missing = [k for k in required if raw.get(k) is None]
    evidence = {
        "task_name": raw.get("task_name"),
        "definition_digest": task_definition_digest(raw) if not missing else None,
        "run_as_identity": raw.get("run_as_identity"),
        "run_as_sid": raw.get("run_as_sid"),
        "executable": raw.get("executable"),
        "working_directory": raw.get("working_directory"),
        "arguments": raw.get("arguments"),
        # Portable mode is decided by the command line at launch, so this is the authoritative signal that
        # the runtime will keep its state inside the slot. It was computed by the adapter and then dropped
        # here, making every gate report it as null — indistinguishable from "not portable".
        "portable_switch": raw.get("portable_switch"),
        "logon_type": raw.get("logon_type"),
        "run_level": raw.get("run_level"),
        "enabled": raw.get("enabled"),
        "last_result": raw.get("last_result"),
    }
    if missing:
        return _wrap(si, operation, PRESENT_INVALID, "task_definition_incomplete", evidence, observed_at)
    if str(raw.get("task_name")) != task_name:
        return _wrap(si, operation, PRESENT_INVALID, "task_name_mismatch", evidence, observed_at)
    # Compare the ACCOUNT component: a principal arrives as ``DOMAIN\user``, ``user@domain`` or a bare
    # name, and exact membership against bare names would let ``.\Administrator`` through untouched.
    if _account_component(raw.get("run_as_identity")) in FORBIDDEN_IDENTITIES:
        return _wrap(si, operation, PRESENT_INVALID, "forbidden_run_as_identity", evidence, observed_at)
    # Containment means a different thing for each task family. BOTH now run powershell.exe from System32 (it
    # can never be inside the slot), so for BOTH what bounds the task is its ARGUMENT string.
    #
    #   LAUNCH    (ADR-0016) runs the fixed, admin-only, hash-pinned wrapper AS the slot identity; the wrapper
    #             launches this slot's own terminal64 and grants the service query access to that one process.
    #             The args must name the fixed wrapper (-File), this slot's own terminal64 (prefix-safe), a
    #             well-formed service-SID grantee (SHAPE only), be portable, and carry no inline command.
    #   TERMINATE runs `Stop-Process -Force` filtered on this slot's own executable path - the only thing
    #             separating it from the operator's production terminal, which carries the same image name.
    executable = str(raw.get("executable") or "")
    arguments = str(raw.get("arguments") or "")
    if which == "launch":
        if _account_component(executable).lower() not in ("powershell.exe", "pwsh.exe"):
            return _wrap(si, operation, PRESENT_INVALID, "launch_executable_unexpected", evidence, observed_at)
        low = arguments.lower()
        if ('-file "' + BETA_LAUNCHER_WRAPPER.lower() + '"') not in low:
            return _wrap(si, operation, PRESENT_INVALID, "launch_wrapper_unscoped", evidence, observed_at)
        # This slot's own terminal64, prefix-safe: ``...\slots\1\terminal`` is a substring of ``...\slots\10``,
        # so the full ``<slot_path>\terminal64.exe`` (slot_path already ends in \terminal) is required.
        if (si.slot_path.rstrip("\\").lower() + "\\terminal64.exe") not in low:
            return _wrap(si, operation, PRESENT_INVALID, "launch_scope_unbounded", evidence, observed_at)
        # The grantee must be SOME service SID (S-1-5-80- namespace). The primitive asserts SHAPE only and
        # never learns the specific GuvFX service SID value - that is bound above by the arguments digest.
        if not re.search(r"-granteesid\s+s-1-5-80-\d+-\d+-\d+-\d+-\d+(\s|$)", low):
            return _wrap(si, operation, PRESENT_INVALID, "launch_grantee_missing", evidence, observed_at)
        if not raw.get("portable_switch"):
            return _wrap(si, operation, PRESENT_INVALID, "launch_not_portable", evidence, observed_at)
        if any(_is_inline_command_switch(t) for t in arguments.split()):
            return _wrap(si, operation, PRESENT_INVALID, "launch_inline_command", evidence, observed_at)
    else:
        if _account_component(executable).lower() not in ("powershell.exe", "pwsh.exe"):
            return _wrap(si, operation, PRESENT_INVALID, "terminate_executable_unexpected", evidence,
                         observed_at)
        # The slot path must appear as a PATH PREFIX, not merely as a substring: ``...\slots\1`` is a
        # substring of ``...\slots\10``, so a bare containment test would accept slot 10's terminate
        # arguments as scoping slot 1. Requiring the trailing separator makes the boundary explicit.
        needle = si.slot_path.rstrip("\\").lower() + "\\"
        if needle not in arguments.lower():
            # An argument string that does not name this slot cannot be scoped to this slot.
            return _wrap(si, operation, PRESENT_INVALID, "terminate_scope_unbounded", evidence, observed_at)
    return _wrap(si, operation, PRESENT_VALID, "", evidence, observed_at)


# ── stage 2: process observation (READ ONLY) ───────────────────────────────────────────────────────────
def observe_process(win, si: SlotInput, *, observed_at=None) -> dict:
    """Observe the slot's runtime process. Never starts, stops or signals anything.

    Distinguishes an absent process from an observation that could not be made — a query failure is
    ``process_observation_unavailable``, never "not running".
    """
    assert_authorised_slot_input(si)
    operation = "observe_process"
    try:
        proc = win.query_slot_process(si.slot_path, si.runtime_identity)
    except MultipleSlotProcesses:
        # Several fully-attributed slot processes. A DISTINCT fail-closed state, never "not running" and
        # never merged into the generic "unavailable" below.
        return _wrap(si, operation, MULTIPLE_MATCHING, "multiple_matching_processes", {}, observed_at)
    except WindowsOpsError as exc:
        # A known observation failure (snapshot could not be taken/walked, or a plausible in-slot candidate
        # could not be attributed). Still UNAVAILABLE (never "not running"), but the SPECIFIC reason survives
        # into the attestation for diagnosis instead of being flattened to a single generic code.
        return _wrap(si, operation, UNAVAILABLE, exc.reason_code or "process_observation_unavailable", {},
                     observed_at)
    except PermissionError:
        return _wrap(si, operation, PERMISSION_DENIED, "process_permission_denied", {}, observed_at)
    except Exception:
        return _wrap(si, operation, UNAVAILABLE, "process_observation_unavailable", {}, observed_at)
    if proc is None:
        return _wrap(si, operation, ABSENT, "process_absent", {}, observed_at)

    image = str(proc.get("image") or "")
    contained = bool(image) and is_beneath(image, si.slot_path)
    created = proc.get("created_at_filetime")
    evidence = {
        "pid": proc.get("pid"),
        "created_at_filetime": created,                 # stable machine-readable (100-ns ticks)
        "image": image,
        "image_digest": proc.get("image_digest"),
        "image_containment_verified": contained,
        "user_sid": proc.get("user_sid"),
        "session_id": proc.get("session_id"),
        "slot": si.slot,
    }
    if not contained:
        return _wrap(si, operation, PRESENT_INVALID, "image_outside_slot", evidence, observed_at)
    if not isinstance(created, int):
        return _wrap(si, operation, PRESENT_INVALID, "creation_time_unusable", evidence, observed_at)
    return _wrap(si, operation, PRESENT_VALID, "", evidence, observed_at)


# ── stage 3: filesystem containment + reparse validation (READ ONLY) ───────────────────────────────────
def inspect_filesystem(win, si: SlotInput, *, observed_at=None) -> dict:
    """Inspect the slot directory tree. Creates nothing, opens nothing for write, alters no ACL.

    Reports reparse status for EVERY relevant path component, because a junction planted on an ancestor is
    as dangerous as one on the leaf.
    """
    assert_authorised_slot_input(si)
    operation = "inspect_filesystem"
    slot_root = si.slot_path.rsplit("\\", 1)[0]
    components = [BETA_SLOTS_ROOT, slot_root, si.slot_path]
    evidence = {"slot": si.slot, "components": []}
    try:
        for path in components:
            exists = bool(win.path_exists(path))
            real = win.real_path(path) if exists else None
            reparsed = bool(real) and real.replace("/", "\\").rstrip("\\").lower() != \
                path.replace("/", "\\").rstrip("\\").lower()
            escapes = bool(real) and not is_beneath(real, BETA_SLOTS_ROOT)
            entry = {"path_digest": hashlib.sha256(path.encode()).hexdigest()[:12],
                     "exists": exists, "is_reparse_point": reparsed, "escapes_namespace": escapes}
            try:
                entry["acl_observed"] = win.read_acl(path) if exists else None
                entry["acl_observation_failed"] = False
            except Exception:
                entry["acl_observed"] = None
                entry["acl_observation_failed"] = True   # readable-where-possible, reported where not
            evidence["components"].append(entry)
    except PermissionError:
        return _wrap(si, operation, PERMISSION_DENIED, "filesystem_permission_denied", evidence,
                     observed_at)
    except Exception:
        return _wrap(si, operation, UNAVAILABLE, "filesystem_observation_unavailable", evidence,
                     observed_at)

    root_entry, leaf_entry = evidence["components"][1], evidence["components"][2]
    evidence["slot_root_exists"] = root_entry["exists"]
    evidence["terminal_path_exists"] = leaf_entry["exists"]
    if any(c["escapes_namespace"] for c in evidence["components"]):
        return _wrap(si, operation, PRESENT_INVALID, "reparse_escapes_namespace", evidence, observed_at)
    if any(c["is_reparse_point"] for c in evidence["components"]):
        return _wrap(si, operation, PRESENT_INVALID, "reparse_point_present", evidence, observed_at)
    if not leaf_entry["exists"]:
        # Not materialised yet is a legitimate, distinct state — not a containment failure.
        return _wrap(si, operation, ABSENT, "terminal_path_absent", evidence, observed_at)
    evidence["containment_verified"] = True
    return _wrap(si, operation, PRESENT_VALID, "", evidence, observed_at)


def _wrap(si: SlotInput, operation: str, outcome: str, reason_code: str, evidence: dict,
          observed_at) -> dict:
    """Attach the minimal local attestation to an observation. Evidence is bounded and sanitised."""
    return {
        "attestation": attest(slot=si.slot, operation=operation, outcome=outcome,
                              reason_code=reason_code, evidence=evidence, observed_at=observed_at),
        "evidence": evidence,
    }
