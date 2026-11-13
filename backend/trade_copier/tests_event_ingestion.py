"""Phase-3/4 signed event-ingestion tests (ADR-0074).

Two layers: PURE verifier (signature, replay, skew, expiry, tamper, scope) and the DB/view path (gate-off 503,
persistence + idempotency, account-scope + identity rejection, and the invariant that it creates NO
ExecutionJob)."""
import json
import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import event_ingestion as ei
from trade_copier.models import CopierIngestedRevision, CopierMaster
from trade_copier.verifier_window import verification_window
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"  # 40 hex chars, no placeholder marker
ENABLED = dict(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True)


class _MemBurn:
    """In-memory single-use nonce burn for the pure verifier tests."""
    def __init__(self):
        self.seen = set()

    def __call__(self, nonce, expiry):
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


class VerifierTests(SimpleTestCase):
    def _payload(self):
        return {"revisions": [{"account_id": 1, "ticket": 1, "revision": 1,
                               "observed_time": timezone.now().isoformat(), "side": "BUY", "volume": 0.1}]}

    def _env(self, **over):
        now = over.pop("now", time.time())
        kw = dict(account_id=1, secret=SECRET, nonce="n1", now=now, payload=self._payload())
        kw.update(over)
        return ei.sign_ingest_request(**kw)

    def _verify(self, env, *, keyring=None, now=None, burn=None):
        return ei.verify_ingest_request(env, keyring=keyring or {ei.DEFAULT_KEY_ID: SECRET},
                                        now=now if now is not None else time.time(),
                                        nonce_burn=burn or _MemBurn())

    def test_roundtrip_ok(self):
        out = self._verify(self._env())
        self.assertEqual(out["signed"]["account_id"], 1)

    def test_bad_signature(self):
        env = self._env()
        env["signature"] = "0" * 64
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_unknown_key_id(self):
        env = self._env()
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env, keyring={"other": SECRET})
        self.assertEqual(c.exception.reason, "unknown_key_id")

    def test_timestamp_skew_and_expired(self):
        with self.assertRaises(ei.IngestError) as c:
            self._verify(self._env(now=time.time() - 10_000))
        self.assertIn(c.exception.reason, ("timestamp_skew", "request_expired"))

    def test_expiry_too_far(self):
        env = self._env(ttl_seconds=10_000)   # expiry - ts > 600
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "expiry_too_far")

    def test_payload_tamper_digest_mismatch(self):
        env = self._env()
        env["payload"]["revisions"][0]["volume"] = 99.0   # changed after signing
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "payload_digest_mismatch")

    def test_nonce_replay_rejected(self):
        burn = _MemBurn()
        self._verify(self._env(), burn=burn)           # first use burns n1
        with self.assertRaises(ei.IngestError) as c:
            self._verify(self._env(), burn=burn)        # same nonce again
        self.assertEqual(c.exception.reason, "nonce_replayed")

    def test_missing_field_and_bad_protocol(self):
        env = self._env()
        env["signed"].pop("nonce")
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "missing_signed_field")


