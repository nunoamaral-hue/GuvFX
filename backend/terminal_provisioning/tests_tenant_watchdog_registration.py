"""P1 (reboot-recovery readiness) — systematic per-tenant bridge watchdog registration guards.

Root cause proven in prod 2026-09-29: 5 of 6 GuvFX_TenantBridgeWatchdog_<id> host tasks were registered against
the OLD node2_bridge_watchdog.ps1, which HARDCODES port 8789 and IGNORES its -Port/-Task args, so they health-checked
account 25's node port and NEVER restarted their own tenant's :88xx bridge -> account 35's :8804 stayed dead for days,
starving deal-ingest and stalling reconciliation. These static guards keep the registration correct for ALL current and
FUTURE tenant bridges, and pin the safety properties of the fixed watchdog + the systematic repair script. Static/text
only (no host); the authoritative parse gate is [Parser]::ParseFile on Windows (RULE 9), mirrored cheaply here.
"""
import os
import re
from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_REGISTRAR = os.path.join(_REPO, "backend", "terminal_provisioning", "windows", "Activate-GuvfxTenantBridge.ps1")
_REPAIR = os.path.join(_REPO, "backend", "terminal_provisioning", "windows", "Repair-GuvfxTenantWatchdogs.ps1")
_FIXED_WD = os.path.join(_REPO, "deploy", "node2-order-bridge", "tenant_bridge_watchdog.ps1")

_KNOWN_SCOPES = {"env", "global", "script", "local", "private", "using", "variable", "workflow"}


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _noncomment_lines(text):
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(ln)
    return out


class TenantWatchdogRegistrationGuards(SimpleTestCase):
    def test_registrar_registers_the_fixed_per_port_watchdog(self):
        text = _read(_REGISTRAR)
        self.assertIn("tenant_bridge_watchdog.ps1", text,
                      "registrar must register the fixed per-port watchdog")
        # The BROKEN node2 watchdog must never appear in an executable (non-comment) line of the registrar.
        offenders = [ln for ln in _noncomment_lines(text) if "node2_bridge_watchdog.ps1" in ln]
        self.assertEqual(offenders, [],
                         "registrar must NOT wire per-tenant watchdogs to node2_bridge_watchdog.ps1 (hardcodes :8789)")

    def test_repair_script_repoints_broken_to_fixed_and_is_scoped(self):
        text = _read(_REPAIR)
        self.assertIn("node2_bridge_watchdog.ps1", text, "repair must detect the broken script")
        self.assertIn("tenant_bridge_watchdog.ps1", text, "repair must repoint to the fixed script")
        # Only ever touches per-tenant watchdog tasks (never the node watchdog / Customer Zero).
        self.assertIn("GuvFX_TenantBridgeWatchdog_", text)
        self.assertIn(r"^GuvFX_TenantBridgeWatchdog_\d+$", text,
                      "repair must match GuvFX_TenantBridgeWatchdog_<id> tasks only")

    def test_fixed_watchdog_wedge_escalation_is_dark_by_default(self):
        text = _read(_FIXED_WD)
        self.assertRegex(text, r"\$WedgeKillThreshold\s*=\s*0",
                         "PID-kill escalation must default OFF (DARK) so behaviour is unchanged unless armed")

    def test_fixed_watchdog_wedge_kill_refuses_reserved_ports_and_nonpython(self):
        text = _read(_FIXED_WD)
        # Defence-in-depth: never kill a process that also owns the CZ (:8788) or node (:8789) port.
        self.assertIn("8788", text)
        self.assertIn("8789", text)
        self.assertIn("python", text, "wedge-kill must verify the target is a python bridge")
        self.assertIn("8800", text)  # per-tenant port range floor guard
        self.assertIn("8899", text)  # per-tenant port range ceiling guard

    def test_ps1_artefacts_are_pure_ascii(self):
        for path in (_REPAIR, _FIXED_WD):
            raw = open(path, "rb").read()
            offenders = sorted({b for b in raw if b > 127})
            self.assertEqual(offenders, [], f"{os.path.basename(path)} has non-ASCII bytes {offenders} (RULE 9)")
            self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", f"{os.path.basename(path)} starts with a UTF-8 BOM")

    def test_ps1_artefacts_have_no_scope_qualifier_parse_trap(self):
        pat = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*):")
        for path in (_REPAIR, _FIXED_WD):
            for i, line in enumerate(_read(path).splitlines(), 1):
                for m in pat.finditer(line):
                    if m.group(1).lower() not in _KNOWN_SCOPES:
                        self.fail(f"{os.path.basename(path)}:{i} scope-qualifier trap '${m.group(1)}:' "
                                  f"(use ${{{m.group(1)}}} before a literal colon)")
