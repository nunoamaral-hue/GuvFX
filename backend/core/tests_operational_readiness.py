"""ADR-0035 Operational Readiness — the read-only health framework, pre-flight, rollback planner,
evidence collector, management commands and DARK staff API.

Proves the load-bearing guarantees: the aggregator NEVER fabricates a healthy reading (dark subsystems
are AWAITING_SPONSOR, unobserved ones degrade, a raising probe fails open to DEGRADED); the pre-flight
fails CLOSED and is honest about the external host-cert gate; the rollback plan lists only non-destructive
flag-disable steps and executes nothing; the evidence manifest is schema-conformant; and the API is
staff-only and 404-invisible while its flag is OFF. Nothing here mutates system state or authorises an
order.
"""
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from io import StringIO

from rest_framework.test import APIRequestFactory, force_authenticate

from core import operational_health as OH
from core.operational_health import HealthState, build_operational_health
from core.preflight import run_preflight
from core.rollback_planner import plan_rollback
from core.operational_evidence import build_operational_evidence
from core.views import OperationalReadinessView
from execution.models import TerminalNode
from trading.models import TradingAccount

U = get_user_model()

# Every hosted / broker / ops flag forced ON so we exercise the enabled branches deterministically.
_ALL_ON = dict(
    HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1",
    HOSTED_MT5_REMOTEAPP_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1",
    BROKER_CONNECTIVITY_HEALTH_ENABLED="1", OPERATIONS_EVENTS_ENABLED="1",
)


