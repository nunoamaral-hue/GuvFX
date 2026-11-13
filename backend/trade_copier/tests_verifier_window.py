"""ADR-0101 — bounded verifier window tests. Synthetic keys only.

Two layers:
* unit — the window lifecycle + the fail-closed ephemeral key-file source (open/claim/close/reap, single-flight,
  expiry, exhaustion, permissions, ownership, symlink, placeholder, shred-on-close, no-secret status).
* endpoint — the two gated verifiers (ingest / attest) resolve the key ONLY from an open window, prove the
  environment is NOT a key source, enforce ``max_uses``, shred on exhaustion, return to 503 when closed, and
  create NO copier arm/authorization/job/replication side effect.

Test failures must never print a key: the synthetic SECRET is a fixed non-placeholder hex string, and no
assertion message embeds a resolved key.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import verifier_window as vw
from trade_copier.emitter import attestation as att
from trade_copier.models import (CopierLiveArm, CopierLiveAuthorization, CopierReplication,
                                 CopierVerifierWindow, CopierVerifierWindowState, MasterEvent)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"   # 40 hex chars, no placeholder marker
SHORT = "tooshort"                                     # < 32 chars
PLACEHOLDER = "changeme_changeme_changeme_changeme_xx"  # contains a placeholder marker


def _write_key(value: str, *, mode: int = 0o600) -> str:
    fd, path = tempfile.mkstemp(prefix="guvfx_vw_test_", suffix=".key")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    os.chmod(path, mode)
    return path


class VerifierWindowUnitTests(TestCase):
    def tearDown(self):
        # leave no key material behind regardless of what a test created
        vw.close_all(reason="teardown")

    # ── open / single-flight ──
    def test_open_then_status_reports_no_secret(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        w = vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path, max_uses=1)
        status = vw.window_status()
        self.assertTrue(status["open"])
        self.assertEqual(status["purpose"], "INGEST")
        # the status NEVER carries the key value
        self.assertNotIn(SECRET, json.dumps(status))
        self.assertEqual(w.state, CopierVerifierWindowState.OPEN)

    def test_single_flight_rejects_second_open(self):
        p1 = _write_key(SECRET); p2 = _write_key(SECRET)
        self.addCleanup(lambda: [os.path.exists(p) and os.unlink(p) for p in (p1, p2)])
        vw.open_window(purpose="BOTH", ttl_seconds=120, key_path=p1)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.open_window(purpose="BOTH", ttl_seconds=120, key_path=p2)
        self.assertEqual(c.exception.reason, "window_already_open")

    # ── claim / fail-closed source ──
    def test_claim_with_no_window_fails_closed(self):
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "no_verifier_window")

    def test_claim_returns_key_within_window(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        self.assertEqual(vw.claim_key("INGEST"), SECRET)

    def test_purpose_mismatch_fails_closed(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("ATTEST")
        self.assertEqual(c.exception.reason, "verifier_window_purpose")

    def test_expired_window_fails_closed_and_is_reaped(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        w = vw.open_window(purpose="BOTH", ttl_seconds=60, key_path=path)
        CopierVerifierWindow.objects.filter(pk=w.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "verifier_window_expired")
        # reaped on next open: a fresh open succeeds and the stale key file is shredded
        reaped = vw.reap_expired()
        self.assertEqual(reaped, 1)
        self.assertFalse(os.path.exists(path))

    def test_max_uses_cap_exhausts_and_shreds(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path, max_uses=1)
        self.assertEqual(vw.claim_key("INGEST"), SECRET)          # first use consumes the cap
        self.assertFalse(os.path.exists(path))                    # key file shredded on exhaustion
        with self.assertRaises(vw.VerifierWindowError) as c:      # second claim: window closed
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "no_verifier_window")
        self.assertFalse(CopierVerifierWindow.objects.filter(state=CopierVerifierWindowState.OPEN).exists())

    # ── key-file hardening ──
    def test_absent_key_file_fails_closed(self):
        missing = os.path.join(tempfile.gettempdir(), "guvfx_vw_absent_%s.key" % time.time())
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=missing)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_absent")

    def test_loose_permissions_fail_closed(self):
        path = _write_key(SECRET, mode=0o644)   # group/other readable
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_permissions")

    def test_symlink_key_file_fails_closed(self):
        real = _write_key(SECRET)
        link = real + ".link"
        os.symlink(real, link)
        self.addCleanup(lambda: [os.path.exists(p) and os.unlink(p) for p in (real, link)])
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=link)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_symlink")

    def test_wrong_ownership_fails_closed(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        with mock.patch("trade_copier.verifier_window.os.getuid", return_value=os.getuid() + 7):
            with self.assertRaises(vw.VerifierWindowError) as c:
                vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_ownership")

    def test_too_short_key_fails_closed(self):
        path = _write_key(SHORT)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_too_short")

    def test_placeholder_key_fails_closed(self):
        path = _write_key(PLACEHOLDER)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="INGEST", ttl_seconds=120, key_path=path)
        with self.assertRaises(vw.VerifierWindowError) as c:
            vw.claim_key("INGEST")
        self.assertEqual(c.exception.reason, "key_file_placeholder")

    def test_write_key_file_sets_0600_and_rejects_short(self):
        fd, path = tempfile.mkstemp(prefix="guvfx_vw_w_", suffix=".key"); os.close(fd); os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.write_key_file(path, SECRET)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with self.assertRaises(vw.VerifierWindowError):
            vw.write_key_file(path, SHORT)

    # ── cleanup idempotency ──
    def test_close_all_is_idempotent_and_shreds(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="BOTH", ttl_seconds=120, key_path=path)
        self.assertEqual(vw.close_all(), 1)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(vw.close_all(), 0)            # idempotent: nothing left to close
        self.assertFalse(vw.window_status()["open"])

    def test_context_manager_cleans_up_on_exception(self):
        try:
            with vw.verification_window(SECRET, purpose="INGEST"):
                self.assertTrue(vw.window_status()["open"])
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(vw.window_status()["open"])    # window closed despite the exception


@override_settings(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True,
                   COPIER_ATTESTATION_VERIFY_ENABLED=True, SECURE_SSL_REDIRECT=False)
class VerifierWindowEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="TW", server_name="Tradersway-Live",
                                              environment="live")
        self.acct = TradingAccount.objects.create(user=self.user, name="m", broker_name="TradersWay",
                                                   account_number="55442", is_demo=False, is_active=False,
                                                   broker_server=self.bs)

    def tearDown(self):
        vw.close_all(reason="teardown")

    def _attest_env(self, **over):
        return att.build_attestation(
            secret=SECRET, account_id=self.acct.id, broker_login=self.acct.account_number,
            broker_server="Tradersway-Live", host_id="host-A", terminal_fp="tfp", bridge_id="127.0.0.1:8806",
            build_sha="fb4ee402", config_digest="cd", nonce=over.get("nonce", "n" + str(time.time())),
            now=time.time(), observed_login=self.acct.account_number)

    def _post_attest(self, env):
        return self.client.post("/api/copy-trading/attest-verify/", data=json.dumps(env),
                                content_type="application/json")

    def test_no_window_attest_returns_503(self):
        r = self._post_attest(self._attest_env())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "unavailable")

    def test_env_is_not_a_key_source(self):
        # Even with the key present in the process environment, WITHOUT an open window the endpoint fails closed.
        with mock.patch.dict(os.environ, {"COPIER_INGEST_HMAC_KEY": SECRET}):
            r = self._post_attest(self._attest_env())
        self.assertEqual(r.status_code, 503)        # env must NEVER resolve the key

    def test_window_enables_verify_with_no_side_effects(self):
        pre_jobs = ExecutionJob.objects.count()
        with vw.verification_window(SECRET, purpose="ATTEST"):
            r = self._post_attest(self._attest_env())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "verified")
        # no copier control-plane or execution side effect whatsoever
        self.assertEqual(MasterEvent.objects.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), pre_jobs)
        self.assertEqual(CopierLiveArm.objects.count(), 0)
        self.assertEqual(CopierLiveAuthorization.objects.count(), 0)
        self.assertEqual(CopierReplication.objects.count(), 0)

    def test_max_uses_one_shred_after_single_verify(self):
        path = _write_key(SECRET)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        vw.open_window(purpose="ATTEST", ttl_seconds=120, key_path=path, max_uses=1)
        r1 = self._post_attest(self._attest_env())
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(os.path.exists(path))            # shredded on exhaustion
        r2 = self._post_attest(self._attest_env())        # second request: window gone
        self.assertEqual(r2.status_code, 503)

    def test_closed_window_returns_to_503(self):
        with vw.verification_window(SECRET, purpose="ATTEST"):
            self.assertEqual(self._post_attest(self._attest_env()).status_code, 200)
        # context exit closed the window → verifier OFF again
        self.assertEqual(self._post_attest(self._attest_env()).status_code, 503)


class VerifierWindowCommandTests(TestCase):
    def tearDown(self):
        vw.close_all(reason="teardown")

    def test_open_from_stdin_then_status_then_close(self):
        fd, path = tempfile.mkstemp(prefix="guvfx_vw_cmd_", suffix=".key"); os.close(fd); os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(SECRET)):
            call_command("copier_verifier_window", "--open", "--purpose", "ingest",
                         "--key-path", path, "--ttl", "120", "--max-uses", "1", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["action"], "open")
        self.assertTrue(payload["key_file_written"])
        self.assertNotIn(SECRET, out.getvalue())          # the key NEVER appears in command output
        self.assertTrue(os.path.exists(path))             # file written 0600
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

        out2 = io.StringIO()
        call_command("copier_verifier_window", "--close", stdout=out2)
        self.assertFalse(os.path.exists(path))            # close shreds the key file
        self.assertNotIn(SECRET, out2.getvalue())
