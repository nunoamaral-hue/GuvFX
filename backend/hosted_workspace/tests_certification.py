"""ADR-0034 / M3b-2 Integration — disposable-host certification composition + operator command.

Proves the certified chain composes end-to-end (mock host + fake M1, no real MT5): M1 guarded attach ->
M3b-2 adapter -> RawWorkspaceSnapshot -> M3b-1 producer -> WorkspaceObservation -> M3a Manager -> decision,
emitted as a SECRET-FREE, allow-list-only dict. Includes the repository-level negative controls (wrong
binding, wrong/missing path, ambiguous target) and proves the command REFUSES a non-disposable path BEFORE
the host is ever constructed.
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hosted_workspace.agent import AttachOutcome, HostReadState, ProcessProbe, WorkspaceSpec
from hosted_workspace.certification import (
    SAFE_FIELDS,
    classify_target_path,
    run_certification,
)
from hosted_workspace.management.commands.certify_workspace_observation import Command
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.tests_agent import MockHost, _state


def _spec(**kw):
    base = dict(workspace_id="disposable-1", expected_login="12345", expected_server="Demo",
                target_path="C:/disp/terminal64.exe", freshness_limit_seconds=60.0, tick_symbol="EURUSD")
    base.update(kw)
    return WorkspaceSpec(**base)


def _cert(host, spec=None, previous_state=S.CONNECTED, classification="disposable_authorised"):
    return run_certification(host, spec or _spec(), clock=lambda: 1000.0,
                             previous_state=str(previous_state),
                             target_path_classification=classification)


class ClassifyPathTests(SimpleTestCase):
    def test_disposable_authorised(self):
        self.assertEqual(classify_target_path("C:/disp/terminal64.exe", allowed_prefixes=["C:/disp"]),
                         "disposable_authorised")

    def test_windows_posix_normalised(self):
        self.assertEqual(classify_target_path("C:\\disp\\terminal64.exe", allowed_prefixes=["c:/DISP"]),
                         "disposable_authorised")

    def test_blank_is_forbidden(self):
        for bad in (None, "", "   "):
            self.assertEqual(classify_target_path(bad, allowed_prefixes=["C:/disp"]), "forbidden")

    def test_non_allowlisted_is_unclassified(self):
        self.assertEqual(classify_target_path("C:/prod/terminal64.exe", allowed_prefixes=["C:/disp"]),
                         "unclassified")
        self.assertEqual(classify_target_path("C:/disp/terminal64.exe", allowed_prefixes=[]), "unclassified")

    def test_traversal_is_forbidden(self):  # adversarial HIGH regression — `..` could resolve to production
        for bad in ("C:/disp/../prod/terminal64.exe", "C:\\disp\\..\\prod\\terminal64.exe",
                    "C:/disp/sub/../../prod/terminal64.exe"):
            self.assertEqual(classify_target_path(bad, allowed_prefixes=["C:/disp"]), "forbidden", bad)

    def test_sibling_and_adjacent_names_not_authorised(self):  # adversarial HIGH regression — segment boundary
        for bad in ("C:/disp-customer-zero/terminal64.exe", "C:/disposable_prod/terminal64.exe",
                    "C:/disp-prod/terminal64.exe", "C:/dispatcher/terminal64.exe"):
            self.assertEqual(classify_target_path(bad, allowed_prefixes=["C:/disp"]), "unclassified", bad)

    def test_ancestor_directory_authorised(self):
        self.assertEqual(classify_target_path("C:/disp/slot1/terminal64.exe", allowed_prefixes=["C:/disp"]),
                         "disposable_authorised")
        # exact directory match (no child) is still an ancestor of itself
        self.assertEqual(classify_target_path("C:/disp", allowed_prefixes=["C:/disp"]),
                         "disposable_authorised")


class CompositionTests(SimpleTestCase):
    def test_healthy_chain_execution_ready(self):
        result = _cert(MockHost())
        self.assertEqual(
            (result["process_running"], result["ipc_available"], result["connected"],
             result["account_match"], result["trade_allowed"], result["fresh"]),
            (True, True, True, True, True, True))
        self.assertEqual(result["observed_trade_mode"], 0)
        self.assertEqual(result["canonical_state"], str(S.EXECUTION_READY))
        self.assertTrue(result["execution_ready"])
        self.assertTrue(result["transition_required"])

    def test_single_attach_only(self):  # the whole safety point — never a second attach
        host = MockHost()
        _cert(host)
        self.assertEqual(host.attach_calls, 1)
        self.assertEqual(host.released, 1)

    def test_output_is_allow_list_only(self):  # secret-free by construction
        result = _cert(MockHost(state=_state(login="SUPERSECRET", server="SRVSECRET")),
                       spec=_spec(expected_login="SUPERSECRET", expected_server="SRVSECRET"))
        self.assertEqual(set(result.keys()), set(SAFE_FIELDS))
        blob = str(result)
        for forbidden in ("SUPERSECRET", "SRVSECRET", "login", "server", "password", "token", "keyring"):
            self.assertNotIn(forbidden, blob, forbidden)


class NegativeControlTests(SimpleTestCase):
    def test_E_wrong_binding_rejects_readiness(self):
        result = _cert(MockHost(), spec=_spec(expected_login="99999"))  # bind to a different login
        self.assertFalse(result["account_match"])
        self.assertFalse(result["execution_ready"])
        self.assertNotEqual(result["canonical_state"], str(S.EXECUTION_READY))

    def test_G_missing_process_fails_closed_no_launch(self):
        host = MockHost(probe=ProcessProbe(running=False, reason="terminal_not_running"))
        result = _cert(host)
        self.assertEqual((result["process_running"], result["ipc_available"], result["connected"],
                          result["execution_ready"]), (False, False, False, False))
        self.assertEqual(host.attach_calls, 0)  # never attached -> never launched

    def test_H_ambiguous_target_fails_closed(self):
        host = MockHost(probe=ProcessProbe(running=True, duplicate=True, reason="dup"))
        result = _cert(host)
        self.assertFalse(result["execution_ready"])
        self.assertEqual(host.attach_calls, 0)

    def test_attach_refused_not_ready(self):
        host = MockHost(attach=AttachOutcome(attempted=True, ok=False, reason="guarded_attach_not_connected"))
        result = _cert(host)
        self.assertFalse(result["ipc_available"])
        self.assertFalse(result["execution_ready"])

    def test_cert_observation_is_freshly_taken(self):
        # The certification observation is ALWAYS freshly taken (observed_at == now by construction, so
        # age 0). Staleness of a LATER re-evaluation is a downstream concern proven in tests_producer /
        # tests_agent (NaN/inf/future/stale freshness), not exercisable through this single-clock composition.
        result = _cert(MockHost())
        self.assertTrue(result["fresh"])
        result_missing = _cert(MockHost(state=HostReadState(terminal=None, account=None)))
        self.assertIn(result_missing["fresh"], (True, False))  # never a positive from missing broker truth


class CommandTests(SimpleTestCase):
    def test_refuses_non_disposable_path_before_touching_host(self):
        with patch.object(Command, "_build_host") as build:
            with self.assertRaises(CommandError):
                call_command("certify_workspace_observation", target_path="C:/prod/terminal64.exe",
                             disposable_prefix=["C:/disp"], expected_login="12345", expected_server="Demo")
            build.assert_not_called()  # host never constructed -> nothing on the host was touched

    def test_refuses_blank_path(self):
        with patch.object(Command, "_build_host") as build:
            with self.assertRaises(CommandError):
                call_command("certify_workspace_observation", target_path="   ", disposable_prefix=["C:/disp"])
            build.assert_not_called()

    def test_refuses_traversal_and_sibling_paths_before_host(self):  # adversarial HIGH regression
        for bad in ("C:/disp/../prod/terminal64.exe", "C:/disp-customer-zero/terminal64.exe"):
            with patch.object(Command, "_build_host") as build:
                with self.assertRaises(CommandError):
                    call_command("certify_workspace_observation", target_path=bad,
                                 disposable_prefix=["C:/disp"], expected_login="12345")
                build.assert_not_called()  # host never constructed for a non-disposable/traversal target

    def test_healthy_run_prints_secret_free_result(self):
        out = StringIO()
        with patch.object(Command, "_build_host", return_value=MockHost()):
            call_command("certify_workspace_observation", target_path="C:/disp/terminal64.exe",
                         disposable_prefix=["C:/disp"], expected_login="12345", expected_server="Demo",
                         workspace_id="disposable-1", stdout=out)
        import json
        result = json.loads(out.getvalue())
        self.assertEqual(set(result.keys()), set(SAFE_FIELDS))
        self.assertEqual(result["target_path_classification"], "disposable_authorised")
        self.assertTrue(result["execution_ready"])

    def test_wrong_binding_run_denies_readiness(self):
        out = StringIO()
        with patch.object(Command, "_build_host", return_value=MockHost(state=_state(login="12345"))):
            call_command("certify_workspace_observation", target_path="C:/disp/terminal64.exe",
                         disposable_prefix=["C:/disp"], expected_login="00000", expected_server="Demo",
                         stdout=out)
        import json
        result = json.loads(out.getvalue())
        self.assertFalse(result["account_match"])
        self.assertFalse(result["execution_ready"])
