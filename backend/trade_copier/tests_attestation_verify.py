"""ADR-0076 — backend NON-LIFECYCLE attestation verify: gate-off 503, positive, identity/replay negatives,
and the invariant that verification creates NO lifecycle/MasterEvent/ExecutionJob. Synthetic identities only."""
import json
import os
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.models import ExecutionJob
from trade_copier.emitter import attestation as att
from trade_copier.models import CopierPositionLifecycle, MasterEvent
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
ON = dict(COPIER_ENABLED=True, COPIER_ATTESTATION_VERIFY_ENABLED=True, SECURE_SSL_REDIRECT=False)


@override_settings(**ON)
class AttestationVerifyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="TW", server_name="Tradersway-Live",
                                              environment="live")
        self.acct = TradingAccount.objects.create(user=self.user, name="m", broker_name="TradersWay",
                                                   account_number="55442", is_demo=False, is_active=False,
                                                   broker_server=self.bs)

    def _env(self, *, account_id=None, broker_login=None, broker_server="Tradersway-Live", nonce=None, now=None,
             observed_login=None):
        return att.build_attestation(
            secret=SECRET, account_id=(self.acct.id if account_id is None else account_id),
            broker_login=(self.acct.account_number if broker_login is None else broker_login),
            broker_server=broker_server, host_id="host-A", terminal_fp="tfp", bridge_id="127.0.0.1:8806",
            build_sha="fb4ee402", config_digest="cd", nonce=(nonce or ("n" + str(time.time()))),
            now=(now or time.time()), observed_login=observed_login)

    def _post(self, env):
        with mock.patch.dict(os.environ, {"COPIER_INGEST_HMAC_KEY": SECRET}):
            return self.client.post("/api/copy-trading/attest-verify/", data=json.dumps(env),
                                    content_type="application/json")

    def test_gate_off_503(self):
        with override_settings(COPIER_ENABLED=False):
            r = self._post(self._env())
        self.assertEqual(r.status_code, 503)

    def test_positive_verify_creates_no_lifecycle_or_job(self):
        r = self._post(self._env())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "verified")
        self.assertEqual(r.json()["account_id"], self.acct.id)
        self.assertEqual(MasterEvent.objects.count(), 0)
        self.assertEqual(CopierPositionLifecycle.objects.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_wrong_server_fails_closed(self):
        r = self._post(self._env(broker_server="IS6Technologies-Live"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "broker_server_mismatch")

    def test_wrong_login_fails_closed(self):
        r = self._post(self._env(broker_login="99999"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "broker_login_mismatch")

    def test_unknown_account_acknowledged_200_rejected(self):
        r = self._post(self._env(account_id=self.acct.id + 9999))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reason"], "account_not_found")

    def test_replay_409(self):
        env = self._env(nonce="fixed-nonce")
        r1 = self._post(env)
        r2 = self._post(env)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 409)

    def test_wrong_observed_terminal_fails_closed(self):
        # The observed live login (terminal the emitter reached) must be THIS account's login; a wrong terminal
        # is cross-checked against the DB and fails closed (not an inert control).
        r = self._post(self._env(observed_login="99999"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "observed_login_mismatch")

    def test_matching_observed_terminal_accepted(self):
        r = self._post(self._env(observed_login="55442"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "verified")

    def test_bad_signature_401(self):
        env = self._env()
        env["signature"] = "0" * 64
        r = self._post(env)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "bad_signature")