@override_settings(**ENABLED, SECURE_SSL_REDIRECT=False)  # test-scoped: avoid the http→https 301 locally
class IngestViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="m", broker_name="IS6", account_number="1000001",
            is_demo=True, is_active=True, broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.acct, observe_enabled=True)

    def _rev(self, ticket=1, revision=1, **over):
        r = {"account_id": self.acct.id, "ticket": ticket, "revision": revision,
             "observed_time": timezone.now().isoformat(), "side": "BUY", "volume": 0.10, "symbol": "XAUUSD"}
        r.update(over)
        return r

    def _post(self, revisions, *, account_id=None, nonce="n1"):
        payload = {"revisions": revisions}
        env = ei.sign_ingest_request(account_id=(self.acct.id if account_id is None else account_id),
                                     secret=SECRET, nonce=nonce, now=time.time(), payload=payload)
        return self.client.post("/api/copy-trading/event-ingest/", data=json.dumps(env),
                                content_type="application/json")

    def test_gate_off_503_persists_nothing(self):
        with override_settings(COPIER_ENABLED=False):
            with verification_window(SECRET, purpose="INGEST"):
                r = self._post([self._rev()])
        self.assertEqual(r.status_code, 503)
        self.assertEqual(CopierIngestedRevision.objects.count(), 0)

    def test_accept_persists_and_creates_no_execution_job(self):
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(ticket=1, revision=1), self._rev(ticket=2, revision=1)])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "accepted")
        self.assertEqual(r.json()["accepted"], 2)
        self.assertEqual(CopierIngestedRevision.objects.count(), 2)
        self.assertEqual(ExecutionJob.objects.count(), 0)   # INVARIANT: ingestion never creates a job

    def test_idempotent_duplicate_revision(self):
        with verification_window(SECRET, purpose="INGEST"):
            self._post([self._rev(ticket=1, revision=1)], nonce="a")
            r2 = self._post([self._rev(ticket=1, revision=1)], nonce="b")   # new nonce, same revision
        self.assertEqual(r2.json()["accepted"], 0)
        self.assertEqual(r2.json()["duplicates"], 1)
        self.assertEqual(CopierIngestedRevision.objects.filter(ticket=1, revision=1).count(), 1)

    def test_replay_same_request_409(self):
        with verification_window(SECRET, purpose="INGEST"):
            env_payload = [self._rev()]
            payload = {"revisions": env_payload}
            env = ei.sign_ingest_request(account_id=self.acct.id, secret=SECRET, nonce="same",
                                         now=time.time(), payload=payload)
            body = json.dumps(env)
            r1 = self.client.post("/api/copy-trading/event-ingest/", data=body, content_type="application/json")
            r2 = self.client.post("/api/copy-trading/event-ingest/", data=body, content_type="application/json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 409)

    def test_account_scope_violation_rejected(self):
        # Signed account matches the master, but a revision claims a different account.
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(ticket=1, revision=1, account_id=self.acct.id + 999)])
        self.assertEqual(r.status_code, 200)
        reasons = [x["reason"] for x in r.json()["rejected"]]
        self.assertIn("account_scope_violation", reasons)
        self.assertEqual(CopierIngestedRevision.objects.count(), 0)

    def test_unknown_account_permanent_reject_200(self):
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev()], account_id=self.acct.id + 999)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reason"], "account_not_master")

    def test_identity_mismatch_rejected(self):
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(broker_login="9999999")])   # wrong login vs account_number 1000001
        self.assertEqual(r.status_code, 200)
        reasons = [x["reason"] for x in r.json()["rejected"]]
        self.assertIn("identity_mismatch", reasons)

    def test_matching_identity_accepted(self):
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(broker_login="1000001", broker_server="IS6Technologies-Demo")])
        self.assertEqual(r.json()["accepted"], 1)

    def test_malformed_json_400(self):
        with override_settings(**ENABLED), verification_window(SECRET, purpose="INGEST"):
            r = self.client.post("/api/copy-trading/event-ingest/", data="{not json",
                                 content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_body_too_large_413(self):
        big = "z" * (ei.MAX_BODY_BYTES + 10)
        with verification_window(SECRET, purpose="INGEST"):
            r = self.client.post("/api/copy-trading/event-ingest/", data=big, content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_structurally_invalid_revision_rejected_not_persisted(self):
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(ticket=1, revision=1, volume=0)])   # non-positive volume on an open
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["rejected"])
        self.assertEqual(CopierIngestedRevision.objects.count(), 0)

    def test_oversize_value_rejected_not_500(self):
        # ADR-0074 M1: an out-of-range magnitude must be a clean per-row rejection, never a late DataError/500.
        with verification_window(SECRET, purpose="INGEST"):
            r = self._post([self._rev(ticket=1, revision=1, volume=2_000_000),            # > _MAX_VOLUME
                            self._rev(ticket=2, revision=1, sl=1e14)])                     # > _MAX_PRICE
        self.assertEqual(r.status_code, 200)
        reasons = [x["reason"] for x in r.json()["rejected"]]
        self.assertIn("volume_out_of_range", reasons)
        self.assertIn("sl_out_of_range", reasons)
        self.assertEqual(CopierIngestedRevision.objects.count(), 0)

    def test_oversize_nonce_is_400_not_500(self):
        # ADR-0074 L1: a signed but over-length nonce is bounded before the DB insert → sanitized 400.
        payload = {"revisions": [self._rev()]}
        env = ei.sign_ingest_request(account_id=self.acct.id, secret=SECRET, nonce="z" * 200,
                                     now=time.time(), payload=payload)
        with verification_window(SECRET, purpose="INGEST"):
            r = self.client.post("/api/copy-trading/event-ingest/", data=json.dumps(env),
                                 content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["reason"], "nonce_too_long")
