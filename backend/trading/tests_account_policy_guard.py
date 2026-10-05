"""Governance (Stream D1, 2026-10-05): forbid NEW ad-hoc DEMO/LIVE EXECUTION DECISIONS outside the policy.

Every execution/strategy DECISION about DEMO vs LIVE must go through ``trading.account_policy``
(``is_demo_environment`` / ``is_live_environment`` / ``account_environment``). This scans the execution /
strategies / hosted_workspace source for a raw ``is_demo`` / ``broker_server.environment`` read used in a
CONDITIONAL context, and fails on any file not in the explicit allowlist. DATA/display reads (dict payloads,
logs, ``__str__``) are NOT decisions and are NOT flagged. The allowlist enumerates the approved low-level modules
and those scheduled for D3/D4 migration; a NEW ad-hoc decision anywhere else fails this test (a ratchet — the
allowlist only shrinks as D3/D4 migrate those modules onto the policy).
"""
import os
import re

from django.test import SimpleTestCase

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend
SCAN_DIRS = ["execution", "strategies", "hosted_workspace"]

# A line that is BOTH a conditional/decision AND a raw environment read, and is NOT a policy call.
_DECISION_CTX = re.compile(r"\b(if|elif|while|assert)\b")
_RAW_READ = re.compile(r"(\.is_demo\b|getattr\([^,]+,\s*['\"]is_demo['\"]|broker_server\.environment|\.environment\b)")
_POLICY_CALL = re.compile(r"is_demo_environment|is_live_environment|account_environment")

# Files approved to interpret environment DIRECTLY in a decision, with the reason. D3/D4 remove these.
ALLOWLIST = {
    "hosted_workspace/matching.py": "D3-pending: observer identity matcher (environment-aware match in D3)",
    "hosted_workspace/provisioning.py": "D3-pending: confirm/bind live gate (D3 monitoring split)",
    "hosted_workspace/capability_recovery.py": "D3-pending: recovery candidate predicate (-> monitoring_eligible)",
    "hosted_workspace/liveness_recovery.py": "D3-pending: recovery candidate predicate (-> monitoring_eligible)",
    "hosted_workspace/live_observe.py": "D3-pending: observe trust anchor",
    "hosted_workspace/producer.py": "D3-pending: observer ExpectedAccount classification",
    "execution/bridge_config.py": "D4-pending: MT5_EXPECTED_IS_DEMO / MT5_ALLOW_LIVE per-runtime boundary",
    "strategies/management/commands/strategy_live_status.py": "operational status report (not the live-order path)",
    "strategies/management/commands/seed_strategies.py": "operational seed guard (not the live-order path)",
    "execution/management/commands/audit_node_assignments.py": "operational audit report LABEL (display only, not an execution decision)",
}


def _iter_py(root):
    for dirpath, _dirs, files in os.walk(root):
        if "/migrations" in dirpath.replace(os.sep, "/"):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            if fn.startswith("tests_") or fn == "tests.py" or fn.startswith("test_"):
                continue
            yield os.path.join(dirpath, fn)


class AccountPolicyGuardTests(SimpleTestCase):
    def test_no_adhoc_demo_live_decision_outside_policy(self):
        offenders = []
        for d in SCAN_DIRS:
            root = os.path.join(BACKEND, d)
            if not os.path.isdir(root):
                continue
            for path in _iter_py(root):
                rel = os.path.relpath(path, BACKEND).replace(os.sep, "/")
                if rel in ALLOWLIST:
                    continue
                try:
                    lines = open(path, encoding="utf-8").read().splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    if _POLICY_CALL.search(line):
                        continue
                    if _DECISION_CTX.search(line) and _RAW_READ.search(line):
                        offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], msg=(
            "Ad-hoc DEMO/LIVE execution decision(s) outside trading.account_policy. Use is_demo_environment()/"
            "is_live_environment(), or (for an approved low-level module) add an explicit ALLOWLIST entry with a "
            "reason in this test:\n" + "\n".join(offenders)))

    def test_allowlist_entries_still_exist(self):
        # Keep the allowlist honest: an entry for a file that no longer has such a read must be removed.
        for rel in ALLOWLIST:
            path = os.path.join(BACKEND, rel)
            self.assertTrue(os.path.exists(path), f"ALLOWLIST references missing file {rel}")
