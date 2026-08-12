"""hosted_workspace.workspace_acl — G5: the reusable per-user NTFS ACL engine (the testable brain).

The Beta Readiness Report named the missing "G5" artefact: the TX-1 per-user runtime tree
(``C:\\GuvFX\\accounts\\<id>``) is created with folders but NO explicit ACL, so it inherits the parent DACL
(BUILTIN\\Users read) — a second hosted identity could read another customer's ``accounts.dat``. That is the
ADR-0033 cross-tenant hard blocker.

This module is the *brain* of the fix: two PURE, fully-tested functions that any host-executor drives.

  * ``build_workspace_acl_plan(runtime_root, windows_username)`` — validates the target and returns the exact
    ACL contract to apply: break inheritance, then grant ONLY SYSTEM + Administrators (Full) + the workspace
    user (Modify). It REFUSES anything that is not a ``guvfx_u_<id>`` identity under the hosted accounts base
    (no admin, no traversal, no foreign path) — the primitive must never be pointed at the wrong tree.
  * ``verify_workspace_acl(rows, user_sid=…, protected=…)`` — the SID-typed read-back verifier. Given the DACL
    the host read back (as SIDs), it asserts the EXACT three-principal set, that inheritance is broken, and
    that each principal holds at least its required right — failing closed on ANY extra Allow principal (the
    cross-tenant leak). It runs a POSITIVE + NEGATIVE self-control every call (RULE 11): a "clean" verdict is
    only trusted once the classifier has been shown to accept a known-good DACL and reject a known-bad one, so
    "matched nothing" can never masquerade as "all clear".

The actual host mutation (icacls / Set-Acl, snapshot + rollback) lives in the ASCII-only host script
``terminal_provisioning/windows/Set-GuvfxWorkspaceAcl.ps1`` and is executed only by a signed host-executor.
Nothing here touches a host, resolves a live SID, or runs a command — it is pure policy + verification, so it
is exercised exhaustively in CI without a Windows box.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# STREAM 10D single-source-of-truth (Decision 2): the SAME canonical writable/code subdir lists drive BOTH the
# G5v2 NTFS ACL (below) AND the AppLocker W^X execute-denies. Never a duplicate manually-maintained list.
from hosted_workspace.applocker_policy import HOSTED_CODE_SUBDIRS, HOSTED_WRITABLE_SUBDIRS

# Well-known SIDs (language-independent; SID-typed so a localised "Administrators" name never matters).
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
BUILTIN_USERS_SID = "S-1-5-32-545"   # the exact principal whose inherited read is the leak we close

# The hosted per-user identity + runtime base (mirrors terminal_provisioning.services conventions).
ACCOUNTS_BASE = r"C:\GuvFX\accounts"
_USERNAME_RE = re.compile(r"^guvfx_u_[1-9][0-9]*$")


class AclError(Exception):
    """Refused/invalid ACL target — a controlled failure (never a silent pass)."""


class AclSelfCheckError(Exception):
    """The verifier's own positive/negative control failed — refuse to certify (RULE 11)."""


@dataclass(frozen=True)
class AclPlan:
    """The exact per-user ACL contract a host-executor must apply to ``runtime_root``.

    ``required`` maps the principal that MUST end up on the tree to its minimum right; the workspace user is
    carried by name (``windows_username``) because its SID is resolvable only on the host — the executor
    resolves it and applies + reads back SID-typed. ``break_inheritance`` is always True (the whole point)."""
    runtime_root: str
    windows_username: str
    break_inheritance: bool = True
    # SID → minimum right ("full"|"modify") for the two fixed principals. The user is added at apply time.
    required: tuple = field(default=((SYSTEM_SID, "full"), (ADMINISTRATORS_SID, "full")))
    user_min_right: str = "modify"


@dataclass(frozen=True)
class AclVerdict:
    ok: bool
    reason: str
    offenders: tuple = ()


