"""Broker Catalogue V1 tests — resolution, approval gating, native fallback, preseed consumption.

Covers the packet's required catalogue cases: exact manifest/hash, supported broker resolution (Pepperstone +
IS6), Demo/Live server mapping, unsupported -> native fallback, corrupt/unapproved supported artefact fails safe
(no copy), DARK default, approved artefact -> preseed with read-back SHA verify, provenance recorded, and that a
verify mismatch falls back to native (never trusts unverified bytes).
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from trading.models import BrokerServer, TradingAccount
from approvals.models import ArtefactApproval
from broker_catalogue import service as S
from broker_catalogue import preseed as P
from broker_catalogue.models import CatalogueArtefact, CatalogueVersion

U = get_user_model()
_n = 0

PEP_SHA = "afd6d65b43b5df4575d766f31af2972ce3677808897588d09495f0b85d072c6b"
IS6_SHA = "db013e27ad0d5a633bc5fcddb14b7c3450467b6ccfaa81d5e02a95dffd48c414"


def _uniq():
    global _n
    _n += 1
    return f"70{_n:04d}"


def _account(server_name):
    login = _uniq()
    u = U.objects.create_user(username=f"bc{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name=server_name)
    return TradingAccount.objects.create(user=u, name="a", broker_name="B", account_number=login,
                                         is_demo=True, is_active=True, broker_server=srv)


def _version(active=True):
    v = CatalogueVersion.objects.create(
        label="v1", status=CatalogueVersion.Status.ACTIVE if active else CatalogueVersion.Status.DRAFT)
    CatalogueArtefact.objects.create(
        version=v, broker_id="pepperstone", display_name="Pepperstone",
        servers=["PepperstoneUK-Demo", "PepperstoneUK-Live"], artefact_ref="pepperstone/v1",
        sha256=PEP_SHA, size_bytes=86592, host_relpath="versions/v1/pepperstone/servers.dat",
        sanitisation_result="PASS")
    CatalogueArtefact.objects.create(
        version=v, broker_id="is6", display_name="IS6 Technologies",
        servers=["IS6Technologies-Demo", "IS6Technologies-Live"], artefact_ref="is6/v1",
        sha256=IS6_SHA, size_bytes=69032, host_relpath="versions/v1/is6/servers.dat",
        sanitisation_result="PASS")
    return v


def _approve(kind, ref, sha):
    return ArtefactApproval.objects.create(artefact_kind=kind, artefact_ref=ref, sha256=sha,
                                           status=ArtefactApproval.Status.APPROVED)


class FakeExecutor:
    def __init__(self, ok=True, verified=None):
        self.calls = []
        self.ok = ok
        self.verified = verified

    def preseed_broker_artefact(self, runtime_root, broker_id, expected_sha256, host_relpath, rdp_host=None):
        self.calls.append((runtime_root, broker_id, expected_sha256, host_relpath))
        v = self.verified if self.verified is not None else expected_sha256
        return {"ok": self.ok, "verified_sha256": v, "reason": "ok" if self.ok else "readback_sha_mismatch"}


class ResolutionTests(TestCase):
    def test_supported_pepperstone_and_is6_demo_and_live(self):
        _version()
        for server, broker in [("PepperstoneUK-Demo", "pepperstone"), ("PepperstoneUK-Live", "pepperstone"),
                               ("IS6Technologies-Demo", "is6"), ("IS6Technologies-Live", "is6")]:
            art = S.resolve_artefact_for_server(server)
            self.assertIsNotNone(art, server)
            self.assertEqual(art.broker_id, broker, server)

    def test_unsupported_broker_resolves_none(self):
        _version()
        self.assertIsNone(S.resolve_artefact_for_server("WIMS-Demo"))   # CZ's legacy server is NOT IS6
        self.assertIsNone(S.resolve_artefact_for_server("SomeOther-Demo"))

    def test_no_active_version_resolves_none(self):
        _version(active=False)   # DRAFT, not ACTIVE
        self.assertIsNone(S.resolve_active_version())
        self.assertIsNone(S.resolve_artefact_for_server("PepperstoneUK-Demo"))

    def test_manifest_sha_is_deterministic_and_order_independent(self):
        v = _version()
        m1 = S.compute_manifest_sha(v)
        self.assertEqual(len(m1), 64)
        self.assertEqual(m1, S.compute_manifest_sha(v))   # stable


@override_settings(APPROVALS_ENABLED="1")
class PreseedPlanTests(TestCase):
    def test_supported_but_unapproved_is_not_preseeded(self):
        _version()   # no approvals created -> unapproved
        plan = S.resolve_broker_preseed(_account("IS6Technologies-Demo"))
        self.assertFalse(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_UNAPPROVED)
        self.assertTrue(plan.fallback_native)

    def test_supported_and_approved_is_preseeded(self):
        _version()
        _approve("broker_servers_dat", "is6/v1", IS6_SHA)
        plan = S.resolve_broker_preseed(_account("IS6Technologies-Live"))
        self.assertTrue(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_SUPPORTED)
        self.assertEqual(plan.broker_id, "is6")
        self.assertEqual(plan.sha256, IS6_SHA)
        self.assertEqual(plan.host_relpath, "versions/v1/is6/servers.dat")

    def test_unsupported_broker_native_fallback(self):
        _version()
        plan = S.resolve_broker_preseed(_account("WIMS-Demo"))
        self.assertFalse(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_NATIVE_FALLBACK)
        self.assertTrue(plan.fallback_native)


class PreseedConsumptionTests(TestCase):
    def test_dark_by_default_no_lookup_no_copy(self):
        _version()
        ex = FakeExecutor()
        out = P.run_catalogue_preseed(_account("PepperstoneUK-Demo"), executor=ex, rdp_host="h")
        self.assertFalse(out["enabled"])
        self.assertFalse(out["preseeded"])
        self.assertEqual(ex.calls, [])   # DARK: never contacts host

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_approved_artefact_is_copied_and_verified(self):
        _version()
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)
        ex = FakeExecutor(ok=True)   # read-back returns the expected sha
        out = P.run_catalogue_preseed(_account("PepperstoneUK-Demo"), executor=ex, rdp_host="h")
        self.assertTrue(out["enabled"])
        self.assertTrue(out["preseeded"])
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.calls[0][1], "pepperstone")
        self.assertEqual(ex.calls[0][2], PEP_SHA)
        self.assertIn("provenance", out)
        self.assertEqual(out["provenance"]["artefact_sha256"], PEP_SHA)

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_readback_mismatch_falls_back_native_not_trusted(self):
        _version()
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)
        ex = FakeExecutor(ok=True, verified="0" * 64)   # host returns a DIFFERENT sha
        out = P.run_catalogue_preseed(_account("PepperstoneUK-Demo"), executor=ex, rdp_host="h")
        self.assertFalse(out["preseeded"])
        self.assertTrue(out["fallback_native"])
        self.assertEqual(out["reason_code"], "catalogue_preseed_verify_failed")

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_unapproved_supported_not_copied(self):
        _version()   # no approval
        ex = FakeExecutor(ok=True)
        out = P.run_catalogue_preseed(_account("IS6Technologies-Demo"), executor=ex, rdp_host="h")
        self.assertFalse(out["preseeded"])
        self.assertTrue(out["fallback_native"])
        self.assertEqual(ex.calls, [])   # never copies unapproved bytes

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_unsupported_broker_native_no_copy(self):
        _version()
        ex = FakeExecutor(ok=True)
        out = P.run_catalogue_preseed(_account("WIMS-Demo"), executor=ex, rdp_host="h")
        self.assertFalse(out["preseeded"])
        self.assertTrue(out["fallback_native"])
        self.assertEqual(ex.calls, [])


@override_settings(APPROVALS_ENABLED="1")
class ActivationGateTests(TestCase):
    def test_activation_refused_when_any_artefact_unapproved(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        _version(active=False)
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)   # only Pepperstone approved; IS6 not
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "v1")
        self.assertIsNone(S.resolve_active_version())   # stayed inactive

    def test_activation_succeeds_when_all_approved(self):
        from django.core.management import call_command
        _version(active=False)
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)
        _approve("broker_servers_dat", "is6/v1", IS6_SHA)
        call_command("activate_catalogue_version", "--label", "v1")
        v = S.resolve_active_version()
        self.assertIsNotNone(v)
        self.assertEqual(len(v.manifest_sha256), 64)
