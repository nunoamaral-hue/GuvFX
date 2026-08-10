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
