"""MULTI_ACCOUNT_PUBLIC_METRICS §A/§C — TradingAccountBrokerContact association model (DARK, additive).

Proves the Sponsor's many-to-many requirements that the legacy `BrokerEmailIdentity (email, broker_name)` unique
constraint blocked: ONE (email, broker) serving MANY accounts; ONE inbox serving MANY accounts/brokers; credentials
not duplicated per account; per-binding verification; history retained (RETIRE, never delete) on disconnect; the
audit is append-only. Additive + non-destructive: the legacy single-FK bindings on BrokerEmailIdentity are untouched.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.db.utils import InternalError, ProgrammingError
from django.test import TestCase

from broker_intelligence import contact_verification
from broker_intelligence.models import (BrokerEmailIdentity, ConnectedMailbox, TradingAccountBrokerContact,
                                        TradingAccountBrokerContactAudit)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "92%04d" % _n


def _acct(user, **kw):
    login = _uniq()
    d = dict(user=user, name="A", broker_name="TradersWay", account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _identity(email, broker="TradersWay", **kw):
    return BrokerEmailIdentity.objects.create(email=email, broker_name=broker, **kw)


def _mailbox(user, pmid):
    return ConnectedMailbox.objects.create(user=user, provider=ConnectedMailbox.Provider.GMAIL,
                                           provider_mailbox_id=pmid, primary_email=pmid,
                                           status=ConnectedMailbox.Status.CONNECTED)


class AccountBrokerContactModel(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="abc", email="abc@x.invalid", password="x")

    def test_one_email_at_one_broker_serves_many_accounts(self):
        # The core requirement blocked by the (email, broker_name) unique on BrokerEmailIdentity: one registry row,
        # many account bindings.
        idn = _identity("shared@googlemail.com", broker="TradersWay")
        a1, a2, a3 = _acct(self.u), _acct(self.u), _acct(self.u)
        for a in (a1, a2, a3):
            TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)
        self.assertEqual(idn.account_contacts.count(), 3)                      # EMAIL 1 -> MANY ACCOUNTS
        self.assertEqual(BrokerEmailIdentity.objects.filter(email="shared@googlemail.com").count(), 1)  # one registry row

    def test_one_inbox_serves_many_accounts_without_duplicating_credentials(self):
        mb = _mailbox(self.u, "inbox@gmail.com")   # ONE mailbox holds the credential ONCE
        i_tw = _identity("shared@googlemail.com", broker="TradersWay")
        i_is6 = _identity("shared@googlemail.com", broker="IS6")        # same email, different broker => distinct row
        a1, a2 = _acct(self.u), _acct(self.u, broker_name="IS6")
        TradingAccountBrokerContact.objects.create(trading_account=a1, broker_email_identity=i_tw, connected_mailbox=mb)
        TradingAccountBrokerContact.objects.create(trading_account=a2, broker_email_identity=i_is6, connected_mailbox=mb)
        self.assertEqual(mb.account_contacts.count(), 2)                 # ONE INBOX -> MANY ACCOUNTS/BROKERS
        self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)   # credential not duplicated

    def test_duplicate_account_identity_pair_is_rejected(self):
        idn = _identity("dup@gmail.com")
        a = _acct(self.u)
        TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)

    def test_per_binding_verification_default_pending(self):
        idn = _identity("v@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(self.u), broker_email_identity=idn)
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.PENDING)   # mailbox control alone != verified

    def test_retire_preserves_binding_and_is_not_a_delete(self):
        idn = _identity("r@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(self.u), broker_email_identity=idn)
        c.status = TradingAccountBrokerContact.Status.RETIRED
        c.save(update_fields=["status", "updated_at"])
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.RETIRED)
        self.assertTrue(TradingAccountBrokerContact.objects.filter(pk=c.pk).exists())   # retained, never deleted

    def test_legacy_identity_bindings_untouched(self):
        # Non-destructive: a legacy BrokerEmailIdentity.trading_account binding still exists alongside the new table.
        a = _acct(self.u)
        idn = _identity("legacy@gmail.com", user=self.u, trading_account=a,
                        status=BrokerEmailIdentity.Status.VERIFIED)
        self.assertEqual(idn.trading_account_id, a.id)                  # legacy single-FK binding intact
        self.assertEqual(idn.account_contacts.count(), 0)               # new table is additive, not a migration of it


class AccountBrokerContactAudit(TestCase):
    def test_audit_is_append_only(self):
        u = User.objects.create_user(username="aa", email="aa@x.invalid", password="x")
        idn = _identity("a@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(u), broker_email_identity=idn)
        row = TradingAccountBrokerContactAudit.objects.create(
            contact=c, action=TradingAccountBrokerContactAudit.Action.BIND, subject_user=u,
            outcome="OK", status_after="PENDING", proofs={"account_owned": True})
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            row.delete()


class AccountBrokerContactDeletionSafeguards(TestCase):
    """C3 §deletion: CASCADE->PROTECT on trading_account (history-safe) + the operator-purge sequence."""

    def test_protect_blocks_account_hard_delete_with_binding(self):
        u = User.objects.create_user(username="del%s" % _uniq(), email="del@x.invalid", password="x")
        a = _acct(u)
        TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=_identity("p@gmail.com"))
        with self.assertRaises(ProtectedError):
            a.delete()                                          # a bound account is never hard-deleted (history-safe)

    def test_user_purge_succeeds_after_retiring_bindings(self):
        # The operator purge CASCADEs User -> TradingAccount; a PROTECTed binding would abort it, so the purge deletes
        # the user's bindings first (bindings are deletable; audit history survives orphaned). Mirrors the runbook.
        u = User.objects.create_user(username="prg%s" % _uniq(), email="prg@x.invalid", password="x")
        a = _acct(u)
        c = TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=_identity("q@gmail.com"))
        TradingAccountBrokerContactAudit.objects.create(
            contact=c, action=TradingAccountBrokerContactAudit.Action.BIND, subject_user=u,
            outcome="OK", status_after="PENDING")
        TradingAccountBrokerContact.objects.filter(trading_account__user=u).delete()   # purge step: clear bindings
        u.delete()                                              # now succeeds
        self.assertFalse(User.objects.filter(pk=u.pk).exists())
        orphan = TradingAccountBrokerContactAudit.objects.filter(action="BIND").first()
        self.assertIsNotNone(orphan)                            # append-only history SURVIVES the purge
        self.assertIsNone(orphan.contact_id)                    # back-reference decayed to NULL


class AccountBrokerContactAuditDbEnforced(TestCase):
    """C3 §audit-integrity: append-only is DB-ENFORCED (not just the app delete() guard), and SELECTIVE so cascade
    SET_NULL still works."""

    def setUp(self):
        self.u = User.objects.create_user(username="aud%s" % _uniq(), email="aud@x.invalid", password="x")
        self.c = TradingAccountBrokerContact.objects.create(trading_account=_acct(self.u),
                                                            broker_email_identity=_identity("au@gmail.com"))
        self.row = TradingAccountBrokerContactAudit.objects.create(
            contact=self.c, action=TradingAccountBrokerContactAudit.Action.BIND, subject_user=self.u,
            outcome="OK", status_after="PENDING", proofs={"x": 1})

    def test_db_trigger_blocks_evidential_update(self):
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContactAudit.objects.filter(pk=self.row.pk).update(outcome="TAMPERED")

    def test_db_trigger_blocks_bulk_delete(self):
        # The instance delete() guard is bypassed by the ORM bulk path; the BEFORE-DELETE trigger must still refuse.
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContactAudit.objects.filter(pk=self.row.pk).delete()

    def test_db_trigger_blocks_fk_repoint(self):
        # A back-reference may decay to NULL on cascade but NEVER be re-pointed to a different value.
        other = User.objects.create_user(username="oth%s" % _uniq(), email="oth@x.invalid", password="x")
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContactAudit.objects.filter(pk=self.row.pk).update(subject_user=other)

    def test_cascade_setnull_on_audit_does_not_raise(self):
        # Refutation-11 regression: deleting a user who only AUTHORED audit rows (subject_user SET_NULL) must SUCCEED —
        # a blanket update-block would abort this and make the beta purge impossible.
        actor = User.objects.create_user(username="act%s" % _uniq(), email="act@x.invalid", password="x")
        row = TradingAccountBrokerContactAudit.objects.create(
            contact=self.c, action=TradingAccountBrokerContactAudit.Action.VERIFY, subject_user=actor,
            outcome="OK", status_after="VERIFIED")
        actor.delete()                                          # must NOT raise
        row.refresh_from_db()
        self.assertIsNone(row.subject_user_id)                  # reference nulled by cascade
        self.assertEqual(row.action, "VERIFY")                  # recorded fact intact


class AccountBrokerContactVerification(TestCase):
    """C3 §ownership/broker/mailbox: fail-closed VERIFIED (app service + the DB floor)."""

    def setUp(self):
        self.u = User.objects.create_user(username="ver%s" % _uniq(), email="ver@x.invalid", password="x")

    def _contact(self, *, mbx_user=None, idn_user=None, mbx_status=None, acct_broker="TradersWay",
                 idn_broker="TradersWay"):
        a = _acct(self.u, broker_name=acct_broker)
        mb = _mailbox(mbx_user or self.u, "mb%s@gmail.com" % _uniq())
        if mbx_status:
            mb.status = mbx_status
            mb.save(update_fields=["status"])
        idn = _identity("id%s@gmail.com" % _uniq(), broker=idn_broker, user=(idn_user or self.u))
        return TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn,
                                                         connected_mailbox=mb)

    def test_verify_success_sets_verified_and_audits(self):
        c = self._contact()
        contact_verification.verify_contact(c, actor=self.u)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.VERIFIED)
        self.assertIsNotNone(c.verified_at)
        self.assertTrue(c.audit.filter(action="VERIFY", outcome="OK").exists())

    def test_verify_mailbox_owner_mismatch_is_unresolved(self):
        other = User.objects.create_user(username="m%s" % _uniq(), email="m@x.invalid", password="x")
        c = self._contact(mbx_user=other)
        contact_verification.verify_contact(c)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)
        self.assertTrue(c.audit.filter(action="REJECT", outcome="mailbox_owner_mismatch").exists())

    def test_verify_cross_user_identity_is_unresolved(self):
        other = User.objects.create_user(username="i%s" % _uniq(), email="i@x.invalid", password="x")
        c = self._contact(idn_user=other)
        contact_verification.verify_contact(c)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)
        self.assertTrue(c.audit.filter(outcome="identity_owner_mismatch").exists())

    def test_verify_null_mailbox_is_unresolved(self):
        a = _acct(self.u)
        idn = _identity("n%s@gmail.com" % _uniq(), user=self.u)
        c = TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)   # no mailbox
        contact_verification.verify_contact(c)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)

    def test_verify_revoked_mailbox_is_unresolved(self):
        c = self._contact(mbx_status=ConnectedMailbox.Status.REVOKED)
        contact_verification.verify_contact(c)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)

    def test_verify_broker_mismatch_is_unresolved(self):
        c = self._contact(acct_broker="TradersWay", idn_broker="IS6")
        contact_verification.verify_contact(c)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)
        self.assertTrue(c.audit.filter(outcome="broker_mismatch").exists())

    def test_db_trigger_blocks_bulk_verified_cross_user(self):
        # The DB floor: even bypassing the service, a cross-user VERIFIED is refused (fail-closed; bulk/raw can't forge).
        other = User.objects.create_user(username="b%s" % _uniq(), email="b@x.invalid", password="x")
        c = self._contact(mbx_user=other)
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.filter(pk=c.pk).update(
                    status=TradingAccountBrokerContact.Status.VERIFIED)

    def test_db_trigger_blocks_direct_verified_insert_cross_user(self):
        other = User.objects.create_user(username="d%s" % _uniq(), email="d@x.invalid", password="x")
        a = _acct(self.u)
        mb = _mailbox(other, "x%s@gmail.com" % _uniq())
        idn = _identity("z%s@gmail.com" % _uniq(), user=self.u)
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.create(
                    trading_account=a, broker_email_identity=idn, connected_mailbox=mb,
                    status=TradingAccountBrokerContact.Status.VERIFIED)

    def test_db_trigger_blocks_bulk_verified_cross_user_identity(self):
        # The DB floor's SECOND clause: a SAME-owner mailbox but a CROSS-USER identity must still be refused via bulk/
        # raw (the first clause passes, so this exercises the identity-ownership clause independently).
        other = User.objects.create_user(username="ci%s" % _uniq(), email="ci@x.invalid", password="x")
        c = self._contact(idn_user=other)                       # mailbox owned by self.u; identity owned by other
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.filter(pk=c.pk).update(
                    status=TradingAccountBrokerContact.Status.VERIFIED)

    def test_db_trigger_blocks_bulk_verified_null_mailbox(self):
        # The DB floor's completeness clause: a transition to VERIFIED with NO mailbox is refused via bulk/raw too.
        a = _acct(self.u)
        idn = _identity("nm%s@gmail.com" % _uniq(), user=self.u)
        c = TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)   # no mailbox
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.filter(pk=c.pk).update(
                    status=TradingAccountBrokerContact.Status.VERIFIED)

    def test_verified_binding_survives_mailbox_delete(self):
        # Cascade-safety: deleting a VERIFIED binding's mailbox must NOT be aborted by the VERIFIED trigger — the
        # SET_NULL decays the reference (verification goes stale), it never blocks the delete.
        c = self._contact()
        contact_verification.verify_contact(c, actor=self.u)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.VERIFIED)
        mb = c.connected_mailbox
        mb.delete()                                             # must NOT raise (SET_NULL cascade on a VERIFIED row)
        c.refresh_from_db()
        self.assertIsNone(c.connected_mailbox_id)               # mailbox reference decayed to NULL
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.VERIFIED)   # stale, to be re-evaluated

    def test_twenty_accounts_one_shared_email_each_verified(self):
        # §I/§A: one (email, broker) identity bound to 20 of the owner's accounts, each VERIFIED independently.
        mb = _mailbox(self.u, "shared%s@gmail.com" % _uniq())
        idn = _identity("shared%s@gmail.com" % _uniq(), broker="TradersWay", user=self.u)
        contacts = []
        for _ in range(20):
            a = _acct(self.u, broker_name="TradersWay")
            contacts.append(TradingAccountBrokerContact.objects.create(
                trading_account=a, broker_email_identity=idn, connected_mailbox=mb))
        for c in contacts:
            contact_verification.verify_contact(c, actor=self.u)
            c.refresh_from_db()
            self.assertEqual(c.status, TradingAccountBrokerContact.Status.VERIFIED)
        self.assertEqual(idn.account_contacts.filter(status="VERIFIED").count(), 20)
