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


def assert_base_invariants(xml: str) -> bool:
    """Prove the effective/base policy keeps the certified hardened posture: the MetaQuotes publisher Allow and
    the Administrator recovery Allow are present, and there is NO writable-tree executable path-Allow (the
    canonical bypass — a renamed shell dropped into ``C:\\GuvFX\\accounts\\*`` must match no Allow). Raises on
    violation."""
    root = ET.fromstring(xml)
    exe = _exe_collection(root)
    has_publisher = any(r.tag == "FilePublisherRule" and r.get("Action") == "Allow" for r in exe)
    has_admin = any(r.get("UserOrGroupSid") == "S-1-5-32-544" and r.get("Action") == "Allow" for r in exe)
    bad = []
    for r in exe:
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