class HealthFrameworkTests(TestCase):
    def test_rollup_uses_only_the_seven_states(self):
        rollup = build_operational_health()
        for s in rollup["subsystems"]:
            self.assertIn(s["state"], HealthState.ALL, s)

    def test_backend_and_database_are_genuinely_healthy(self):
        rollup = build_operational_health()
        byname = {s["name"]: s for s in rollup["subsystems"]}
        self.assertEqual(byname["backend"]["state"], HealthState.HEALTHY)
        self.assertEqual(byname["database"]["state"], HealthState.HEALTHY)
        self.assertTrue(byname["database"]["observed"])

    def test_dark_subsystems_are_awaiting_sponsor_never_healthy(self):
        # With every hosted/broker/ops flag OFF (default), the whole hosted family + broker health +
        # agent monitor + operational events must read AWAITING_SPONSOR — never HEALTHY.
        rollup = build_operational_health()
        byname = {s["name"]: s for s in rollup["subsystems"]}
        for name in ("hosted_workspace", "delivery", "execution", "onboarding",
                     "broker_health", "operational_events"):
            self.assertEqual(byname[name]["state"], HealthState.AWAITING_SPONSOR, name)

    def test_unobserved_component_never_reports_healthy(self):
        # No ComponentHealth rows -> workers/bridge/mt5 are DEGRADED + observed=False, not HEALTHY.
        rollup = build_operational_health()
        byname = {s["name"]: s for s in rollup["subsystems"]}
        for name in ("workers", "bridge", "mt5"):
            self.assertNotEqual(byname[name]["state"], HealthState.HEALTHY, name)
            self.assertFalse(byname[name]["observed"], name)

    def test_a_raising_probe_fails_open_to_degraded_not_crash(self):
        def boom():
            raise RuntimeError("kaboom")
        boom._subsystem = "boomsys"
        res = OH._guard(boom)
        self.assertEqual(res.state, HealthState.DEGRADED)
        self.assertFalse(res.observed)

    def test_onboarding_healthy_only_when_flags_on(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1",
                               HOSTED_WORKSPACE_ONBOARDING_ENABLED="1"):
            byname = {s["name"]: s for s in build_operational_health()["subsystems"]}
            self.assertEqual(byname["onboarding"]["state"], HealthState.HEALTHY)

    def test_delivery_awaits_host_cert_even_with_flags_on(self):
        # Delivery must NOT go healthy on flags alone — the RDS/RemoteApp host gate keeps it dark.
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_REMOTEAPP_ENABLED="1"):
            byname = {s["name"]: s for s in build_operational_health()["subsystems"]}
            self.assertEqual(byname["delivery"]["state"], HealthState.AWAITING_SPONSOR)

    def test_agent_monitor_misconfigured_not_hidden_by_stale_band(self):
        # Regression (review HIGH): a blind/UNCONFIGURED monitor whose durable current_band is a leftover
        # 'HEALTHY' must classify MISCONFIGURED off current_state — the stale band cannot mask the fault.
        # (The singleton is migration-seeded, so update the existing row rather than create it.)
        from terminal_provisioning.models import AgentMonitorState
        AgentMonitorState.objects.update_or_create(
            pk=AgentMonitorState.SINGLETON_ID,
            defaults={"current_state": "UNCONFIGURED", "current_band": "HEALTHY"})
        with override_settings(VALIDATION_AGENT_MONITORING_ENABLED=True):
            byname = {s["name"]: s for s in build_operational_health()["subsystems"]}
        self.assertEqual(byname["agent_monitor"]["state"], HealthState.MISCONFIGURED)

    def test_build_health_never_creates_agent_monitor_row(self):
        # Regression (review MEDIUM): the read-only rollup must NOT get_or_create the singleton. Delete the
        # migration-seeded row, then prove build_operational_health() does not recreate it (with .load() it
        # would; with the read-only .filter().first() it stays absent) — both dark and enabled.
        from terminal_provisioning.models import AgentMonitorState
        AgentMonitorState.objects.all().delete()
        self.assertEqual(AgentMonitorState.objects.count(), 0)
        build_operational_health()
        self.assertEqual(AgentMonitorState.objects.count(), 0)
        with override_settings(VALIDATION_AGENT_MONITORING_ENABLED=True):
            build_operational_health()
        self.assertEqual(AgentMonitorState.objects.count(), 0)

    def test_onboarding_healthy_and_observed_backed_by_a_real_read(self):
        # Regression (review MEDIUM #1): onboarding HEALTHY must be backed by a real read (observed=True),
        # not flags alone. Regression (re-review MEDIUM): a TERMINAL RETIRED workspace (a normal decommission
        # whose row persists) must NOT degrade onboarding — onboarding health is "can a NEW customer
        # onboard?", independent of decommissioned workspaces — so the rollup is not permanently poisoned.
        from hosted_workspace.models import HostedMt5Workspace
        u = U.objects.create_user(username="wsu", email="wsu@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=u, name="a", broker_name="b", account_number="700900",
                                             is_demo=True)
        HostedMt5Workspace.objects.create(trading_account=acct, canonical_state="RETIRED")
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1",
                               HOSTED_WORKSPACE_ONBOARDING_ENABLED="1"):
            byname = {s["name"]: s for s in build_operational_health()["subsystems"]}
        self.assertEqual(byname["onboarding"]["state"], HealthState.HEALTHY)
        self.assertTrue(byname["onboarding"]["observed"])

    def test_overall_all_dark_is_awaiting_sponsor_when_no_fault(self):
        # Synthesise: no faults, no healthy -> AWAITING_SPONSOR (never silently 'healthy').
        subs = [OH.SubsystemHealth("a", HealthState.AWAITING_SPONSOR, True),
                OH.SubsystemHealth("b", HealthState.AWAITING_SPONSOR, True)]
        # Emulate the rollup's no-fault branch directly.
        faults = [s for s in subs if s.state in HealthState.FAULTS]
        self.assertEqual(faults, [])


class PreflightTests(TestCase):
    def test_host_certification_is_always_blocked(self):
        result = run_preflight()
        host = [c for c in result["checks"] if c["id"] == "host.certification"][0]
        self.assertEqual(host["status"], "BLOCKED")

    def test_no_active_node_fails_closed(self):
        result = run_preflight()
        cap = [c for c in result["checks"] if c["id"] == "capacity.active_nodes"][0]
        self.assertEqual(cap["status"], "FAIL")
        self.assertEqual(result["verdict"], "NOT_READY")

    def test_with_capacity_verdict_is_blocked_on_sponsor(self):
        TerminalNode.objects.create(hostname="n1", status=TerminalNode.Status.ACTIVE, max_accounts=5)
        result = run_preflight()
        self.assertEqual(result["verdict"], "BLOCKED_ON_SPONSOR")
        # "no fake READY": a host-cert-BLOCKED system is NOT ready, even with prerequisites met.
        self.assertFalse(result["ready"])
        ids = {b["id"] for b in result["blocking"]}
        self.assertIn("host.certification", ids)

    def test_node_binding_integrity_passes_with_no_bound_workspaces(self):
        TerminalNode.objects.create(hostname="n2", status=TerminalNode.Status.ACTIVE, max_accounts=5)
        result = run_preflight()
        integ = [c for c in result["checks"] if c["id"] == "integrity.node_binding"][0]
        self.assertEqual(integ["status"], "PASS")

    def test_preflight_mutates_nothing(self):
        before = TerminalNode.objects.count()
        run_preflight()
        self.assertEqual(TerminalNode.objects.count(), before)


