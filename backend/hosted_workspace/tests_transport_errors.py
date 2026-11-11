"""ADR-0091 — truthful, sanitised signed-transport error taxonomy (de-conflates `host_unavailable`).

Pure, no host, no DB (SimpleTestCase). Covers the classifier directly, the grouping-compat helper, that the
executor's fail-closed path now carries the specific code with NO secrets, and that a consequential op is NEVER
retried on a transport failure.
"""
import http.client
import io
import json
import socket
import urllib.error

from django.test import SimpleTestCase

from hosted_workspace.host_executor import SignedHostExecutor
from hosted_workspace.tests_host_executor import KR, T, _fake_seal
from hosted_workspace.transport_errors import (
    HOST_CONNECTION_DROPPED_PRE_RESPONSE, HOST_HTTP_ERROR, HOST_OBSERVATION_CAPACITY_DEFERRED,
    HOST_TRANSPORT_ERROR, HOST_UNREACHABLE, LEGACY_HOST_UNAVAILABLE, classify_transport_exception,
    is_host_unavailable_reason,
)


class ClassifyTransportExceptionTests(SimpleTestCase):
    def test_connection_refused_is_unreachable(self):
        self.assertEqual(classify_transport_exception(ConnectionRefusedError("refused"))["reason"], HOST_UNREACHABLE)

    def test_timeout_is_unreachable(self):
        self.assertEqual(classify_transport_exception(socket.timeout("timed out"))["reason"], HOST_UNREACHABLE)
        self.assertEqual(classify_transport_exception(TimeoutError("timed out"))["reason"], HOST_UNREACHABLE)

    def test_dns_failure_is_unreachable(self):
        self.assertEqual(classify_transport_exception(socket.gaierror("name resolution"))["reason"], HOST_UNREACHABLE)

    def test_urlerror_wrapping_refused_is_unreachable(self):
        self.assertEqual(
            classify_transport_exception(urllib.error.URLError(ConnectionRefusedError("x")))["reason"],
            HOST_UNREACHABLE)

    def test_remote_disconnected_is_dropped_pre_response(self):
        # The executor daemon's connection-cap shutdown_request looks EXACTLY like this to the client.
        self.assertEqual(
            classify_transport_exception(http.client.RemoteDisconnected("closed without response"))["reason"],
            HOST_CONNECTION_DROPPED_PRE_RESPONSE)

    def test_connection_reset_is_dropped_pre_response(self):
        self.assertEqual(classify_transport_exception(ConnectionResetError("reset"))["reason"],
                         HOST_CONNECTION_DROPPED_PRE_RESPONSE)

    def test_urlerror_wrapping_remote_disconnected_is_dropped_pre_response(self):
        self.assertEqual(
            classify_transport_exception(urllib.error.URLError(http.client.RemoteDisconnected()))["reason"],
            HOST_CONNECTION_DROPPED_PRE_RESPONSE)

    def test_http_error_keeps_only_the_integer_status(self):
        body = io.BytesIO(b'{"agent_token":"SECRET-TOKEN-should-never-surface","detail":"boom"}')
        err = urllib.error.HTTPError("http://host.invalid/hosted/provision", 503, "Service Unavailable", {}, body)
        out = classify_transport_exception(err)
        self.assertEqual(out["reason"], HOST_HTTP_ERROR)
        self.assertEqual(out["http_status"], 503)
        # No body, token, URL or message — only the reason code and the integer status.
        self.assertNotIn("SECRET-TOKEN-should-never-surface", json.dumps(out))
        self.assertEqual(set(out), {"reason", "http_status"})

    def test_unknown_exception_is_generic_transport_error_not_unavailable(self):
        out = classify_transport_exception(ValueError("weird"))
        self.assertEqual(out["reason"], HOST_TRANSPORT_ERROR)
        self.assertNotEqual(out["reason"], LEGACY_HOST_UNAVAILABLE)


class IsHostUnavailableReasonTests(SimpleTestCase):
    def test_groups_every_transport_failure_code_and_legacy(self):
        for r in (HOST_UNREACHABLE, HOST_CONNECTION_DROPPED_PRE_RESPONSE, HOST_HTTP_ERROR,
                  HOST_TRANSPORT_ERROR, LEGACY_HOST_UNAVAILABLE):
            self.assertTrue(is_host_unavailable_reason(r), r)

    def test_is_false_for_non_transport_reasons(self):
        for r in ("no_observer_task", "terminal_not_running", HOST_OBSERVATION_CAPACITY_DEFERRED, "", None):
            self.assertFalse(is_host_unavailable_reason(r), r)


def _ex(transport, *, account_id=14):
    return SignedHostExecutor(account_id=account_id, rdp_host="x", transport=transport, keyring=KR, key_id="k1",
                              base_url="https://host.invalid", seal_password=_fake_seal, reserved_ids="",
                              clock=lambda: T)


class ExecutorFailClosedClassifiedTests(SimpleTestCase):
    def test_send_carries_specific_code_and_no_secrets(self):
        def drop(base_url, request):
            raise http.client.RemoteDisconnected("closed without response")
        res = _ex(drop).verify_slot()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], HOST_CONNECTION_DROPPED_PRE_RESPONSE)
        # The fail-closed result exposes ONLY ok/reason (+ integer http_status) — never a body, token or signature.
        self.assertLessEqual(set(res), {"ok", "reason", "http_status"})

    def test_send_http_error_surfaces_status_only(self):
        def http_err(base_url, request):
            body = io.BytesIO(b'{"signature":"SECRET-SIG"}')
            raise urllib.error.HTTPError("http://host.invalid", 500, "err", {}, body)
        res = _ex(http_err).verify_slot()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], HOST_HTTP_ERROR)
        self.assertEqual(res["http_status"], 500)
        self.assertNotIn("SECRET-SIG", json.dumps(res))

    def test_consequential_op_is_single_attempt_no_retry(self):
        # A transport failure on a CONSEQUENTIAL op (teardown) must NOT be retried — exactly one POST, fail closed.
        calls = {"n": 0}
        def boom(base_url, request):
            calls["n"] += 1
            raise ConnectionResetError("reset mid-flight")
        res = _ex(boom).decommission_runtime(username="guvfx_u_14", runtime_root=r"C:\GuvFX\accounts\14")
        self.assertEqual(calls["n"], 1)                       # single attempt — no hidden retry loop
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], HOST_CONNECTION_DROPPED_PRE_RESPONSE)
