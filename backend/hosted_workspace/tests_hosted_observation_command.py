"""Beta Readiness Stream 2 — G15: the hosted-observation scheduler command.

Proves two-level darkness (dormant unless HOSTED_OBSERVATION_SCHEDULER_ENABLED; drivers self-gate on the
master flag), the singleton (no-overlap) guard, the fail-closed observe_fn (yields None → nothing ingested),
and that one cycle runs both provisioning (G2) and observation. Nothing here arms execution.
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from hosted_workspace import provisioning as P
from hosted_workspace.management.commands import run_hosted_observations as CMD
from hosted_workspace.tests_provisioning import _FLAGS_ON, _node, _user

_SCHED_ON = dict(HOSTED_OBSERVATION_SCHEDULER_ENABLED="1")


def _requested(login="700900"):
    res = P.request_hosted_workspace(_user(login), expected_login=login)
    assert res.ok, res.reason
    return res.workspace


class SchedulerCommandTests(TestCase):
    def test_dormant_when_scheduler_flag_off(self):
        out = StringIO()
        call_command("run_hosted_observations", stdout=out)
        self.assertIn("dormant", out.getvalue())

    @override_settings(**_SCHED_ON, **_FLAGS_ON)
    def test_runs_provisioning_and_observation_cycle(self):
        _node(max_accounts=5)
        ws = _requested()
        out = StringIO()
        call_command("run_hosted_observations", stdout=out)
        s = out.getvalue()
        self.assertIn("prov:", s)
        self.assertIn("obs:", s)
        ws.refresh_from_db()
        self.assertIsNotNone(ws.execution_node_id)        # G2 ran inside the scheduler cycle

    @override_settings(**_SCHED_ON)
    def test_singleton_lock_skips_when_held(self):
        out = StringIO()
        with mock.patch.object(CMD, "try_acquire_singleton", return_value=False):
            call_command("run_hosted_observations", stdout=out)
        self.assertIn("skipped", out.getvalue())

    @override_settings(**_FLAGS_ON)
    def test_force_runs_without_scheduler_flag(self):
        out = StringIO()
        call_command("run_hosted_observations", "--force", stdout=out)
        self.assertIn("prov:", out.getvalue())

    @override_settings(**_FLAGS_ON)
    def test_observe_fn_is_fail_closed_none_ingests_nothing(self):
        self.assertIsNone(CMD.resolve_observe_fn()(object()))     # DARK placeholder yields None
        _node(max_accounts=5)
        _requested()
        result = CMD.run_cycle()                                  # lock-free core
        self.assertEqual(result["provisioning"]["allocated"], 1)
        self.assertEqual(result["observation"]["applied"], 0)     # observe_fn=None → nothing ingested
        self.assertGreaterEqual(result["observation"]["unavailable"], 1)

    def test_singleton_helpers_are_safe_off_postgres(self):
        # A non-postgres backend has no cross-connection lock: acquire returns True, release is a no-op.
        with mock.patch.object(CMD.connection, "vendor", "sqlite"):
            self.assertTrue(CMD.try_acquire_singleton())
            CMD.release_singleton()                               # must not raise

    @override_settings(**_SCHED_ON, **_FLAGS_ON, HOSTED_BOUNDED_OBSERVATION_ENABLED="1")
    def test_bounded_telemetry_is_logged_when_armed(self):
        # §8/§9: the bounded worker count, typed unavailable reasons, and recovery onboarding-skip/relaunch
        # counts must be OBSERVABLE in the ops summary line. Patch the cycle (no host) and assert they render.
        canned = {"enabled": True, "polled": 3, "applied": 1, "unavailable": 2, "errors": 0, "workers": 8,
                  "reasons": {"ok": 1, "observation_timeout": 2},
                  "delivery": {"connected": 0, "disconnected": 0, "held": 3, "cz_skipped": 0}}
        with mock.patch("hosted_workspace.bounded_observation.run_bounded_observation_cycle",
                        return_value=canned):
            out = StringIO()
            call_command("run_hosted_observations", stdout=out)
        s = out.getvalue()
        self.assertIn("bounded: workers=8", s)
        self.assertIn("observation_timeout", s)               # typed reason, not flattened
        self.assertIn("recovery:", s)
        self.assertIn("skipped_onboarding=", s)               # §9 onboarding-gate counter is visible

    @override_settings(**_SCHED_ON, **_FLAGS_ON)
    def test_legacy_summary_line_has_no_bounded_or_recovery_section(self):
        # Flag OFF ⇒ legacy path ⇒ the summary line is byte-identical to before this stream (no new sections).
        _node(max_accounts=5)
        _requested()
        out = StringIO()
        call_command("run_hosted_observations", stdout=out)
        s = out.getvalue()
        self.assertIn("obs:", s)
        self.assertNotIn("bounded:", s)
        self.assertNotIn("recovery:", s)
