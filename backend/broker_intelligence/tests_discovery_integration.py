"""ADR-0089 — integration tests: the discovery SERVICE (redacted candidates, idempotency, owner-scope, no event/
withdrawal, PII-free), the acquisition BODY-FETCH gate (`_acquire_discovery`), the `ingest_mailbox` discovery pass
(no cursor rewind), and the staff review API. SYNTHETIC fixtures only."""
from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from broker_intelligence import discovery as disc
from broker_intelligence import parsers
from broker_intelligence.models import (BrokerEvent, ConnectedMailbox, DiscoveryCandidateAction, EvidenceBlob,
                                        GenericWithdrawalPattern, UnknownSenderDiscoveryCandidate, Withdrawal)
from broker_intelligence.views import DiscoveryCandidateActionView, DiscoveryCandidateListView

User = get_user_model()
_CANARY = "disc@guvfx.test,disc-b@guvfx.test,staff@guvfx.test"


def _raw(from_hdr, subject, body, *, auth="dmarc=pass; spf=pass; dkim=pass"):
    return (f"From: {from_hdr}\r\nTo: owner@gmail.com\r\nSubject: {subject}\r\n"
            f"Authentication-Results: mx.google.com; {auth}\r\n"
            "Date: Wed, 08 Oct 2026 10:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode("utf-8")


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com",
                   BROKER_UNKNOWN_SENDER_DISCOVERY_OWNER_ALLOWLIST=_CANARY)
class _Base(TestCase):
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp(prefix="bi-disc-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        parsers.clear_registry()
        self.addCleanup(parsers.clear_registry)
        self.user = User.objects.create_user(username="disc", email="disc@guvfx.test", password="x")

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)
        super().tearDown()


class Service(_Base):
    def _msg(self, pmid, from_hdr="Acme <no-reply@acme-fx.io>", subject="Withdrawal completed",
             body="your withdrawal has been completed"):
        return {"provider_message_id": pmid, "raw_bytes": _raw(from_hdr, subject, body)}

    def test_creates_redacted_candidate_no_event_no_withdrawal(self):
        n = disc.process_discovery_messages([self._msg("m1")], owner=self.user)
        self.assertEqual(n, 1)
        c = UnknownSenderDiscoveryCandidate.objects.get()
        self.assertEqual(c.owner_id, self.user.id)
        self.assertEqual(c.lifecycle_class, "COMPLETED")
        self.assertTrue(c.auth_policy_passed)
        self.assertEqual(c.registrable_domain, "acme-fx.io")           # auth-approved broker public domain
        self.assertEqual(len(c.message_sha256), 64)
        self.assertEqual(BrokerEvent.objects.count(), 0)               # NEVER an event
        self.assertEqual(Withdrawal.objects.count(), 0)               # NEVER a withdrawal
        self.assertEqual(EvidenceBlob.objects.count(), 0)            # raw NEVER stored

    def test_auth_fail_no_candidate(self):
        self.assertEqual(disc.process_discovery_messages(
            [{"provider_message_id": "m2", "raw_bytes": _raw("x <a@x.io>", "Withdrawal completed",
                                                             "has been completed", auth="dmarc=fail")}],
            owner=self.user), 0)
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.count(), 0)

    def test_negatives_no_candidate(self):
        msgs = [self._msg("d", subject="Deposit received", body="your deposit completed"),
                self._msg("mk", subject="Trade now", body="webinar bonus offer unsubscribe"),
                self._msg("kyc", subject="Verify your identity", body="complete your verification kyc")]
        self.assertEqual(disc.process_discovery_messages(msgs, owner=self.user), 0)

    def test_idempotent_replay(self):
        disc.process_discovery_messages([self._msg("rep")], owner=self.user)
        disc.process_discovery_messages([self._msg("rep")], owner=self.user)   # same pmid
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.filter(owner=self.user).count(), 1)

    def test_owner_scoped(self):
        ub = User.objects.create_user(username="ub", email="disc-b@guvfx.test", password="x")
        disc.process_discovery_messages([self._msg("a")], owner=self.user)
        disc.process_discovery_messages([self._msg("b")], owner=ub)
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.filter(owner=ub).count(), 1)

    def test_candidate_row_has_no_pii(self):
        m = self._msg("pii", from_hdr="John Smith <john.smith@acme-fx.io>",
                      body="Dear John Smith, your withdrawal of GBP 42.00 to account 99887766 ref WX123 has been completed")
        disc.process_discovery_messages([m], owner=self.user)
        c = UnknownSenderDiscoveryCandidate.objects.get()
        blob = " ".join(str(x) for x in (c.registrable_domain, c.sender_domain_hash, c.message_sha256,
                                         c.provider_message_id_hash, c.spf, c.dkim, c.dmarc, c.lifecycle_class,
                                         c.confidence_band, c.detector_version, " ".join(c.reason_codes or [])))
        for bad in ("John", "Smith", "john.smith", "99887766", "WX123", "42.00", "has been completed"):
            self.assertNotIn(bad, blob)


