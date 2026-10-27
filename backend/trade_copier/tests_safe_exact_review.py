"""Governed SAFE-EXACT-only reviewed-mapping persistence controls (ADR-0062).

Proves the reviewer-gated discovery-approval layer added to ``InstrumentMapping`` + the
``refresh_instrument_mappings --safe-exact-only`` mode: dry-run is inert; commit fails closed without a
named reviewer and an evidence reference; ONLY SAFE-EXACT candidates persist (AMBIGUOUS / INELIGIBLE /
ABSENT / STALE / hard-dimension / material-warning are all skipped); self-pair and cross-tenant are refused;
it is idempotent and directional; the existing verified ``CopierSymbolMap`` is never touched; approved
discovery rows never become execution-VERIFIED; ``InstrumentMapping`` stays off the execution path; and no
flag / ExecutionJob / order is created.
"""
import os
import pathlib

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from execution.models import BrokerInstrument, ExecutionJob
from trade_copier import flags
from trade_copier import matrix as M
from trade_copier.matrix import generate_candidates, is_safe_exact
from trade_copier.models import (CopierSymbolMap, InstrumentMapping, ReviewStatus)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

# SPOT FX, USD-settled, identical on both sides => SAFE-EXACT.
_EUR = {"contract_size": 100000.0, "currency_base": "EUR", "currency_profit": "USD", "currency_margin": "EUR",
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\EURUSD"}
# Same quote/settlement/contract_type but a DIFFERENT contract_size => soft material warning (not SAFE-EXACT).
_GBP_100K = {"contract_size": 100000.0, "currency_base": "GBP", "currency_profit": "USD", "currency_margin": "GBP",
             "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\GBPUSD"}
_GBP_50K = {**_GBP_100K, "contract_size": 50000.0}
# Hard-dimension mismatch: settlement currency differs (USD vs AUD) => AMBIGUOUS.
_AUD_USD = {"contract_size": 100000.0, "currency_base": "AUD", "currency_profit": "USD", "currency_margin": "AUD",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\AUDUSD"}
_AUD_AUD = {**_AUD_USD, "currency_profit": "AUD"}
# Two follower symbols of the same canonical => AMBIGUOUS (multiple matches).
_CAD = {"contract_size": 100000.0, "currency_base": "USD", "currency_profit": "CAD", "currency_margin": "USD",
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\USDCAD"}
_XAG = {"contract_size": 5000.0, "currency_base": "XAG", "currency_profit": "USD", "currency_margin": "XAG",
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 50.0, "trade_mode": 4, "path": "Metals\\XAGUSD"}
_XAG_DISABLED = {**_XAG, "trade_mode": 0}  # not openable on follower => INELIGIBLE
_IDX = {"contract_size": 1.0, "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
        "volume_min": 0.1, "volume_step": 0.1, "volume_max": 100.0, "trade_mode": 4, "path": "Indices\\NAS100"}
_XAU_USD = {"contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD", "currency_margin": "XAU",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "trade_mode": 4, "path": "Metals\\XAUUSD"}
_XAU_STD = {"contract_size": 100.0, "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0, "trade_mode": 4, "path": "Spot\\XAUUSD.std"}


def _acct(user, name, num, broker, server):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


def _bi(account, symbol, base, meta, *, enabled=True):
    return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=base,
                                           enabled=enabled, metadata=meta)


class SafeExactFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="f", email="f@guvfx.invalid", password="x")
        self.other = User.objects.create_user(username="g", email="g@guvfx.invalid", password="x")
        self.m = _acct(self.user, "TradersWay", "46", "TradersWay", "Tradersway-Live")
        self.f = _acct(self.user, "IS6FX", "48", "IS6FX", "IS6Technologies-Live")
        self.x = _acct(self.other, "OtherTenant", "99", "IS6FX", "IS6Technologies-Live")
        # master eligible set (7): one of each candidate type
        _bi(self.m, "XAUUSD", "XAUUSD", _XAU_USD)   # -> VERIFIED (symbol map)
        _bi(self.m, "EURUSD", "EURUSD", _EUR)       # -> UNVERIFIED, no warning => SAFE-EXACT
        _bi(self.m, "GBPUSD", "GBPUSD", _GBP_100K)  # -> UNVERIFIED + contract_size warning => NOT safe
        _bi(self.m, "AUDUSD", "AUDUSD", _AUD_USD)   # -> AMBIGUOUS (hard settlement mismatch)
        _bi(self.m, "USDCAD", "USDCAD", _CAD)       # -> AMBIGUOUS (multiple follower matches)
        _bi(self.m, "XAGUSD", "XAGUSD", _XAG)       # -> INELIGIBLE (follower disabled)
        _bi(self.m, "NAS100", "NAS100", _IDX)       # -> ABSENT (no follower counterpart)
        # follower
        _bi(self.f, "XAUUSD.std", "XAUUSD", _XAU_STD)
        _bi(self.f, "EURUSD.std", "EURUSD", _EUR)
        _bi(self.f, "GBPUSD.std", "GBPUSD", _GBP_50K)
        _bi(self.f, "AUDUSD.x", "AUDUSD", _AUD_AUD)
        _bi(self.f, "USDCAD", "USDCAD", _CAD)
        _bi(self.f, "USDCAD.pro", "USDCAD", _CAD)
        _bi(self.f, "XAGUSD.std", "XAGUSD", _XAG_DISABLED)
        # follower also carries EURUSD so the reverse direction f->m has a SAFE-EXACT too (direction test)
        self.vmap = CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.user, verified_at=timezone.now())

    def _commit_safe_exact(self, master, follower, **kw):
        kw.setdefault("reviewer", "nuno-operator")
        kw.setdefault("evidence", "notion:406d72ea-phaseB")
        return call_command("refresh_instrument_mappings", master=master.id, follower=follower.id,
                            safe_exact_only=True, commit=True, **kw)


class PredicateDeterminism(SafeExactFixture):
    def test_fixture_classifications_are_deterministic(self):
        d = generate_candidates(self.m, self.f)
        by = {c.master_symbol: c for c in d.candidates}
        self.assertEqual(by["XAUUSD"].classification, M.VERIFIED)
        self.assertEqual(by["EURUSD"].classification, M.UNVERIFIED)
        self.assertEqual(by["GBPUSD"].classification, M.UNVERIFIED)
        self.assertEqual(by["AUDUSD"].classification, M.AMBIGUOUS)
        self.assertEqual(by["USDCAD"].classification, M.AMBIGUOUS)
        self.assertEqual(by["XAGUSD"].classification, M.INELIGIBLE)
        self.assertEqual(by["NAS100"].classification, M.ABSENT)
        # SAFE-EXACT predicate: ONLY the clean warning-free UNVERIFIED EURUSD
        self.assertTrue(is_safe_exact(by["EURUSD"]))
        self.assertFalse(is_safe_exact(by["GBPUSD"]))   # soft contract_size warning
        self.assertFalse(is_safe_exact(by["AUDUSD"]))   # hard mismatch
        self.assertFalse(is_safe_exact(by["USDCAD"]))   # ambiguous
        self.assertFalse(is_safe_exact(by["XAGUSD"]))   # ineligible
        self.assertFalse(is_safe_exact(by["NAS100"]))   # absent
        self.assertFalse(is_safe_exact(by["XAUUSD"]))   # verified != safe-exact


class CommandSafety(SafeExactFixture):
    def test_01_dry_run_writes_nothing(self):
        call_command("refresh_instrument_mappings", master=self.m.id, follower=self.f.id,
                     safe_exact_only=True)  # no --commit
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_02_commit_without_reviewer_fails_closed(self):
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", master=self.m.id, follower=self.f.id,
                         safe_exact_only=True, commit=True, evidence="notion:x")
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_03_commit_without_evidence_fails_closed(self):
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", master=self.m.id, follower=self.f.id,
                         safe_exact_only=True, commit=True, reviewer="nuno")
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_04_safe_exact_persists_clean_candidate(self):
        self._commit_safe_exact(self.m, self.f)
        rows = list(InstrumentMapping.objects.all())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.master_symbol, r.follower_symbol), ("EURUSD", "EURUSD.std"))
        self.assertEqual(r.review_status, ReviewStatus.APPROVED)
        self.assertEqual(r.reviewed_by, "nuno-operator")
        self.assertIsNotNone(r.reviewed_at)
        self.assertEqual(r.review_evidence_ref, "notion:406d72ea-phaseB")

    def test_05_ambiguous_is_skipped(self):
        self._commit_safe_exact(self.m, self.f)
        self.assertFalse(InstrumentMapping.objects.filter(master_symbol__in=["AUDUSD", "USDCAD"]).exists())

    def test_06_ineligible_is_skipped(self):
        self._commit_safe_exact(self.m, self.f)
        self.assertFalse(InstrumentMapping.objects.filter(master_symbol="XAGUSD").exists())

    def test_07_absent_is_skipped(self):
        self._commit_safe_exact(self.m, self.f)
        self.assertFalse(InstrumentMapping.objects.filter(master_symbol="NAS100").exists())

    def test_08_stale_is_skipped(self):
        old = timezone.now() - timezone.timedelta(days=30)
        BrokerInstrument.objects.filter(account=self.m).update(synced_at=old)
        BrokerInstrument.objects.filter(account=self.f).update(synced_at=old)
        self._commit_safe_exact(self.m, self.f)
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_09_hard_dimension_mismatch_is_skipped(self):
        self._commit_safe_exact(self.m, self.f)
        self.assertFalse(InstrumentMapping.objects.filter(master_symbol="AUDUSD").exists())

    def test_10_material_warning_is_skipped(self):
        self._commit_safe_exact(self.m, self.f)
        self.assertFalse(InstrumentMapping.objects.filter(master_symbol="GBPUSD").exists())

    def test_11_self_pair_refused(self):
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", master=self.m.id, follower=self.m.id,
                         safe_exact_only=True, commit=True, reviewer="n", evidence="e")
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_12_cross_tenant_refused(self):
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", master=self.m.id, follower=self.x.id,
                         safe_exact_only=True, commit=True, reviewer="n", evidence="e")
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_13_idempotent(self):
        self._commit_safe_exact(self.m, self.f)
        n1 = InstrumentMapping.objects.count()
        self._commit_safe_exact(self.m, self.f)
        n2 = InstrumentMapping.objects.count()
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)  # no duplicate on re-run

    def test_14_direction_a_b_does_not_overwrite_b_a(self):
        self._commit_safe_exact(self.m, self.f)   # forward: EURUSD -> EURUSD.std
        self._commit_safe_exact(self.f, self.m)   # reverse: EURUSD.std -> EURUSD (+ its own safe-exacts)
        fwd = InstrumentMapping.objects.filter(
            master_account=self.m, follower_account=self.f,
            master_symbol="EURUSD", follower_symbol="EURUSD.std")
        rev = InstrumentMapping.objects.filter(
            master_account=self.f, follower_account=self.m,
            master_symbol="EURUSD.std", follower_symbol="EURUSD")
        self.assertEqual(fwd.count(), 1)
        self.assertEqual(rev.count(), 1)
        # the forward row survived the reverse commit unchanged (independent, additive directions)
        self.assertEqual(fwd.first().review_status, ReviewStatus.APPROVED)

    def test_15_existing_symbol_map_unchanged(self):
        before = (self.vmap.master_broker, self.vmap.master_symbol, self.vmap.follower_broker,
                  self.vmap.follower_symbol, self.vmap.verified, self.vmap.verified_at, self.vmap.verified_by_id)
        self._commit_safe_exact(self.m, self.f)
        self.assertEqual(CopierSymbolMap.objects.count(), 1)
        v = CopierSymbolMap.objects.get(pk=self.vmap.pk)
        after = (v.master_broker, v.master_symbol, v.follower_broker, v.follower_symbol,
                 v.verified, v.verified_at, v.verified_by_id)
        self.assertEqual(before, after)

    def test_16_instrument_mapping_absent_from_execution_path(self):
        backend = pathlib.Path(__file__).resolve().parents[1]
        allow = {
            "trade_copier/models.py", "trade_copier/matrix.py", "trade_copier/matrix_api.py",
            "trade_copier/management/commands/refresh_instrument_mappings.py",
            "trade_copier/tests_matrix.py", "trade_copier/tests_safe_exact_review.py",
            "trade_copier/admin.py",
        }
        offenders = []
        for p in backend.rglob("*.py"):
            rel = p.relative_to(backend).as_posix()
            if "/migrations/" in rel or rel in allow:
                continue
            try:
                if "InstrumentMapping" in p.read_text(encoding="utf-8"):
                    offenders.append(rel)
            except (OSError, UnicodeDecodeError):
                continue
        self.assertEqual(offenders, [], f"InstrumentMapping referenced outside the discovery/audit layer: {offenders}")

    def test_17_approved_row_is_not_execution_verified(self):
        self._commit_safe_exact(self.m, self.f)
        r = InstrumentMapping.objects.get(master_symbol="EURUSD")
        self.assertEqual(r.review_status, ReviewStatus.APPROVED)
        self.assertNotEqual(r.classification, M.VERIFIED)      # stays UNVERIFIED
        self.assertFalse(r.verified_via_symbol_map)            # never reflects a symbol-map verify

    def test_18_no_flag_or_job_or_order(self):
        before = ExecutionJob.objects.count()
        self._commit_safe_exact(self.m, self.f)
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertFalse(flags.copier_enabled())
        self.assertFalse(flags.copier_execute_enabled())
        self.assertFalse(flags.copier_observer_enabled())
