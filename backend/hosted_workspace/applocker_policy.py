"""hosted_workspace.applocker_policy — Beta Readiness Stream 6 (M1): the multi-tenant AppLocker policy compiler.

Stream 5 found that `Set-GuvfxAppLocker.ps1 -Deploy` REPLACES the machine-wide policy (no `-Merge`), so
provisioning a second hosted tenant on the Customer-Zero host would wipe CZ's enforced hardening. This module
is the principled fix: a deterministic policy model that is **additive, isolated, idempotent and reversible**.

Model (matches the certified CZ policy `applocker/guvfx-hosted-auditonly.xml`):
  * BASE — the SHARED, machine-wide rules (Administrators Allow-* recovery; Everyone Allow %WINDIR% /
    %PROGRAMFILES% / MSI cache / the MetaQuotes publisher rule for the portable MT5). One copy, tenant-agnostic.
  * TENANT FRAGMENT — per account, the shell/escape DENY rules scoped to THAT account's `guvfx_u_<id>` SID, with
    rule IDs deterministically tagged with the account id so they can be added, re-added (idempotent) and removed
    without touching any other tenant or the base.

  effective = base + Σ tenant fragments.  Adding account N merges only N's denies; removing N strips only N's.

This is the "deterministic policy compiler" the packet calls for: certified base + declared tenant fragments →
complete effective policy, with compare-before-apply (``policy_changed``) and per-tenant rollback (``remove_tenant``).
It NEVER emits a writable-path executable Allow (the canonical bypass) and Customer Zero (account #1) can never be
removed through a tenant operation. Pure stdlib (ElementTree + re) so the Django side AND the host-side dispatcher
share one authoritative model; the host applier (`Set-GuvfxAppLockerTenant.ps1`) mirrors the same rule IDs.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Customer Zero (account #1) can never be removed via a tenant operation.
RESERVED_CUSTOMER_ZERO = frozenset({1})

# The shell/escape binaries denied to each hosted identity (verbatim from the certified template).
DENY_BINARIES = (
    "cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe", "explorer.exe", "regedit.exe",
    "mmc.exe", "taskmgr.exe", "wscript.exe", "cscript.exe", "mshta.exe", "control.exe",
)

HOSTED_SID_TOKEN = "{{HOSTED_USER_SID}}"
# A GuvFX tenant deny rule is identified by a fixed marker in the 3rd GUID group ('4d54' = 'MT') plus the
# account id (hex) in the 1st group — so removal/idempotency key on the account, never on a fragile name match.
_TENANT_MARKER = "4d54"
_SID_RE = re.compile(r"^S-1-\d+(-\d+)+$")     # a well-formed SID (rejects the {{…}} token and empty)
_MAX_ACCOUNT = 0x0FFFFFFF                      # fits the 8-hex GUID group; ~268M accounts, no 5-user cap


class AppLockerPolicyError(Exception):
    """Controlled policy-model failure (invalid account/SID, missing base collection, forbidden removal)."""


def _account(account_id) -> int:
    try:
        n = int(account_id)
    except (TypeError, ValueError):
        raise AppLockerPolicyError("account_id_not_int")
    if n <= 0 or n > _MAX_ACCOUNT:
        raise AppLockerPolicyError("account_id_out_of_range")
    return n


def tenant_rule_id(account_id, seq: int) -> str:
    """Deterministic AppLocker rule GUID for (account, seq): ``<acct:08x>-0000-4d54-0000-<seq:012x>``."""
    n = _account(account_id)
    return f"{n:08x}-0000-{_TENANT_MARKER}-0000-{int(seq):012x}"


def _is_tenant_rule(rule_id: str, account_id=None) -> bool:
    parts = str(rule_id or "").split("-")
    if len(parts) != 5 or parts[2].lower() != _TENANT_MARKER:
        return False
    if account_id is None:
        return True
    try:
        return int(parts[0], 16) == int(account_id)
    except ValueError:
        return False


def _exe_collection(root: ET.Element) -> ET.Element:
    for coll in root.findall("RuleCollection"):
        if coll.get("Type") == "Exe":
            return coll
    raise AppLockerPolicyError("policy_missing_exe_collection")


def _tostr(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def tenant_deny_rules(account_id, sid: str) -> list:
    """The list of Exe DENY rule elements for one account, scoped to ``sid``. Deterministic IDs; fail-closed on a
    malformed account or SID (a Deny scoped to a bad/empty/Everyone SID would be a cross-tenant hazard)."""
    n = _account(account_id)
    s = str(sid or "").strip()
    if not _SID_RE.match(s):
        raise AppLockerPolicyError("malformed_sid")
    if s in ("S-1-1-0", "S-1-5-11", "S-1-5-32-544"):        # Everyone / Authenticated Users / Administrators
        raise AppLockerPolicyError("refusing_deny_on_shared_principal")
    rules = []
    for i, binary in enumerate(DENY_BINARIES):
        rule = ET.Element("FilePathRule", {
            "Id": tenant_rule_id(n, 0x10 + i),
            "Name": f"(Hosted acct {n}) Deny {binary}",
            "Description": f"acct={n}", "UserOrGroupSid": s, "Action": "Deny"})
        cond = ET.SubElement(rule, "Conditions")
        ET.SubElement(cond, "FilePathCondition", {"Path": f"*\\{binary}"})
        rules.append(rule)
    return rules


def tenant_fragment(account_id, sid: str) -> str:
    """A minimal ``AppLockerPolicy`` carrying ONLY account N's Exe denies — the fragment ``Set-AppLockerPolicy
    -Merge`` adds to the existing machine policy (additive; touches no other rule).

    The Exe collection is ``EnforcementMode="NotConfigured"`` DELIBERATELY: on merge, a NotConfigured collection
    contributes its rules WITHOUT changing the target collection's enforcement mode. So merging N's fragment can
    never downgrade Customer Zero's Exe collection (whether it is AuditOnly or, later, Enabled/Enforce) — N's
    denies are simply evaluated under whatever mode the machine collection already carries."""
    root = ET.Element("AppLockerPolicy", {"Version": "1"})
    coll = ET.SubElement(root, "RuleCollection", {"Type": "Exe", "EnforcementMode": "NotConfigured"})
    for r in tenant_deny_rules(account_id, sid):
        coll.append(r)
    return _tostr(root)


def load_base_policy(template_xml: str) -> str:
    """Derive the shared BASE (the certified template with every hosted/tenant DENY rule stripped) — so the base
    always tracks the certified policy and can never carry a per-tenant rule."""
    root = ET.fromstring(template_xml)
    for coll in root.findall("RuleCollection"):
        for rule in list(coll):
            if rule.get("UserOrGroupSid", "") == HOSTED_SID_TOKEN or _is_tenant_rule(rule.get("Id", "")):
                coll.remove(rule)
    return _tostr(root)


def compile_effective_policy(base_xml: str, tenants) -> str:
    """base + each tenant's denies (deterministic, sorted by account, idempotent). ``tenants`` = iterable of
    ``(account_id, sid)``."""
    root = ET.fromstring(base_xml)
    exe = _exe_collection(root)
    seen = set()
    for account_id, sid in sorted(tenants, key=lambda t: int(t[0])):
        n = _account(account_id)
        if n in seen:
            continue
        seen.add(n)
        for r in tenant_deny_rules(n, sid):
            exe.append(r)
    return _tostr(root)


def merge_tenant(effective_xml: str, account_id, sid: str) -> str:
    """Add (or idempotently replace) account N's denies in an existing effective policy — models the host
    ``-Merge``. Leaves the base and every other tenant untouched."""
    n = _account(account_id)
    root = ET.fromstring(effective_xml)
    exe = _exe_collection(root)
    for rule in list(exe):
        if _is_tenant_rule(rule.get("Id", ""), n):
            exe.remove(rule)
    for r in tenant_deny_rules(n, sid):
        exe.append(r)
    return _tostr(root)


def remove_tenant(effective_xml: str, account_id) -> tuple:
    """Remove ONLY account N's tenant rules (per-tenant rollback). Refuses Customer Zero. Returns
    ``(xml, removed_count)``; never touches the base or another tenant."""
    n = _account(account_id)
    if n in RESERVED_CUSTOMER_ZERO:
        raise AppLockerPolicyError("customer_zero_removal_forbidden")
    root = ET.fromstring(effective_xml)
    removed = 0
    for coll in root.findall("RuleCollection"):
        for rule in list(coll):
            if _is_tenant_rule(rule.get("Id", ""), n):
                coll.remove(rule)
                removed += 1
    return _tostr(root), removed


def tenant_account_ids(xml: str) -> set:
    """The set of account ids that currently have tenant rules in ``xml``."""
    out = set()
    root = ET.fromstring(xml)
    for coll in root.findall("RuleCollection"):
        for rule in coll:
            rid = rule.get("Id", "")
            if _is_tenant_rule(rid):
                out.add(int(rid.split("-")[0], 16))
    return out


def policy_changed(current_xml: str, target_xml: str) -> bool:
    """Compare-before-apply: True iff the normalised policies differ (so the host applies only real deltas)."""
    return _canonical(current_xml) != _canonical(target_xml)


def _canonical(xml: str) -> str:
    root = ET.fromstring(xml)
    # order-independent: sort each collection's rules by Id
    for coll in root.findall("RuleCollection"):
        rules = sorted(list(coll), key=lambda r: r.get("Id", ""))
        for r in list(coll):
            coll.remove(r)
        for r in rules:
            coll.append(r)
    return _tostr(root)


# ══════════════════════════════════════════════════════════════════════════════════════════════════════════
# STREAM 10B — the canonical DENY-BY-DEFAULT allow model (ADR-0042). THE single source of truth for the hosted
# AppLocker surface. Replaces the legacy broad "(Everyone) Allow %WINDIR%/%PROGRAMFILES%" model (which left ~49
# %WINDIR% LOLBIN/interpreter primitives runnable by a hosted tenant — proven in STREAM 10 Phase A) with an
# explicit minimal allow-list; everything not listed is denied by default.
#
# Machine-wide, tenant-agnostic: every hosted tenant is only a member of Everyone, so the effective hosted
# surface is EXACTLY {MetaQuotes publisher} + {the curated RemoteApp/session infra below}. System, service and
# the dynamic per-session virtual accounts keep unrestricted Windows execution via their well-known (group) SIDs,
# so the OS / RDS / desktop compositor are unaffected. NO general-purpose interpreter is ever allowed to a hosted
# tenant (ADR-0041: a tenant that can run python/cmd/rundll32 can forge the observation — so the observer ships
# as a signed compiled EXE in STREAM 10C, NOT as tenant-run python).
# ══════════════════════════════════════════════════════════════════════════════════════════════════════════

ADMIN_SID = "S-1-5-32-544"
EVERYONE_SID = "S-1-1-0"
METAQUOTES_PUBLISHER_NAME = "O=METAQUOTES LTD., S=LEMESOS, C=CY"
# Microsoft OS-component signer. Hosted tenants may load Microsoft-signed OS DLLs (a signature cannot be forged)
# INSTEAD of a broad ``(Everyone) %WINDIR%\\*`` DLL path allow, which is bypassable via user-writable %WINDIR%
# subdirectories (``%WINDIR%\\Temp``, ``System32\\spool\\drivers\\color``, ...) — STREAM 10B re-verify HIGH. A
# Microsoft-signed DLL is trusted code, NOT an arbitrary-code primitive, so this publisher is allowed for the Dll
# collection ONLY (a Microsoft-signed EXE such as rundll32 WOULD be a signed-LOLBIN ACE, so it is never allowed in
# Exe/Msi/Script). SOAK-VERIFY the exact subject against the host's real DLL signatures before Enforce.
MICROSOFT_WINDOWS_PUBLISHER_NAME = "O=MICROSOFT CORPORATION, L=REDMOND, S=WASHINGTON, C=US"

# Principals that KEEP unrestricted Windows execution (deny-by-default does NOT constrain them). The dynamic
# per-session virtual accounts (DWM ``S-1-5-90-0-N``, Font Driver Host ``S-1-5-96-0-N``) are covered by their
# GROUP SIDs ``S-1-5-90-0`` / ``S-1-5-96-0`` — so removing the broad Everyone allow does not break the compositor.
SYSTEM_EXEC_SIDS = (
    ("S-1-5-18", "Local System"), ("S-1-5-19", "Local Service"), ("S-1-5-20", "Network Service"),
    ("S-1-5-6", "Service"), ("S-1-5-90-0", "Window Manager Group"), ("S-1-5-96-0", "Font Driver Host"),
)
_SYSTEM_ALSO_PF = frozenset({"S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-6"})   # also need Program Files

# The ONLY additional binaries a hosted RemoteApp MT5 session legitimately spawns AS the tenant: RemoteApp/RDS
# session infrastructure + the minimal interactive-session shell. All system-signed, NON-interpreter, NON-LOLBIN.
# Curated from the STREAM 10 workload capture (contamination — browsers/telemetry/rundll32 from a prior full
# desktop session — excluded). Validated by the AuditOnly soak; ANY addition requires re-certification.
HOSTED_SESSION_ALLOW = (
    "rdpinit.exe", "rdpshell.exe", "rdpclip.exe", "tstheme.exe",
    "userinit.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe", "conhost.exe",
    "shellappruntime.exe", "shellhost.exe", "wlrmdr.exe",
)

# Interpreters / compilers / LOLBINs that must NEVER be granted to a hosted tenant (a permanent regression guard;
# each is an arbitrary-code-execution primitive that would defeat ADR-0041). This is the belt-and-suspenders CI
# tripwire; the PRIMARY guard is now the positive allowlist in ``assert_allow_model_invariants`` (a tenant-reachable
# Allow must be EXACTLY the MetaQuotes publisher or a curated %SYSTEM32%\<leaf>). The extra names below were added
# after the STREAM 10B review flagged that a fixed blocklist could miss primitives like wsl/odbcconf/scriptrunner
# added to HOSTED_SESSION_ALLOW; the frozen-set change-detector test on HOSTED_SESSION_ALLOW backs this up.
FORBIDDEN_HOSTED_ALLOW = (
    "python.exe", "pythonw.exe", "py.exe", "pyw.exe", "cmd.exe", "powershell.exe", "powershell_ise.exe",
    "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe", "regsvcs.exe",
    "regasm.exe", "installutil.exe", "msbuild.exe", "csc.exe", "vbc.exe", "jsc.exe", "ilasm.exe", "cmstp.exe",
    "mavinject.exe", "bitsadmin.exe", "certutil.exe", "wmic.exe", "explorer.exe", "regedit.exe",
    # STREAM 10B review additions (LOLBAS execution/proxy primitives + interpreters not in the original set):
    "wsl.exe", "wslconfig.exe", "bash.exe", "odbcconf.exe", "scriptrunner.exe", "pcalua.exe", "forfiles.exe",
    "presentationhost.exe", "msdt.exe", "hh.exe", "msiexec.exe", "mmc.exe", "taskmgr.exe", "control.exe",
    "wt.exe", "verclsid.exe", "rasautou.exe", "diskshadow.exe", "wbemtest.exe", "dnscmd.exe",
)

_ALLOW_ID_MARKER = "a11e"   # 'a11e' ~ "allow"; base allow-model rule-id namespace (distinct from tenant '4d54')


def _win_allow(sid: str, name: str, path: str, ident: str) -> ET.Element:
    r = ET.Element("FilePathRule",
                   {"Id": ident, "Name": name, "Description": "", "UserOrGroupSid": sid, "Action": "Allow"})
    c = ET.SubElement(r, "Conditions")
    ET.SubElement(c, "FilePathCondition", {"Path": path})
    return r


def _publisher_allow(sid: str, name: str, publisher: str, ident: str) -> ET.Element:
    r = ET.Element("FilePublisherRule",
                   {"Id": ident, "Name": name, "Description": "Publisher-scoped Allow (signature-pinned)",
                    "UserOrGroupSid": sid, "Action": "Allow"})
    c = ET.SubElement(r, "Conditions")
    pc = ET.SubElement(c, "FilePublisherCondition",
                       {"PublisherName": publisher, "ProductName": "*", "BinaryName": "*"})
    ET.SubElement(pc, "BinaryVersionRange", {"LowSection": "*", "HighSection": "*"})
    return r


def generate_base_policy(enforcement: str = "AuditOnly") -> str:
    """Generate the canonical deny-by-default allow-model base (Exe/Msi/Script), machine-wide + tenant-agnostic.
    ``enforcement`` in {'AuditOnly','Enabled'}. Deterministic rule ids so redeploys are idempotent and the
    committed template can be drift-checked against this generator."""
    if enforcement not in ("AuditOnly", "Enabled"):
        raise AppLockerPolicyError("bad_enforcement_mode")
    root = ET.Element("AppLockerPolicy", {"Version": "1"})
    seq = [0]

    def rid(prefix: str) -> str:
        seq[0] += 1
        return f"{prefix}000000-0000-{_ALLOW_ID_MARKER}-0000-{seq[0]:012x}"

    # ── Exe: deny-by-default; explicit minimal allow-list ──────────────────────────────────────────────────
    exe = ET.SubElement(root, "RuleCollection", {"Type": "Exe", "EnforcementMode": enforcement})
    exe.append(_win_allow(ADMIN_SID, "(Admins) Allow all EXE - operator recovery", "*", rid("b1")))
    for sid, label in SYSTEM_EXEC_SIDS:
        exe.append(_win_allow(sid, f"({label}) Windows EXE", "%WINDIR%\\*", rid("b1")))
        if sid in _SYSTEM_ALSO_PF:
            exe.append(_win_allow(sid, f"({label}) Program Files EXE", "%PROGRAMFILES%\\*", rid("b1")))
    exe.append(_publisher_allow(EVERYONE_SID, "(Everyone) MetaQuotes-signed EXE (MT5)",
                                METAQUOTES_PUBLISHER_NAME, rid("b1")))
    for b in HOSTED_SESSION_ALLOW:
        exe.append(_win_allow(EVERYONE_SID, f"(Hosted session) Allow {b}", f"%SYSTEM32%\\{b}", rid("b1")))

    # ── Msi: admins + system installer cache only ──────────────────────────────────────────────────────────
    msi = ET.SubElement(root, "RuleCollection", {"Type": "Msi", "EnforcementMode": enforcement})
    msi.append(_win_allow(ADMIN_SID, "(Admins) Allow all MSI - operator recovery", "*", rid("b2")))
    msi.append(_win_allow("S-1-5-18", "(System) Windows Installer cache MSI", "%WINDIR%\\Installer\\*", rid("b2")))

    # ── Script: admins + system/service Windows only; hosted tenants get NO script allow (deny-by-default) ──
    scr = ET.SubElement(root, "RuleCollection", {"Type": "Script", "EnforcementMode": enforcement})
    scr.append(_win_allow(ADMIN_SID, "(Admins) Allow all scripts - operator recovery", "*", rid("b3")))
    for sid, label in SYSTEM_EXEC_SIDS:
        if sid in _SYSTEM_ALSO_PF:
            scr.append(_win_allow(sid, f"({label}) Windows scripts", "%WINDIR%\\*", rid("b3")))

    # ── Dll: the load-bearing anti-sideload / anti-COM-hijack collection. POSITIVE, PUBLISHER-based allowlist ─
    # (STREAM 10B review, ADR-0042, hardened by the re-verify HIGH). AppLocker's Exe rules check LAUNCHED IMAGES
    # but NOT a DLL loaded INTO an already-allowed process; without a Dll collection a hosted tenant runs arbitrary
    # NATIVE code via a DLL side-load (stage a signed terminal64.exe + plant a sibling dwmapi.dll) or an HKCU COM
    # InprocServer32 hijack into sihost/taskhostw. A ``%WINDIR%\\*`` PATH allow does NOT close it: %WINDIR%\\Temp,
    # \\Tasks, \\tracing, \\Registration\\CRMLog, System32\\{spool\\drivers\\color, FxsTmp, com\\dmp} and the
    # SysWOW64 equivalents are USER-WRITABLE, so a planted DLL relocated there matches the wildcard. A hosted
    # tenant's token reaches only Everyone, so the tenant DLL surface is PUBLISHER-ONLY: Microsoft-signed OS DLLs
    # + MetaQuotes-signed MT5 DLLs. A planted unsigned/self-signed DLL matches NEITHER and is denied WHEREVER it is
    # planted (accounts tree, %WINDIR%\\Temp, anywhere). Service daemons that load NON-Microsoft DLLs (e.g.
    # python311.dll) run as the service SIDs and get %PROGRAMFILES%\\* (NOT tenant-reachable, NOT tenant-writable).
    # SOAK-VERIFY the Microsoft subject + any non-publisher MT5/service DLL (8003 in AuditOnly) before Enforce.
    dll = ET.SubElement(root, "RuleCollection", {"Type": "Dll", "EnforcementMode": enforcement})
    dll.append(_win_allow(ADMIN_SID, "(Admins) Allow all DLL - operator recovery", "*", rid("b4")))
    dll.append(_publisher_allow(EVERYONE_SID, "(Everyone) Microsoft-signed DLL (OS libraries)",
                                MICROSOFT_WINDOWS_PUBLISHER_NAME, rid("b4")))
    dll.append(_publisher_allow(EVERYONE_SID, "(Everyone) MetaQuotes-signed DLL (MT5)",
                                METAQUOTES_PUBLISHER_NAME, rid("b4")))
    for sid, label in SYSTEM_EXEC_SIDS:
        if sid in _SYSTEM_ALSO_PF:
            dll.append(_win_allow(sid, f"({label}) Program Files DLL - service daemons", "%PROGRAMFILES%\\*",
                                  rid("b4")))
    return _tostr(root)


# The principals a hosted (non-admin) tenant token does NOT carry — Administrators + the system/service/virtual
# accounts. Their (broad, by-design) OS allows are exempt from the tenant-surface check. EVERY OTHER principal in
# an Allow — Everyone, BUILTIN\Users, Authenticated Users, Interactive, a specific user SID, anything — is treated
# as TENANT-REACHABLE and constrained to exactly the two certified forms. This closes the whole class the STREAM
# 10B review found: an alias path (%WINDIR%\System32\*), a broad allow to a tenant-inclusive GROUP other than
# Everyone, an unguarded publisher rule, a widening in the Msi/Script/Dll collection, and a fixed-blocklist bypass.
_SYSTEM_SID_SET = frozenset(s for s, _ in SYSTEM_EXEC_SIDS)
# Non-tenant-writable OS locations. A hosted tenant cannot WRITE under any of these, so allowing execution (system
# principals) or DLL loads (Everyone, Dll collection) from them plants no attacker-controlled code.
_OS_PATH_PREFIXES = ("%WINDIR%", "%SYSTEM32%", "%PROGRAMFILES%", "%PROGRAMFILES(X86)%",
                     "%OSDRIVE%\\WINDOWS", "%OSDRIVE%\\PROGRAM FILES")


def _norm_path(path: str) -> str:
    return (path or "").upper().replace("/", "\\")


def _is_os_path(path: str) -> bool:
    p = _norm_path(path)
    return p == "*" or any(p.startswith(pre) for pre in _OS_PATH_PREFIXES)


def _path_conditions(r: ET.Element):
    return [c.get("Path") or "" for c in r.findall("Conditions/FilePathCondition")]


def _publisher_names(r: ET.Element):
    return [c.get("PublisherName") or "" for c in r.findall("Conditions/FilePublisherCondition")]


def _is_allow(r: ET.Element) -> bool:
    return (r.get("Action") or "").lower() == "allow"     # case-insensitive: Action="allow" must not evade the guard


def _assert_tenant_reachable_allow_ok(coll_type: str, r: ET.Element) -> None:
    """A tenant-reachable Allow must be EXACTLY one certified form, else raise:
      * MetaQuotes publisher rule (any collection) — portable MT5; a signature cannot be forged; OR
      * Microsoft publisher rule (Dll ONLY) — OS DLLs; a signed DLL is trusted code, not an arbitrary-code
        primitive (a Microsoft-signed EXE would be a signed-LOLBIN ACE, so MS publisher is NOT allowed in
        Exe/Msi/Script); OR
      * (Exe) a FilePathRule for exactly ``%SYSTEM32%\\<leaf>`` with leaf in HOSTED_SESSION_ALLOW (a specific,
        non-tenant-writable FILE — never a wildcard, which spans user-writable subdirectories).
    NO tenant-reachable PATH allow is permitted in Dll/Msi/Script — the Dll surface is publisher-only precisely
    because a ``%WINDIR%\\*`` wildcard is bypassable via user-writable %WINDIR% subdirs (re-verify HIGH)."""
    sid = r.get("UserOrGroupSid") or ""
    if r.tag == "FilePublisherRule":
        allowed = {METAQUOTES_PUBLISHER_NAME.upper()}
        if coll_type == "Dll":
            allowed.add(MICROSOFT_WINDOWS_PUBLISHER_NAME.upper())
        pubs = [p.upper() for p in _publisher_names(r)]
        if pubs and all(p in allowed for p in pubs):
            return
        raise AppLockerPolicyError(f"tenant_reachable_publisher_not_allowed:{coll_type}:{sid}:{pubs}")
    if r.tag == "FilePathRule":
        paths = _path_conditions(r)
        if not paths:                                 # a path rule with NO condition would match nothing to bless,
            raise AppLockerPolicyError(f"tenant_reachable_empty_path_rule:{coll_type}:{sid}")   # reject it explicitly
        for path in paths:
            p = _norm_path(path)
            ok = coll_type == "Exe" and any(p == ("%SYSTEM32%\\" + b.upper()) for b in HOSTED_SESSION_ALLOW)
            if not ok:
                raise AppLockerPolicyError(f"tenant_reachable_broad_allow:{coll_type}:{sid}:{path}")
        return
    raise AppLockerPolicyError(f"tenant_reachable_unknown_rule:{coll_type}:{sid}:{r.tag}")


def assert_allow_model_invariants(xml: str) -> bool:
    """Prove the deny-by-default allow model is intact — the PERMANENT STREAM 10B regression guard, rewritten
    (per the STREAM 10B adversarial review + re-verify) as a POSITIVE ALLOWLIST over EVERY rule collection rather
    than a fixed blocklist over the Exe collection only. Raises unless:
      1. the Exe AND Dll collections are present (Dll closes the DLL-sideload / COM-hijack native-code path);
      2. ALL collections share ONE enforcement mode (no silently-NotConfigured collection);
      3. every Allow reachable by a tenant-reachable principal (anything but Admin / system-service-virtual SIDs)
         is exactly one certified form (MetaQuotes publisher; Microsoft publisher for Dll only; or the curated
         %SYSTEM32%\\<leaf> Exe path);
      4. no forbidden interpreter/LOLBIN leaf is granted to such a principal (belt-and-suspenders tripwire);
      5. system/service allows stay confined to non-tenant-writable OS paths / Microsoft|MetaQuotes publishers;
      6. every system/service/virtual-account principal keeps its Exe Windows allow (OS + compositor must run).
    Collections are iterated as a LIST (not keyed by Type) so a DUPLICATE ``RuleCollection Type`` cannot hide a
    widening from analysis."""
    root = ET.fromstring(xml)
    collections = list(root.findall("RuleCollection"))       # LIST — a duplicate Type must not shadow another
    types_present = {c.get("Type") for c in collections}
    for required in ("Exe", "Dll"):
        if required not in types_present:
            raise AppLockerPolicyError(f"missing_rule_collection:{required}")
    modes = {c.get("EnforcementMode") for c in collections}
    if len(modes) != 1:
        raise AppLockerPolicyError(f"mixed_enforcement_modes:{sorted(m or '' for m in modes)}")
    if not (modes <= {"AuditOnly", "Enabled"}):     # a uniform NotConfigured/absent mode disables all enforcement
        raise AppLockerPolicyError(f"non_enforcing_mode:{sorted(m or '' for m in modes)}")

    ms_mq = {MICROSOFT_WINDOWS_PUBLISHER_NAME.upper(), METAQUOTES_PUBLISHER_NAME.upper()}
    lowered_forbidden = {f.lower() for f in FORBIDDEN_HOSTED_ALLOW}
    for coll in collections:
        ctype = coll.get("Type")
        for r in coll:
            if not _is_allow(r):
                continue
            sid = r.get("UserOrGroupSid") or ""
            if sid == ADMIN_SID:
                continue                                     # operator recovery — Allow * permitted
            if sid in _SYSTEM_SID_SET:
                # system/service/virtual principal (NOT tenant-reachable): OS paths only; a publisher rule (if
                # any) must still be Microsoft/MetaQuotes — never a blanket third-party signer.
                if r.tag == "FilePathRule":
                    for path in _path_conditions(r):
                        if not _is_os_path(path):
                            raise AppLockerPolicyError(f"system_sid_non_os_allow:{ctype}:{sid}:{path}")
                elif r.tag == "FilePublisherRule":
                    pubs = [p.upper() for p in _publisher_names(r)]
                    if not (pubs and all(p in ms_mq for p in pubs)):
                        raise AppLockerPolicyError(f"system_sid_unexpected_publisher:{ctype}:{sid}:{pubs}")
                continue
            _assert_tenant_reachable_allow_ok(ctype, r)      # every other principal → certified forms only
            for path in _path_conditions(r):
                leaf = _norm_path(path).rsplit("\\", 1)[-1].lower()
                if leaf in lowered_forbidden:
                    raise AppLockerPolicyError(f"forbidden_interpreter_allow:{ctype}:{path}")

    exe_allowed_sids = set()
    for coll in collections:
        if coll.get("Type") == "Exe":
            exe_allowed_sids.update(r.get("UserOrGroupSid") for r in coll if _is_allow(r))
    missing = [sid for sid, _ in SYSTEM_EXEC_SIDS if sid not in exe_allowed_sids]
    if missing:
        raise AppLockerPolicyError(f"missing_system_exec_allow:{missing}")
    return True


def assert_base_invariants(xml: str) -> bool:
    """Prove the effective/base policy keeps the certified hardened posture: the MetaQuotes publisher Allow and
    the Administrator recovery Allow are present, and there is NO writable-tree executable path-Allow (the
    canonical bypass — a renamed shell dropped into ``C:\\GuvFX\\accounts\\*`` must match no Allow). Raises on
    violation."""
    root = ET.fromstring(xml)
    exe = _exe_collection(root)
    has_publisher = any(r.tag == "FilePublisherRule" and r.get("Action") == "Allow" for r in exe)
    has_admin = any(r.get("UserOrGroupSid") == "S-1-5-32-544" and r.get("Action") == "Allow" for r in exe)
    # The writable-tree bypass (a renamed shell / planted DLL dropped in C:\GuvFX\accounts\*) must match NO Allow
    # path rule in ANY collection — Exe or Dll — regardless of principal.
    bad = []
    for coll in root.findall("RuleCollection"):
        for r in coll:
            if r.tag == "FilePathRule" and r.get("Action") == "Allow":
                for cond in r.findall("Conditions/FilePathCondition"):
                    p = (cond.get("Path") or "").lower().replace("/", "\\")
                    if "guvfx\\accounts" in p:
                        bad.append(r.get("Id"))
    if not has_publisher:
        raise AppLockerPolicyError("missing_metaquotes_publisher_allow")
    if not has_admin:
        raise AppLockerPolicyError("missing_admin_recovery_allow")
    if bad:
        raise AppLockerPolicyError(f"writable_accounts_tree_exe_allow:{sorted(bad)}")
    return True