class AcquisitionGate(_Base):
    """The body-fetch narrowing in GmailApiClient._acquire_discovery (no real network/token)."""
    def _client(self):
        from broker_intelligence.gmail_source import GmailApiClient
        mb = ConnectedMailbox.objects.create(user=self.user, provider=ConnectedMailbox.Provider.GMAIL,
                                             provider_mailbox_id="owner@gmail.com", credential_ref="cr" + "0" * 32,
                                             cursor_state="100", status=ConnectedMailbox.Status.CONNECTED)
        return GmailApiClient(mb, client_id="cid", client_secret="sec", discovery_enabled=True)

    def _meta(self, frm, subject):
        return {"payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]}}

    def test_allowlisted_sender_skipped(self):
        c = self._client()
        with mock.patch.object(c, "_get_json", return_value=self._meta("x <a@tradersway.com>", "Withdrawal done")), \
             mock.patch.object(c, "_fetch_message") as fm:
            self.assertIsNone(c._acquire_discovery("mid1"))
            fm.assert_not_called()                                    # allowlisted -> normal path, no discovery raw fetch

    def test_no_subject_keyword_no_raw_fetch(self):
        c = self._client()
        with mock.patch.object(c, "_get_json", return_value=self._meta("x <a@acme-fx.io>", "Your monthly newsletter")), \
             mock.patch.object(c, "_fetch_message") as fm:
            self.assertIsNone(c._acquire_discovery("mid2"))
            fm.assert_not_called()                                    # no withdrawal keyword in Subject -> body NEVER fetched

    def test_keyword_subject_fetches_raw(self):
        c = self._client()
        with mock.patch.object(c, "_get_json", return_value=self._meta("x <a@acme-fx.io>", "Withdrawal completed")), \
             mock.patch.object(c, "_fetch_message", return_value={"provider_message_id": "mid3", "raw_bytes": b"x"}) as fm:
            out = c._acquire_discovery("mid3")
            fm.assert_called_once()
            self.assertEqual(out["provider_message_id"], "mid3")


class _FakeClientWithDiscovery:
    """Returns no allowlisted mail but exposes discovery_raw (what the real incremental walk would have collected)."""
    def __init__(self, discovery_raw, new_cursor="250"):
        self.discovery_raw = discovery_raw
        self._cur = new_cursor
        self.messages_gone = 0

    def list_new_messages(self, cursor_state):
        return [], self._cur


class IngestPass(_Base):
    def setUp(self):
        super().setUp()
        self.mb = ConnectedMailbox.objects.create(user=self.user, provider=ConnectedMailbox.Provider.GMAIL,
                                                  provider_mailbox_id="owner@gmail.com", credential_ref="cr" + "0" * 32,
                                                  cursor_state="100", status=ConnectedMailbox.Status.CONNECTED)

    def test_discovery_processed_for_canary_cursor_advances_no_rewind(self):
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        draw = [{"provider_message_id": "n1", "raw_bytes": _raw("Acme <no-reply@acme-fx.io>", "Withdrawal completed",
                                                                "your withdrawal has been completed")}]
        res = ingest_mailbox(self.mb, client=_FakeClientWithDiscovery(draw))
        self.assertTrue(res["ok"], res)
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "250")                 # advanced FORWARD (100 -> 250), never rewound
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(BrokerEvent.objects.count(), 0)
        self.assertEqual(EvidenceBlob.objects.count(), 0)

    @override_settings(BROKER_UNKNOWN_SENDER_DISCOVERY_OWNER_ALLOWLIST="")   # owner NOT in canary
    def test_discovery_off_canary_no_candidate(self):
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        draw = [{"provider_message_id": "n2", "raw_bytes": _raw("Acme <no-reply@acme-fx.io>", "Withdrawal completed",
                                                                "has been completed")}]
        res = ingest_mailbox(self.mb, client=_FakeClientWithDiscovery(draw))
        self.assertTrue(res["ok"], res)
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.count(), 0)   # gate off -> not processed