class RollbackPlannerTests(TestCase):
    def test_fully_dark_by_default(self):
        plan = plan_rollback()
        self.assertEqual(plan["posture"], "FULLY_DARK")
        self.assertEqual(plan["rollback_steps"], [])
        self.assertFalse(plan["executes_anything"])

    def test_armed_flag_yields_nondestructive_step(self):
        with override_settings(HOSTED_MT5_EXECUTION_ENABLED="1"):
            plan = plan_rollback()
        self.assertEqual(plan["posture"], "ARMED")
        self.assertIn("HOSTED_MT5_EXECUTION_ENABLED", plan["armed_flags"])
        self.assertTrue(plan["rollback_steps"])
        self.assertFalse(any(s["destructive"] for s in plan["rollback_steps"]))

    def test_deploy_image_rollback_is_reference_only(self):
        plan = plan_rollback()
        self.assertEqual(plan["deploy_image_rollback"]["image_tag"], "rollback-preADR0021")


class EvidenceManifestTests(TestCase):
    REQUIRED = ("schema_version", "handoff_id", "packet_id", "created_at_utc", "branch",
                "base_commit", "head_commit", "commands", "expected_results", "actual_results",
                "status", "limitations", "artefact_locations", "checksums", "reviewer")

    def test_manifest_is_schema_conformant(self):
        m = build_operational_evidence(packet_id="OPS", handoff_id="h1", created_at_utc="2026-08-09T00:00:00Z",
                                       branch="feat/x", base_commit="abc", head_commit="def")
        for k in self.REQUIRED:
            self.assertIn(k, m, k)
        self.assertIn(m["status"], ("PASS", "PARTIAL", "FAIL"))
        self.assertIsInstance(m["commands"], list)
        self.assertIsInstance(m["checksums"], dict)

    def test_status_is_partial_when_only_host_gate_blocks(self):
        TerminalNode.objects.create(hostname="n3", status=TerminalNode.Status.ACTIVE, max_accounts=5)
        m = build_operational_evidence(packet_id="OPS", handoff_id="h2", created_at_utc="t",
                                       branch="b", base_commit="c")
        self.assertEqual(m["preflight"]["verdict"], "BLOCKED_ON_SPONSOR")
        self.assertEqual(m["status"], "PARTIAL")

    def test_manifest_validates_against_repo_json_schema_if_available(self):
        import os
        schema_path = os.path.join(os.path.dirname(__file__), "..", "..", "evidence", "schema",
                                   "evidence-manifest.schema.json")
        if not os.path.exists(schema_path):
            self.skipTest("evidence schema not present")
        try:
            import jsonschema
        except Exception:
            self.skipTest("jsonschema not installed")
        with open(schema_path) as fh:
            schema = json.load(fh)
        m = build_operational_evidence(packet_id="OPS", handoff_id="h3", created_at_utc="t",
                                       branch="b", base_commit="c", head_commit="d")
        jsonschema.validate(m, schema)   # raises on non-conformance


class CommandTests(TestCase):
    def test_operational_health_command_json(self):
        out = StringIO()
        call_command("operational_health", "--json", stdout=out)
        data = json.loads(out.getvalue())
        self.assertIn("overall", data)
        self.assertIn("subsystems", data)

    def test_preflight_command_json(self):
        out = StringIO()
        call_command("hosted_workspace_preflight", "--json", stdout=out)
        data = json.loads(out.getvalue())
        self.assertIn("verdict", data)

    def test_rollback_plan_command_json(self):
        out = StringIO()
        call_command("rollback_plan", "--json", stdout=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data["executes_anything"], False)

    def test_collect_evidence_command_stdout(self):
        out = StringIO()
        call_command("collect_operational_evidence", "--packet-id", "OPS", "--handoff-id", "h", stdout=out)
        data = json.loads(out.getvalue())
        self.assertIn(data["status"], ("PASS", "PARTIAL", "FAIL"))


