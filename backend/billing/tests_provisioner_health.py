"""ADR-0021 PR A — provisioning-health predicate tests (Correction 1).

Covers the FAIL-CLOSED durable heartbeat (missing/stale/DEGRADED/ERROR ⇒ unhealthy; a fresh PROCESSING
busy worker ⇒ healthy) and the bounded, fail-closed host-agent liveness probe (which responses prove
liveness, and that a 401 reached WITHOUT credentials counts as reachable).
"""
import urllib.error
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from billing import beta
from terminal_provisioning.models import ProvisionerHeartbeat


def _age(seconds: int) -> None:
    """Backdate the singleton heartbeat by ``seconds`` (bypasses auto_now via queryset update)."""
    ProvisionerHeartbeat.objects.filter(pk=ProvisionerHeartbeat.SINGLETON_ID).update(
        updated_at=timezone.now() - timedelta(seconds=seconds))


@override_settings(BETA_RUNTIMES_ENABLED=True, BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS=120)
class HeartbeatHealthTests(TestCase):
    def test_missing_heartbeat_is_unhealthy(self):
        self.assertFalse(ProvisionerHeartbeat.objects.exists())
        self.assertFalse(beta._provisioner_heartbeat_fresh())          # fail closed
        self.assertFalse(beta.provisioning_service_healthy())

    def test_fresh_idle_ready_is_healthy(self):
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.IDLE_READY)
        self.assertTrue(beta._provisioner_heartbeat_fresh())
        self.assertTrue(beta.provisioning_service_healthy())

    def test_fresh_processing_busy_worker_is_healthy(self):
        # KEY: a worker mid-job (PROCESSING) with a fresh heartbeat is HEALTHY — busy ≠ unhealthy.
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.PROCESSING, last_job_id=42)
        self.assertTrue(beta._provisioner_heartbeat_fresh())
        self.assertTrue(beta.provisioning_service_healthy())

    def test_stale_heartbeat_is_unhealthy(self):
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.PROCESSING)
        _age(121)                                                       # just past the 120s TTL
        self.assertFalse(beta._provisioner_heartbeat_fresh())
        self.assertFalse(beta.provisioning_service_healthy())

    def test_fresh_degraded_is_unhealthy(self):
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.DEGRADED)
        self.assertFalse(beta._provisioner_heartbeat_fresh())          # alive but not healthy
        self.assertFalse(beta.provisioning_service_healthy())

    def test_fresh_error_is_unhealthy(self):
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.ERROR)
        self.assertFalse(beta._provisioner_heartbeat_fresh())
        self.assertFalse(beta.provisioning_service_healthy())

    def test_kill_switch_off_is_unhealthy_even_if_fresh(self):
        ProvisionerHeartbeat.touch("w1", ProvisionerHeartbeat.Status.IDLE_READY)
        with override_settings(BETA_RUNTIMES_ENABLED=False):
            self.assertFalse(beta.provisioning_service_healthy())      # kill switch dominates


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code):
    return urllib.error.HTTPError("http://agent/health", code, "err", hdrs=None, fp=None)


@override_settings(HOST_AGENT_REACHABLE="", GUVFX_WINDOWS_AGENT_BASE_URL="http://agent.invalid",
                   HOST_AGENT_PROBE_CACHE_SECONDS=0, HOST_AGENT_PROBE_TIMEOUT_SECONDS=1)
class HostAgentProbeTests(TestCase):
    def setUp(self):
        beta._HOST_AGENT_PROBE_CACHE["at"] = None   # never carry a probe result across tests
        beta._HOST_AGENT_PROBE_CACHE["ok"] = None

    # -- explicit override wins (deterministic ops/tests break-glass) --
    def test_override_true(self):
        with override_settings(HOST_AGENT_REACHABLE="1"):
            self.assertTrue(beta.host_agent_reachable())

    def test_override_false(self):
        with override_settings(HOST_AGENT_REACHABLE="0"):
            self.assertFalse(beta.host_agent_reachable())

    def test_no_base_config_fails_closed(self):
        with override_settings(GUVFX_WINDOWS_AGENT_BASE_URL=""):
            self.assertFalse(beta.host_agent_reachable())

    # -- which responses prove liveness --
    def test_200_is_reachable(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)):
            self.assertTrue(beta.host_agent_reachable())

    def test_401_without_credentials_is_reachable(self):
        # DOCUMENTED: the probe sends NO credentials; a 401 proves the agent is up and answering.
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
            self.assertTrue(beta.host_agent_reachable())

    def test_403_is_reachable(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(403)):
            self.assertTrue(beta.host_agent_reachable())

    def test_500_fails_closed(self):
        # An unexpected server error is NOT proof of a healthy agent — fail closed.
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            self.assertFalse(beta.host_agent_reachable())

    def test_connection_error_fails_closed(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(beta.host_agent_reachable())

    def test_timeout_fails_closed(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            self.assertFalse(beta.host_agent_reachable())

    def test_bounded_cache_avoids_probe_storm(self):
        with override_settings(HOST_AGENT_PROBE_CACHE_SECONDS=30):
            with mock.patch("urllib.request.urlopen", return_value=_FakeResp(200)) as m:
                self.assertTrue(beta.host_agent_reachable())
                self.assertTrue(beta.host_agent_reachable())
                self.assertEqual(m.call_count, 1)   # second call served from the bounded cache