class ReviewApi(_Base):
    def setUp(self):
        super().setUp()
        disc.process_discovery_messages(
            [{"provider_message_id": "r1", "raw_bytes": _raw("Acme <no-reply@acme-fx.io>", "Withdrawal completed",
                                                             "your withdrawal has been completed")}], owner=self.user)
        self.cand = UnknownSenderDiscoveryCandidate.objects.get()

    def _list(self, user):
        req = APIRequestFactory().get("/x"); force_authenticate(req, user=user)
        r = DiscoveryCandidateListView.as_view()(req); r.render()
        import json
        return r.status_code, json.loads(r.content or b"null")

    def _act(self, user, cid, data):
        req = APIRequestFactory().post("/x", data, format="json"); force_authenticate(req, user=user)
        r = DiscoveryCandidateActionView.as_view()(req, candidate_id=cid); r.render()
        import json
        return r.status_code, json.loads(r.content or b"null")

    def test_list_owner_scoped_and_gated(self):
        sc, body = self._list(self.user)
        self.assertEqual(sc, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["candidates"][0]["lifecycle_class"], "COMPLETED")
        outsider = User.objects.create_user(username="out", email="out@nope.test", password="x")
        self.assertEqual(self._list(outsider)[0], 404)

    def test_action_requires_staff_and_audits_reject(self):
        self.assertEqual(self._act(self.user, self.cand.id, {"action": "REJECTED"})[0], 403)
        staff = User.objects.create_user(username="st", email="staff@guvfx.test", password="x")
        staff.is_staff = True; staff.save(update_fields=["is_staff"])
        sc, body = self._act(staff, self.cand.id, {"action": "REJECTED", "reason": "not a withdrawal"})
        self.assertEqual(sc, 200)
        self.cand.refresh_from_db()
        self.assertEqual(self.cand.review_state, "REJECTED")
        act = DiscoveryCandidateAction.objects.get()
        self.assertEqual(act.actor, "user:%d" % staff.id)
        self.assertEqual(act.message_sha256, self.cand.message_sha256)
        self.assertEqual(Withdrawal.objects.count(), 0)               # a rejected candidate creates no withdrawal

    def test_propose_pattern_creates_disabled_and_inert(self):
        staff = User.objects.create_user(username="st2", email="staff@guvfx.test", password="x")
        staff.is_staff = True; staff.save(update_fields=["is_staff"])
        sc, body = self._act(staff, self.cand.id, {"action": "PROPOSED_PATTERN"})
        self.assertEqual(sc, 200)
        pat = GenericWithdrawalPattern.objects.get(pk=body["proposed_pattern_id"])
        self.assertFalse(pat.enabled)                                 # DISABLED -> inert; cannot create an event
        self.assertEqual(pat.authenticated_sender_domains, ["acme-fx.io"])
        # the domain is NOT allowlisted (no active BrokerProfile), so even the disabled pattern can never fire
        from broker_intelligence.broker_senders import is_allowlisted_broker_sender
        self.assertFalse(is_allowlisted_broker_sender("no-reply@acme-fx.io"))


class CursorIsolation(_Base):
    def test_discovery_fetch_error_never_aborts_pass_or_holds_cursor(self):
        from broker_intelligence.gmail_source import GmailApiClient, GmailApiError
        mb = ConnectedMailbox.objects.create(user=self.user, provider=ConnectedMailbox.Provider.GMAIL,
                                             provider_mailbox_id="owner@gmail.com", credential_ref="cr" + "0" * 32,
                                             cursor_state="100", status=ConnectedMailbox.Status.CONNECTED)
        c = GmailApiClient(mb, client_id="cid", client_secret="sec", discovery_enabled=True)
        with mock.patch.object(c, "_collect_added_ids", return_value=(["mid1"], "250")), \
             mock.patch.object(c, "_acquire_if_broker", return_value=None), \
             mock.patch.object(c, "_acquire_discovery", side_effect=GmailApiError(500, "messages.metadata")):
            out, cur = c.list_new_messages("100")        # discovery error must be swallowed, NOT raised
        self.assertEqual(out, [])
        self.assertEqual(cur, "250")                      # cursor advances forward; discovery never holds it