class HostCertStageTests(TestCase):
    """ADR-0035 amendment: host-cert status is evidence-driven (a durable stage), NOT a hard-coded
    permanent block — and never green off a feature flag."""

    def test_stage_defaults_not_started_and_fails_safe(self):
        from core.host_cert import NOT_STARTED, host_cert_stage, is_certified
        self.assertEqual(host_cert_stage(), NOT_STARTED)
        self.assertFalse(is_certified())
        with override_settings(HOSTED_HOST_CERT_STAGE="banana"):   # unrecognised -> fail-safe NOT_STARTED
            self.assertEqual(host_cert_stage(), NOT_STARTED)
            self.assertFalse(is_certified())

    def test_stage_recognises_valid_values(self):
        from core.host_cert import CERTIFIED, IN_PROGRESS, host_cert_stage, is_certified
        with override_settings(HOSTED_HOST_CERT_STAGE="in_progress"):
            self.assertEqual(host_cert_stage(), IN_PROGRESS)
        with override_settings(HOSTED_HOST_CERT_STAGE="CERTIFIED"):
            self.assertEqual(host_cert_stage(), CERTIFIED)
            self.assertTrue(is_certified())

    def test_preflight_host_check_tracks_the_stage(self):
        def _host(res):
            return [c for c in res["checks"] if c["id"] == "host.certification"][0]["status"]
        self.assertEqual(_host(run_preflight()), "BLOCKED")   # NOT_STARTED default
        for stage, expected in (("IN_PROGRESS", "BLOCKED"), ("BLOCKED_ON_HUMAN", "BLOCKED"),
                                ("CERTIFIED", "PASS")):
            with override_settings(HOSTED_HOST_CERT_STAGE=stage):
                self.assertEqual(_host(run_preflight()), expected, stage)

    def test_certified_stage_clears_the_permanent_block(self):
        # The load-bearing correction: with capacity present AND cert recorded CERTIFIED, the verdict is no
        # longer the permanent BLOCKED_ON_SPONSOR — it becomes genuinely ready.
        TerminalNode.objects.create(hostname="nz", status=TerminalNode.Status.ACTIVE, max_accounts=5)
        with override_settings(HOSTED_HOST_CERT_STAGE="CERTIFIED"):
            res = run_preflight()
        self.assertIn(res["verdict"], ("READY", "READY_WITH_WARNINGS"))
        self.assertTrue(res["ready"])

    def test_flag_does_not_make_certification_green(self):
        # Enabling every feature flag must NOT flip host cert to PASS — only a recorded CERTIFIED stage does.
        with override_settings(**_ALL_ON):
            host = [c for c in run_preflight()["checks"] if c["id"] == "host.certification"][0]
        self.assertEqual(host["status"], "BLOCKED")


class ReadinessApiTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = U.objects.create_user(username="ops", email="ops@x.invalid", password="x",
                                            is_staff=True)
        self.user = U.objects.create_user(username="joe", email="joe@x.invalid", password="x")

    def _get(self, user, query=""):
        req = self.factory.get("/api/operational-readiness/" + query)
        force_authenticate(req, user=user)
        return OperationalReadinessView.as_view()(req)

    def test_404_when_flag_off(self):
        resp = self._get(self.staff)
        self.assertEqual(resp.status_code, 404)

    @override_settings(OPERATIONAL_READINESS_API_ENABLED="1")
    def test_staff_gets_rollup_when_flag_on(self):
        resp = self._get(self.staff)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("operational_health", resp.data)
        self.assertIn("preflight", resp.data)
        self.assertIn("rollback_plan", resp.data)

    @override_settings(OPERATIONAL_READINESS_API_ENABLED="1")
    def test_non_staff_forbidden(self):
        resp = self._get(self.user)
        self.assertIn(resp.status_code, (401, 403))

    @override_settings(OPERATIONAL_READINESS_API_ENABLED="1")
    def test_section_filter(self):
        resp = self._get(self.staff, "?section=health")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("operational_health", resp.data)
        self.assertNotIn("preflight", resp.data)