# Stable, secret-free reason codes.
V_OK = "ok"
V_INHERITANCE = "inheritance_not_broken"
V_INHERITED_ACE = "inherited_allow_ace_present"
V_UNEXPECTED = "unexpected_allow_principal"
V_MISSING = "missing_required_principal"
V_RIGHTS = "insufficient_rights"


def build_workspace_acl_plan(runtime_root: str, windows_username: str) -> AclPlan:
    """Validate the target and return the ACL contract. Fail closed on anything that is not a hosted
    ``guvfx_u_<id>`` identity under the accounts base (never point the engine at the wrong tree / an admin)."""
    user = str(windows_username or "").strip()
    if not _USERNAME_RE.match(user):
        raise AclError("refusing ACL: not a hosted guvfx_u_<id> identity")
    account_id = user.rsplit("_", 1)[-1]                      # guvfx_u_<id>
    root = str(runtime_root or "").strip().replace("/", "\\").rstrip("\\")
    if not root:
        raise AclError("refusing ACL: empty runtime_root")
    if ".." in root:
        raise AclError("refusing ACL: path traversal in runtime_root")
    # Bind the identity to its tree: the runtime root MUST be exactly <accounts base>\<id> (the deterministic
    # terminal_provisioning mapping). This refuses BOTH an out-of-base/sibling tree AND a guvfx_u_5 vs
    # accounts\9 identity/tree mismatch — the engine can never be pointed at another account's directory.
    if root.lower() != f"{ACCOUNTS_BASE}\\{account_id}".lower():
        raise AclError("refusing ACL: runtime_root does not match the identity account id under the accounts base")
    return AclPlan(runtime_root=root, windows_username=user)


def _rights_satisfies(rights: str, minimum: str) -> bool:
    """True iff a read-back rights label meets the minimum. FullControl satisfies everything; Modify satisfies
    'modify'. Conservative: an unrecognised/lesser label does NOT satisfy (fail closed)."""
    r = (rights or "").lower().replace(" ", "")
    if "fullcontrol" in r:
        return True
    if minimum == "full":
        return False
    return "modify" in r


def _classify(rows, user_sid: str, protected: bool) -> AclVerdict:
    """Pure DACL classifier. ``rows`` = list of {sid, type(Allow/Deny), rights, inherited}. Evaluates only
    Allow ACEs (Deny ACEs cannot widen access, so they are not a leak)."""
    user_sid = str(user_sid or "")
    allow = [r for r in rows if str(r.get("type", "")).strip().lower() == "allow"]

    if not protected:
        return AclVerdict(False, V_INHERITANCE)
    # Defence in depth: even if the caller claims protected, an inherited Allow ACE is a leak.
    if any(bool(r.get("inherited")) for r in allow):
        return AclVerdict(False, V_INHERITED_ACE)

    principals = {str(r.get("sid", "")) for r in allow}
    expected = {SYSTEM_SID, ADMINISTRATORS_SID, user_sid}
    extra = principals - expected
    if extra:
        return AclVerdict(False, V_UNEXPECTED, tuple(sorted(extra)))
    missing = expected - principals
    if missing:
        return AclVerdict(False, V_MISSING, tuple(sorted(missing)))

    by_sid: dict[str, list[str]] = {}
    for r in allow:
        by_sid.setdefault(str(r.get("sid", "")), []).append(str(r.get("rights", "")))

    def _has(sid: str, minimum: str) -> bool:
        return any(_rights_satisfies(x, minimum) for x in by_sid.get(sid, []))

    if not _has(SYSTEM_SID, "full"):
        return AclVerdict(False, V_RIGHTS, (SYSTEM_SID,))
    if not _has(ADMINISTRATORS_SID, "full"):
        return AclVerdict(False, V_RIGHTS, (ADMINISTRATORS_SID,))
    if not _has(user_sid, "modify"):
        return AclVerdict(False, V_RIGHTS, (user_sid,))
    return AclVerdict(True, V_OK)