class ProposeIdempotent(_Base):
    def setUp(self):
        super().setUp()
        disc.process_discovery_messages(
            [{"provider_message_id": "pr", "raw_bytes": _raw("Acme <no-reply@acme-fx.io>", "Withdrawal completed",
                                                             "your withdrawal has been completed")}], owner=self.user)
        self.cand = UnknownSenderDiscoveryCandidate.objects.get()
        self.staff = User.objects.create_user(username="sti", email="staff@guvfx.test", password="x")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])

    def _act(self, data):
        req = APIRequestFactory().post("/x", data, format="json"); force_authenticate(req, user=self.staff)
        r = DiscoveryCandidateActionView.as_view()(req, candidate_id=self.cand.id); r.render()
        import json
        return r.status_code, json.loads(r.content or b"null")

    def test_repeat_propose_pattern_is_idempotent_no_500(self):
        sc1, b1 = self._act({"action": "PROPOSED_PATTERN"})
        sc2, b2 = self._act({"action": "PROPOSED_PATTERN"})
        self.assertEqual((sc1, sc2), (200, 200))          # a repeat must not 500 (IntegrityError)
        self.assertEqual(b1["proposed_pattern_id"], b2["proposed_pattern_id"])
        self.assertEqual(GenericWithdrawalPattern.objects.filter(enabled=False).count(), 1)


class VantageTwoMessageLifecycle(_Base):
    """ADR-0089 live-observation (Vantage): the two-message lifecycle produces two REDACTED candidates (same broker,
    distinct messages), never a withdrawal/event, raw never stored, replay-idempotent, PII-free. SYNTHETIC only."""
    FOOTER = "<footer>Unsubscribe here | Trade now | Login to your account</footer>"

    def _rawv(self, subject, body):
        return _raw("Vantage <no-reply@vantagemarkets.test>", subject,
                    "<html><body><p>%s</p>%s</body></html>" % (body, self.FOOTER))

    def test_two_messages_two_candidates_same_domain_no_withdrawal(self):
        msgs = [{"provider_message_id": "v1", "raw_bytes": self._rawv("Withdrawal Submitted",
                                                                      "your withdrawal has been submitted")},
                {"provider_message_id": "v2", "raw_bytes": self._rawv("Withdrawal Request Successful",
                                                                      "your withdrawal request was successful")}]
        self.assertEqual(disc.process_discovery_messages(msgs, owner=self.user), 2)
        cands = list(UnknownSenderDiscoveryCandidate.objects.filter(owner=self.user).order_by("id"))
        self.assertEqual(len(cands), 2)
        for c in cands:
            self.assertEqual(c.lifecycle_class, "REQUESTED")        # conservative; NEVER completed
            self.assertEqual(c.registrable_domain, "vantagemarkets.test")
        self.assertEqual(cands[0].sender_domain_hash, cands[1].sender_domain_hash)   # same broker -> groupable
        self.assertNotEqual(cands[0].message_sha256, cands[1].message_sha256)        # two distinct observations
        self.assertEqual(BrokerEvent.objects.count(), 0)            # NO event
        self.assertEqual(Withdrawal.objects.count(), 0)            # NO withdrawal auto-created
        self.assertEqual(EvidenceBlob.objects.count(), 0)         # raw NEVER stored

    def test_replay_idempotent(self):
        m = {"provider_message_id": "vrep", "raw_bytes": self._rawv("Withdrawal Submitted",
                                                                    "your withdrawal has been submitted")}
        disc.process_discovery_messages([m], owner=self.user)
        disc.process_discovery_messages([m], owner=self.user)
        self.assertEqual(UnknownSenderDiscoveryCandidate.objects.filter(owner=self.user).count(), 1)

    def test_candidate_is_pii_free(self):
        m = {"provider_message_id": "vpii",
             "raw_bytes": self._rawv("Withdrawal Submitted",
                                     "Dear John Smith, your withdrawal 99887766 ref WX1 has been submitted")}
        disc.process_discovery_messages([m], owner=self.user)
        c = UnknownSenderDiscoveryCandidate.objects.get()
        blob = " ".join(str(x) for x in (c.registrable_domain, c.sender_domain_hash, c.message_sha256,
                                         c.provider_message_id_hash, c.lifecycle_class, " ".join(c.reason_codes or [])))
        for bad in ("John", "Smith", "99887766", "WX1", "has been submitted"):
            self.assertNotIn(bad, blob)
