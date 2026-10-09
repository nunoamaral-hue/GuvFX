"""WP3 — ingestion core: parser contract/registry, alias->account resolver, and the ingest pipeline.

Covers: resolver (bound/unbound/retired/unknown), parser selection, pipeline (parse->event, unparseable->no event
but evidence retained, idempotent re-ingest, provenance default SYNTHETIC + explicit REAL, money as Decimal, never
fabricate absent fields), and isolation (no execution/strategy imports). All DARK — exercised via FixtureMailSource."""
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import ingestion, parsers, resolver
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.mail_source import FixtureMailSource, MailMessage
from broker_intelligence.models import BrokerEmailAlias, BrokerEvent, EvidenceBlob, Provenance
from broker_intelligence.services import mint_alias_for_account, retire_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "92%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="wp3%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=True, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _msg(to_addr, *, subject="Withdrawal request received", body="Ref ABC123 100.50 USD",
         frm="noreply@tradersway.invalid", raw=None, auth_verdict=None):
    return MailMessage(
        raw_bytes=raw if raw is not None else ("to=%s|%s|%s" % (to_addr, subject, body)).encode(),
        to_addresses=(to_addr,), from_address=frm, subject=subject, body=body,
        received_at=timezone.now(), provider_message_id=_uniq(), auth_verdict=auth_verdict)


class _FakeParser:
    """A deterministic fixture parser (stands in for the WP4 TradersWay parser)."""
    name = "fake"
    version = "v1"

    def can_parse(self, *, subject, body, from_address):
        return "withdrawal" in (subject or "").lower()

    def parse(self, *, subject, body, from_address):
        return parsers.ParsedBrokerEvent(
            event_type="WITHDRAWAL_REQUESTED", broker="TradersWay",
            amount=Decimal("100.50"), currency="USD", broker_reference_id="ABC123",
            confidence=Decimal("0.900"))


class _TmpEvidence(TestCase):
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp(prefix="bi-wp3-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()

    def tearDown(self):
        parsers.clear_registry()
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)
        super().tearDown()


class ResolverTests(_TmpEvidence):
    def test_resolves_active_alias_to_account(self):
        a = _acct()
        al = mint_alias_for_account(a)
        alias, account = resolver.resolve([al.address()])
        self.assertEqual(alias.pk, al.pk)
        self.assertEqual(account.pk, a.pk)

    def test_unknown_address_resolves_nothing(self):
        alias, account = resolver.resolve(["ba" + "0" * 32 + "@accounts.guvfx.com"])
        self.assertIsNone(alias)
        self.assertIsNone(account)

    def test_retired_alias_resolves_alias_but_not_account(self):
        a = _acct()
        al = mint_alias_for_account(a)
        retire_alias_for_account(a)
        alias, account = resolver.resolve([al.address()])
        self.assertEqual(alias.pk, al.pk)           # evidence still attributable
        self.assertIsNone(account)                  # but a removed account is never treated as live

    def test_display_name_is_not_matched(self):
        a = _acct()
        al = mint_alias_for_account(a)
        # An address whose display-name contains the token but whose real local-part differs must NOT match.
        alias, _ = resolver.resolve(["%s <someoneelse@accounts.guvfx.com>" % al.alias_local])
        self.assertIsNone(alias)


class ParserRegistryTests(_TmpEvidence):
    def test_select_returns_first_matching(self):
        parsers.register_parser(_FakeParser())
        p = parsers.select_parser(subject="Withdrawal request", body="x", from_address="y")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "fake")

    def test_select_none_when_no_match(self):
        parsers.register_parser(_FakeParser())
        self.assertIsNone(parsers.select_parser(subject="Deposit", body="x", from_address="y"))

    def test_register_is_idempotent_by_name_version(self):
        parsers.register_parser(_FakeParser())
        parsers.register_parser(_FakeParser())
        self.assertEqual(len(parsers.get_parsers()), 1)