def _self_check() -> None:
    """RULE 11 positive + negative control: prove the classifier ACCEPTS a known-good DACL and REJECTS a
    known-bad one (an extra BUILTIN\\Users read). A 'clean' verdict is worthless unless the detector is live."""
    probe_sid = "S-1-5-21-0-0-0-1001"
    good = [
        {"sid": SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": probe_sid, "type": "Allow", "rights": "Modify", "inherited": False},
    ]
    bad = good + [{"sid": BUILTIN_USERS_SID, "type": "Allow", "rights": "ReadAndExecute", "inherited": False}]
    if not _classify(good, probe_sid, True).ok:
        raise AclSelfCheckError("positive control failed — verifier rejects a known-good DACL")
    if _classify(bad, probe_sid, True).ok:
        raise AclSelfCheckError("negative control failed — verifier accepts a leaking DACL")


def verify_workspace_acl(rows, *, user_sid: str, protected: bool = True) -> AclVerdict:
    """Verify a read-back DACL is EXACTLY {SYSTEM(Full), Administrators(Full), user(Modify)} with inheritance
    broken. Runs the positive/negative self-control first (RULE 11); raises ``AclSelfCheckError`` if the
    detector is not demonstrably live. Otherwise returns a verdict — fail-closed on any extra Allow principal
    (the cross-tenant leak) or any missing/under-privileged required principal."""
    _self_check()
    return _classify(list(rows or []), user_sid, protected)


# ══════════════════════════════════════════════════════════════════════════════════════════════════════════
# STREAM 10D — G5v2: the INVERTED (W^X) ACL contract (ADR-0043). CANDIDATE; G5v1 above stays the certified live
# contract until G5v2's own behavioural certification passes. Canonical invariant:
#     root + code dirs => tenant READ+EXECUTE only (executable => NON-writable)
#     enumerated data subdirs => tenant Modify (writable), and AppLocker execute-denies them (writable => NON-executable)
#     common.ini + code dirs => explicit tenant DENY-write (so AllowDllImport=0 is immutable and no .ex5 is plantable)
# ══════════════════════════════════════════════════════════════════════════════════════════════════════════

# common.ini is the AllowDllImport ceiling; it sits under the (writable) config\ subdir but is DENY-write to the
# tenant so the Options toggle / on-exit rewrite cannot flip AllowDllImport to 1.
COMMON_INI_RELPATH = r"terminal\config\common.ini"

V2_ROOT = ""   # the dict key for the runtime root DACL in a v2 read-back
V2_ROOT_NOT_RX_ONLY = "root_tenant_not_readexecute_only"
V2_WRITABLE_NOT_MODIFY = "writable_subdir_tenant_not_modify"
V2_CODE_NOT_DENIED = "code_or_common_ini_tenant_not_deny_write"
V2_MISSING_PATH = "missing_path_dacl"
V2_FOREIGN_PRINCIPAL = "foreign_allow_principal_on_subdir"


@dataclass(frozen=True)
class AclPlanV2:
    """The inverted per-user ACL contract a host-executor applies to ``runtime_root`` (G5v2). ``writable_subdirs``
    get tenant Modify; ``deny_write_paths`` get an explicit tenant Deny-write; everything else (root + code dirs)
    is tenant Read+Execute only. Consumes the canonical HOSTED_* lists so NTFS and AppLocker never diverge."""
    runtime_root: str
    windows_username: str
    break_inheritance: bool = True
    required: tuple = field(default=((SYSTEM_SID, "full"), (ADMINISTRATORS_SID, "full")))
    user_root_right: str = "readexecute"
    writable_subdirs: tuple = HOSTED_WRITABLE_SUBDIRS
    deny_write_paths: tuple = HOSTED_CODE_SUBDIRS + (COMMON_INI_RELPATH,)


def _validate_runtime_target(runtime_root: str, windows_username: str) -> tuple:
    """Shared target validation (identical rule to v1; duplicated so the certified v1 body stays untouched)."""
    user = str(windows_username or "").strip()
    if not _USERNAME_RE.match(user):
        raise AclError("refusing ACL: not a hosted guvfx_u_<id> identity")
    account_id = user.rsplit("_", 1)[-1]
    root = str(runtime_root or "").strip().replace("/", "\\").rstrip("\\")
    if not root:
        raise AclError("refusing ACL: empty runtime_root")
    if ".." in root:
        raise AclError("refusing ACL: path traversal in runtime_root")
    if root.lower() != f"{ACCOUNTS_BASE}\\{account_id}".lower():
        raise AclError("refusing ACL: runtime_root does not match the identity account id under the accounts base")
    return root, user


def build_workspace_acl_plan_v2(runtime_root: str, windows_username: str) -> AclPlanV2:
    """Validate the target and return the inverted (W^X) ACL contract. Same fail-closed target checks as v1."""
    root, user = _validate_runtime_target(runtime_root, windows_username)
    return AclPlanV2(runtime_root=root, windows_username=user)


def _has_write(rights: str) -> bool:
    r = (rights or "").lower().replace(" ", "")
    return any(t in r for t in ("write", "modify", "fullcontrol", "full", "append", "delete"))


def _has_readexecute(rights: str) -> bool:
    r = (rights or "").lower().replace(" ", "")
    return ("readandexecute" in r or "readexecute" in r or "modify" in r or "fullcontrol" in r
            or ("read" in r and "execute" in r))


def _deny_covers_write(rights: str) -> bool:
    """Immutability-grade Deny test: because the code dirs / common.ini INHERIT the tenant's Modify (config\\ is a
    writable subdir), a partial Deny (only Delete, or only Write) leaves an effective write path — the tenant
    edits AllowDllImport in place (Write not denied) or deletes+recreates the file (Delete not denied). So a Deny
    only counts if it denies BOTH in-place WRITE (WriteData/AppendData, rendered 'Write') AND DELETE (or is a
    Modify/FullControl Deny that subsumes both). Distinct from ``_has_write`` (which is any-write-token, used for
    the writable-subdir Allow check)."""
    r = (rights or "").lower().replace(" ", "")
    if "fullcontrol" in r or "modify" in r:
        return True
    has_write = any(t in r for t in ("write", "createfiles", "append"))
    return has_write and "delete" in r


def _v2_allows(rows, user_sid):
    return [r for r in rows if str(r.get("type", "")).strip().lower() == "allow"
            and str(r.get("sid", "")) == user_sid]


def _v2_denies(rows, user_sid):
    return [r for r in rows if str(r.get("type", "")).strip().lower() == "deny"
            and str(r.get("sid", "")) == user_sid]


def _classify_v2(path_dacls: dict, user_sid: str, plan: AclPlanV2) -> AclVerdict:
    """Verify a v2 read-back (dict: relpath -> DACL rows; "" = root) satisfies the inverted contract."""
    user_sid = str(user_sid or "")
    if V2_ROOT not in path_dacls:
        return AclVerdict(False, V2_MISSING_PATH, (V2_ROOT,))
    # 1. Root: exactly {SYSTEM, Admins, user}, inheritance broken, and the tenant is READ+EXECUTE ONLY (no write).
    root_v = _classify(path_dacls[V2_ROOT], user_sid, protected=True)
    if not root_v.ok and root_v.reason not in (V_RIGHTS,):
        return root_v                                    # inheritance / extra principal / missing — same as v1
    root_allows = _v2_allows(path_dacls[V2_ROOT], user_sid)
    if not root_allows or any(_has_write(a.get("rights", "")) for a in root_allows) \
            or not any(_has_readexecute(a.get("rights", "")) for a in root_allows):
        return AclVerdict(False, V2_ROOT_NOT_RX_ONLY)
    # The EXACT allowed principal set for every mutated path (no foreign Allow — e.g. an inherited BUILTIN\Users
    # write on a code dir would defeat the Deny-write / cross-tenant isolation).
    expected = {SYSTEM_SID, ADMINISTRATORS_SID, user_sid}

    def _foreign(rows):
        principals = {str(r.get("sid", "")) for r in rows
                      if str(r.get("type", "")).strip().lower() == "allow"}
        return tuple(sorted(principals - expected))

    # 2. Each writable data subdir: NO foreign Allow principal, and the tenant has Modify (write allowed there —
    # but AppLocker execute-denies it).
    for rel in plan.writable_subdirs:
        rows = path_dacls.get(rel)
        if rows is None:
            return AclVerdict(False, V2_MISSING_PATH, (rel,))
        extra = _foreign(rows)
        if extra:
            return AclVerdict(False, V2_FOREIGN_PRINCIPAL, extra)
        if not any(_has_write(a.get("rights", "")) for a in _v2_allows(rows, user_sid)):
            return AclVerdict(False, V2_WRITABLE_NOT_MODIFY, (rel,))
    # 3. Each code dir + common.ini: NO foreign Allow principal, and the tenant has an EXPLICIT Deny-write ACE
    # (so no .ex5 plant, no AllowDllImport flip).
    for rel in plan.deny_write_paths:
        rows = path_dacls.get(rel)
        if rows is None:
            return AclVerdict(False, V2_MISSING_PATH, (rel,))
        extra = _foreign(rows)
        if extra:
            return AclVerdict(False, V2_FOREIGN_PRINCIPAL, extra)
        if not any(_deny_covers_write(d.get("rights", "")) for d in _v2_denies(rows, user_sid)):
            return AclVerdict(False, V2_CODE_NOT_DENIED, (rel,))
    return AclVerdict(True, V_OK)


def _self_check_v2(plan: AclPlanV2) -> None:
    """RULE 11 positive + negative control for the inverted verifier: prove it ACCEPTS a known-good W^X read-back
    and REJECTS a known-bad one (the tenant given Modify at the root — the exact W^X violation)."""
    probe = "S-1-5-21-0-0-0-1001"
    root_ok = [
        {"sid": SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": probe, "type": "Allow", "rights": "ReadAndExecute", "inherited": False},
    ]
    good = {V2_ROOT: root_ok}
    for rel in plan.writable_subdirs:
        good[rel] = root_ok + [{"sid": probe, "type": "Allow", "rights": "Modify", "inherited": False}]
    for rel in plan.deny_write_paths:
        good[rel] = root_ok + [{"sid": probe, "type": "Deny", "rights": "Write, Delete", "inherited": False}]
    bad = {k: list(v) for k, v in good.items()}
    bad[V2_ROOT] = root_ok[:2] + [{"sid": probe, "type": "Allow", "rights": "Modify", "inherited": False}]
    if not _classify_v2(good, probe, plan).ok:
        raise AclSelfCheckError("v2 positive control failed — verifier rejects a known-good W^X DACL")
    if _classify_v2(bad, probe, plan).ok:
        raise AclSelfCheckError("v2 negative control failed — verifier accepts a tenant-writable-root DACL")


def verify_workspace_acl_v2(path_dacls: dict, *, user_sid: str, plan: AclPlanV2) -> AclVerdict:
    """Verify a v2 (W^X) read-back satisfies the inverted contract: root/code dirs tenant Read+Execute only,
    data subdirs tenant Modify, code dirs + common.ini explicit tenant Deny-write. Runs the RULE-11 self-control
    first (raises ``AclSelfCheckError`` if the detector is not demonstrably live), then returns a verdict —
    fail-closed on a tenant-writable root/code dir, a missing path, or a missing Deny-write."""
    _self_check_v2(plan)
    return _classify_v2(dict(path_dacls or {}), user_sid, plan)
