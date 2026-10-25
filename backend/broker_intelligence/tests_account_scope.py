"""Account-scope correction (P0 demo defect) — the Withdrawal Intelligence surface must FOLLOW the dashboard account
selector. Proves the scope contract for ``withdrawal_metrics`` + ``withdrawal_history`` and the owner-validated view
layer:

  * ALL (no account_id)  -> owner-wide, INCLUDING owner-attributed account-UNRESOLVED withdrawals (ADR-0050).
  * a specific account   -> ONLY withdrawals with an EXACT ``trading_account_id`` match; account-UNRESOLVED
                            (trading_account NULL) and other-account rows excluded; NEVER broker-name inference.
  * a foreign / malformed / not-owned account_id -> FAILS CLOSED (empty), never owner-wide, never leaking existence.

The production record (ref 4113783: COMPLETED, 24.12 USD, trading_account NULL, owner-attributed) is reproduced as an
account-UNRESOLVED withdrawal: it appears under ALL but NEVER under a specific account — not even its own TradersWay
account, because association is never inferred from the broker name."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence.metrics import withdrawal_history, withdrawal_metrics
from broker_intelligence.models import Provenance, Withdrawal
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "95%04d" % _n


def _acct(user, broker="TradersWay", **kw):
    login = _uniq()
    d = dict(user=user, name="A", broker_name=broker, account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _resolved_wd(account, *, amount="100.00", currency="USD", ref="", provenance=Provenance.REAL,
                 status=Withdrawal.Status.COMPLETED, hours=1):
    """A withdrawal verified to a specific GuvFX account (trading_account set)."""
    t0 = timezone.now() - timedelta(hours=hours)
    return Withdrawal.objects.create(
        trading_account=account, owner=None, broker_reference_id=ref,
        amount=(Decimal(amount) if amount is not None else None), currency=currency,
        requested_at=t0, completed_at=timezone.now(), status=status, provenance=provenance)


def _unresolved_wd(owner, *, amount="24.12", currency="USD", ref="4113783", provenance=Provenance.REAL):
    """The production-shaped record: owner-attributed, account-UNRESOLVED (trading_account NULL), lifecycle COMPLETED."""
    t0 = timezone.now() - timedelta(minutes=33)
    return Withdrawal.objects.create(
        trading_account=None, owner=owner, broker_reference_id=ref,
        amount=Decimal(amount), currency=currency, requested_at=t0, completed_at=timezone.now(),
        status=Withdrawal.Status.COMPLETED, provenance=provenance)


class WithdrawalAccountScopeProjection(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="sc1", email="sc1@x.invalid", password="x")
        self.a46 = _acct(self.u, broker="TradersWay")          # same broker as the unresolved record
        self.a48 = _acct(self.u, broker="IS6 Technologies")
        self.a49 = _acct(self.u, broker="Pepperstone")
        self.accts = [self.a46, self.a48, self.a49]
        self.unresolved = _unresolved_wd(self.u)               # the production-shaped 24.12 USD account-UNRESOLVED row

    # (1) ALL includes the unresolved record — metrics AND history.
    def test_all_includes_unresolved(self):
        m = withdrawal_metrics(self.accts, owner=self.u)
        self.assertEqual(m["total"], 1)
        self.assertEqual(m["completed_amount_by_currency"], {"USD": "24.12"})
        h = withdrawal_history(self.accts, owner=self.u)
        self.assertEqual(len(h["withdrawals"]), 1)
        self.assertTrue(h["withdrawals"][0]["account_unresolved"])

    # (2) Account 49 excludes it.
    def test_account49_excludes_unresolved(self):
        self.assertEqual(withdrawal_metrics(self.accts, owner=self.u, account_id=self.a49.id)["total"], 0)
        self.assertEqual(withdrawal_history(self.accts, owner=self.u, account_id=self.a49.id)["withdrawals"], [])

    # (3) Account 46 (same broker, TradersWay) ALSO excludes it while unresolved — no broker-name inference.
    def test_account46_excludes_unresolved_despite_broker_match(self):
        m = withdrawal_metrics(self.accts, owner=self.u, account_id=self.a46.id)
        self.assertEqual(m["total"], 0)
        self.assertEqual(m["unresolved_count"], 0)
        self.assertEqual(m["completed_amount_by_currency"], {})
        self.assertIsNone(m["processing_duration"])
        self.assertEqual(withdrawal_history(self.accts, owner=self.u, account_id=self.a46.id)["withdrawals"], [])

    # (4 & 5) A withdrawal verified to a specific account appears ONLY for that account (and in ALL).
    def test_resolved_withdrawal_appears_only_for_its_account(self):
        _resolved_wd(self.a46, amount="200.00", ref="w46")
        _resolved_wd(self.a48, amount="300.00", ref="w48")
        m46 = withdrawal_metrics(self.accts, owner=self.u, account_id=self.a46.id)
        self.assertEqual(m46["total"], 1)
        self.assertEqual(m46["completed_amount_by_currency"], {"USD": "200.00"})
        m48 = withdrawal_metrics(self.accts, owner=self.u, account_id=self.a48.id)
        self.assertEqual(m48["total"], 1)
        self.assertEqual(m48["completed_amount_by_currency"], {"USD": "300.00"})
        self.assertEqual(withdrawal_metrics(self.accts, owner=self.u, account_id=self.a49.id)["total"], 0)
        self.assertEqual(withdrawal_metrics(self.accts, owner=self.u)["total"], 3)   # w46 + w48 + unresolved
        self.assertEqual([w["broker_reference_id"] for w in
                          withdrawal_history(self.accts, owner=self.u, account_id=self.a46.id)["withdrawals"]], ["w46"])
        self.assertEqual([w["broker_reference_id"] for w in
                          withdrawal_history(self.accts, owner=self.u, account_id=self.a48.id)["withdrawals"]], ["w48"])

    # (6 & 7) Metrics and history use the SAME scope; scoped totals include ONLY matching records.
    def test_metrics_and_history_use_same_scope(self):
        _resolved_wd(self.a46, amount="200.00", ref="w46")
        for aid in (self.a46.id, self.a48.id, self.a49.id, None):
            m = withdrawal_metrics(self.accts, owner=self.u, account_id=aid)
            h = withdrawal_history(self.accts, owner=self.u, account_id=aid)
            self.assertEqual(m["total"], len(h["withdrawals"]), f"scope mismatch for account_id={aid}")

    # (8) A selected account with no verified withdrawals -> empty scope (NOT the owner-wide totals).
    def test_account_with_no_records_is_empty_not_ownerwide(self):
        m = withdrawal_metrics(self.accts, owner=self.u, account_id=self.a48.id)
        self.assertEqual(m["total"], 0)
        self.assertEqual(m["completed_amount_by_currency"], {})

    # (14) Owner-wide behaviour unchanged (no account_id == legacy owner-wide, incl. unresolved).
    def test_owner_wide_unchanged(self):
        _resolved_wd(self.a46, amount="200.00", ref="w46")
        m = withdrawal_metrics(self.accts, owner=self.u)
        self.assertEqual(m["total"], 2)                                   # resolved w46 + the unresolved
        self.assertEqual(m["completed_amount_by_currency"], {"USD": "224.12"})

    # Defence in depth: the projection itself fails closed for a NOT-owned id passed directly.
    def test_projection_foreign_id_intersects_empty(self):
        other = User.objects.create_user(username="sc1o", email="sc1o@x.invalid", password="x")
        oa = _acct(other, broker="TradersWay")
        _resolved_wd(oa, amount="999.00", ref="theirs")
        self.assertEqual(withdrawal_metrics(self.accts, owner=self.u, account_id=oa.id)["total"], 0)


@override_settings(BROKER_WITHDRAWAL_UX_ENABLED="1")
class WithdrawalAccountScopeApi(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="sc2", email="sc2@x.invalid", password="x")
        self.a46 = _acct(self.u, broker="TradersWay")
        self.unresolved = _unresolved_wd(self.u)
        self.c = APIClient()
        self.c.force_authenticate(self.u)

    def _m(self, qs=""):
        return self.c.get("/api/broker-intelligence/withdrawals/metrics/" + qs)

    def _h(self, qs=""):
        return self.c.get("/api/broker-intelligence/withdrawals/history/" + qs)

    def test_api_owner_wide_includes_unresolved(self):
        r = self._m()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 1)

    def test_api_account_scope_excludes_unresolved(self):
        r = self._m(f"?account_id={self.a46.id}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 0)
        self.assertEqual(self._h(f"?account_id={self.a46.id}").json()["withdrawals"], [])

    # (10) A FOREIGN tenant's account id must not expose data OR existence (200 + empty, indistinguishable from empty).
    def test_foreign_account_id_fails_closed(self):
        other = User.objects.create_user(username="sc2o", email="sc2o@x.invalid", password="x")
        oa = _acct(other, broker="TradersWay")
        _resolved_wd(oa, amount="999.00", ref="theirs")
        r = self._m(f"?account_id={oa.id}")
        self.assertEqual(r.status_code, 200, r.content)                    # no 403/404 existence signal
        self.assertEqual(r.json()["total"], 0)
        self.assertEqual(r.json()["completed_amount_by_currency"], {})     # never the foreign account's data
        self.assertEqual(self._h(f"?account_id={oa.id}").json()["withdrawals"], [])

    # (11) Malformed / inaccessible id fails closed (no 500, NOT owner-wide).
    def test_malformed_account_id_fails_closed(self):
        r = self._m("?account_id=not-an-int")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 0)
        rh = self._h("?account_id=not-an-int")
        self.assertEqual(rh.status_code, 200, rh.content)
        self.assertEqual(rh.json()["withdrawals"], [])

    def test_nonexistent_owned_int_fails_closed(self):
        r = self._m("?account_id=99999999")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 0)