class IngestPipelineTests(_TmpEvidence):
    def test_parse_creates_bound_event_with_evidence(self):
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        ev = ingestion.ingest_message(_msg(al.address()), store=self.store)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.trading_account_id, a.id)
        self.assertEqual(ev.alias_id, al.id)
        self.assertEqual(ev.event_type, "WITHDRAWAL_REQUESTED")
        self.assertIsInstance(ev.amount, Decimal)
        self.assertEqual(ev.amount, Decimal("100.50"))
        self.assertEqual(ev.currency, "USD")
        self.assertEqual(ev.broker_reference_id, "ABC123")
        self.assertEqual(ev.parser_name, "fake")
        self.assertEqual(ev.evidence.sha256, ev.evidence_hash)
        self.assertEqual(ev.provenance, Provenance.SYNTHETIC)         # default label
        self.assertEqual(ev.correlation_status, BrokerEvent.CorrelationStatus.UNRESOLVED)

    def test_unparseable_retains_evidence_but_emits_no_event(self):
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        ev = ingestion.ingest_message(_msg(al.address(), subject="Deposit confirmation"), store=self.store)
        self.assertIsNone(ev)                                         # no fabricated event
        self.assertEqual(BrokerEvent.objects.count(), 0)
        self.assertEqual(EvidenceBlob.objects.count(), 1)            # raw evidence quarantined, not dropped

    def test_ingest_is_idempotent_by_content(self):
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        raw = b"identical-bytes-for-idempotency"
        m1 = _msg(al.address(), raw=raw)
        m2 = _msg(al.address(), raw=raw)
        e1 = ingestion.ingest_message(m1, store=self.store)
        e2 = ingestion.ingest_message(m2, store=self.store)
        self.assertEqual(e1.pk, e2.pk)                               # same evidence blob -> same event
        self.assertEqual(BrokerEvent.objects.count(), 1)
        self.assertEqual(EvidenceBlob.objects.count(), 1)

    def test_unknown_recipient_still_captures_event_with_no_account(self):
        parsers.register_parser(_FakeParser())
        ev = ingestion.ingest_message(_msg("ba" + "0" * 32 + "@accounts.guvfx.com"), store=self.store)
        self.assertIsNotNone(ev)
        self.assertIsNone(ev.trading_account_id)                     # unresolved account
        self.assertIsNone(ev.alias_id)

    def test_raising_parser_is_declined_not_crashed(self):
        # Symmetry with can_parse's sandbox: a parser that matches then RAISES in parse() must be treated as
        # 'declined' — evidence retained, no event, no exception out of ingest_message.
        class _Boom:
            name, version = "boom", "v1"
            def can_parse(self, **kw):
                return True
            def parse(self, **kw):
                raise RuntimeError("parser blew up")
        parsers.register_parser(_Boom())
        a = _acct()
        al = mint_alias_for_account(a)
        ev = ingestion.ingest_message(_msg(al.address()), store=self.store)   # must NOT raise
        self.assertIsNone(ev)
        self.assertEqual(BrokerEvent.objects.count(), 0)
        self.assertEqual(EvidenceBlob.objects.count(), 1)                     # raw evidence retained

    def test_evidence_unique_is_db_enforced(self):
        # Structural idempotency backstop: a second BrokerEvent for the same evidence blob is refused at the DB
        # (uniq_brokerevent_evidence), not just by the app-layer pre-check — so a concurrent race cannot duplicate.
        from django.db import IntegrityError, transaction
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        ev = ingestion.ingest_message(_msg(al.address()), store=self.store)
        self.assertIsNotNone(ev)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEvent.objects.create(
                    trading_account=a, event_type="WITHDRAWAL_COMPLETED", received_at=timezone.now(),
                    evidence=ev.evidence, evidence_hash=ev.evidence.sha256)

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.invalid")
    def test_explicit_real_provenance_is_honoured(self):
        # REAL mail now requires an allowlisted broker sender (R1) AND a passing auth verdict (R3b) before an event is
        # created. With both satisfied, the explicit REAL provenance is honoured.
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        ev = ingestion.ingest_message(_msg(al.address(), auth_verdict="pass"),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertEqual(ev.provenance, Provenance.REAL)

    def test_fixture_source_roundtrip(self):
        parsers.register_parser(_FakeParser())
        a = _acct()
        al = mint_alias_for_account(a)
        src = FixtureMailSource(messages=[_msg(al.address())])
        fetched = list(src.fetch())
        self.assertEqual(len(fetched), 1)
        ev = ingestion.ingest_message(fetched[0], store=self.store)
        src.ack(fetched[0])
        self.assertIsNotNone(ev)
        self.assertEqual(list(src.fetch()), [])                      # acked -> gone


class IsolationTests(TestCase):
    def test_ingestion_modules_do_not_import_execution_or_strategies(self):
        # Architectural boundary: the ingestion plane must never import the trading execution/strategy domains.
        import inspect

        import broker_intelligence.ingestion as ing
        import broker_intelligence.mail_source as ms
        import broker_intelligence.parsers as pz
        import broker_intelligence.resolver as rz
        for mod in (ing, ms, pz, rz):
            src = inspect.getsource(mod)
            for forbidden in ("import execution", "from execution", "import strategies", "from strategies"):
                self.assertNotIn(forbidden, src, f"{mod.__name__} must not import trading execution/strategy domains")
