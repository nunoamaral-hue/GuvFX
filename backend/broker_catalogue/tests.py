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
# Taurex catalogue artefact captured (credential-free) from the Account-36 Taurex-Demo environment (PR D). This is
# the exact SHA256 of the certified servers.dat; DEMO-ONLY scope (servers list is Taurex-Demo, never Taurex-Live).
TAUREX_SHA = "23fd33b87f3d6b628ba6cb457189e3cd8caa03617a381306e3873b2bba6fd876"


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
    # Build artefacts while the version is DRAFT (the model immutability guard forbids creating/updating an
    # artefact once the version is ACTIVE/RETIRED), then stamp the manifest + activate so resolution's integrity
    # re-verification passes.
    v = CatalogueVersion.objects.create(label="v1", status=CatalogueVersion.Status.DRAFT)
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
    # PR D — Taurex, DEMO-ONLY: the servers list is Taurex-Demo only (Taurex-Live is deliberately omitted so a Live
    # account never preseeds a demo-captured file; it falls back to native discovery until Live is separately certified).
    CatalogueArtefact.objects.create(
        version=v, broker_id="taurex", display_name="Taurex",
        servers=["Taurex-Demo"], artefact_ref="taurex/v1",
        sha256=TAUREX_SHA, size_bytes=47384, host_relpath="versions/v1/taurex/servers.dat",
        sanitisation_result="PASS")
    if active:
        v.status = CatalogueVersion.Status.ACTIVE
        v.manifest_algo = S.MANIFEST_ALGO
        v.manifest_sha256 = S.compute_manifest_sha(v)
        v.save(update_fields=["status", "manifest_algo", "manifest_sha256"])
    return v


def _approve(kind, ref, sha, sanitiser_passed=True, **meta):
    md = dict(meta)
    if sanitiser_passed:
        md.setdefault("sanitiser", {"passed": True, "version": "sanitiser_v1", "evidence_sha256": "e" * 64})
    return ArtefactApproval.objects.create(artefact_kind=kind, artefact_ref=ref, sha256=sha,
                                           status=ArtefactApproval.Status.APPROVED, metadata=md)


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

    def test_taurex_demo_resolves_but_live_does_not(self):
        # PR D DEMO-only scope: Taurex-Demo maps to the taurex artefact; Taurex-Live must NOT (it is deliberately
        # absent from the servers list so a Live account never preseeds the demo-captured file).
        _version()
        art = S.resolve_artefact_for_server("Taurex-Demo")
        self.assertIsNotNone(art)
        self.assertEqual(art.broker_id, "taurex")
        self.assertEqual(art.servers, ["Taurex-Demo"])
        self.assertIsNone(S.resolve_artefact_for_server("Taurex-Live"))

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

    def test_taurex_demo_approved_is_preseeded(self):
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)
        plan = S.resolve_broker_preseed(_account("Taurex-Demo"))
        self.assertTrue(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_SUPPORTED)
        self.assertEqual(plan.broker_id, "taurex")
        self.assertEqual(plan.sha256, TAUREX_SHA)
        self.assertEqual(plan.host_relpath, "versions/v1/taurex/servers.dat")

    def test_taurex_live_native_fallback_even_when_demo_approved(self):
        # A Taurex-Live account must fall back to native discovery (Demo-only scope), never preseed the demo artefact.
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)
        plan = S.resolve_broker_preseed(_account("Taurex-Live"))
        self.assertFalse(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_NATIVE_FALLBACK)
        self.assertTrue(plan.fallback_native)

    def test_wrong_broker_never_receives_taurex_bytes(self):
        # Cross-broker isolation: with Taurex approved, a Pepperstone account resolves to ITS own artefact (never
        # the Taurex SHA), and an unknown-broker account never carries a Taurex identity.
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)
        pep = S.resolve_broker_preseed(_account("PepperstoneUK-Demo"))
        self.assertTrue(pep.preseed)
        self.assertEqual(pep.broker_id, "pepperstone")
        self.assertNotEqual(pep.sha256, TAUREX_SHA)
        wims = S.resolve_broker_preseed(_account("WIMS-Demo"))
        self.assertFalse(wims.preseed)
        self.assertTrue(wims.fallback_native)
        self.assertNotEqual(getattr(wims, "broker_id", None), "taurex")


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
    def test_taurex_approved_artefact_is_copied_and_verified(self):
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)
        ex = FakeExecutor(ok=True)
        out = P.run_catalogue_preseed(_account("Taurex-Demo"), executor=ex, rdp_host="h")
        self.assertTrue(out["preseeded"])
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.calls[0][1], "taurex")
        self.assertEqual(ex.calls[0][2], TAUREX_SHA)
        self.assertEqual(ex.calls[0][3], "versions/v1/taurex/servers.dat")
        self.assertEqual(out["provenance"]["artefact_sha256"], TAUREX_SHA)

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

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_executor_unavailable_falls_back_native(self):
        # Fail-open: a supported+approved account with NO executor still never breaks provisioning.
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)
        out = P.run_catalogue_preseed(_account("Taurex-Demo"), executor=None, rdp_host="h")
        self.assertFalse(out["preseeded"])
        self.assertTrue(out["fallback_native"])

    @override_settings(HOSTED_BROKER_CATALOGUE_ENABLED="1", APPROVALS_ENABLED="1")
    def test_executor_raise_falls_back_native(self):
        # Fail-open: a host copy that RAISES is caught and degrades to native discovery (never propagates).
        _version()
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)

        class Boom:
            def preseed_broker_artefact(self, *a, **k):
                raise RuntimeError("host boom")

        out = P.run_catalogue_preseed(_account("Taurex-Demo"), executor=Boom(), rdp_host="h")
        self.assertFalse(out["preseeded"])
        self.assertTrue(out["fallback_native"])


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
        _approve("broker_servers_dat", "taurex/v1", TAUREX_SHA)   # PR D: version-wide gate needs Taurex approved too
        call_command("activate_catalogue_version", "--label", "v1")
        v = S.resolve_active_version()
        self.assertIsNotNone(v)
        self.assertEqual(len(v.manifest_sha256), 64)

    def test_activation_refused_when_only_taurex_missing(self):
        # Proves the version-wide gate: pepperstone + is6 approved but Taurex not -> activation must refuse.
        from django.core.management import call_command
        from django.core.management.base import CommandError
        _version(active=False)
        _approve("broker_servers_dat", "pepperstone/v1", PEP_SHA)
        _approve("broker_servers_dat", "is6/v1", IS6_SHA)
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "v1")
        self.assertIsNone(S.resolve_active_version())


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Catalogue Hardening (generic, for the 40-broker scale) — manifest-covers-bytes, immutability, sanitiser gate,
# approval byte-binding, server-id uniqueness, deterministic build, activation transaction, rollback.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
from django.core.exceptions import ValidationError                     # noqa: E402
from django.core.management import call_command                        # noqa: E402
from django.core.management.base import CommandError                   # noqa: E402
from broker_catalogue import sanitiser as SAN                          # noqa: E402

_HA = "a" * 64
_HB = "b" * 64
_HC = "c" * 64


def _san(passed=True):
    return {"passed": passed, "version": "sanitiser_v1", "evidence_sha256": "e" * 64}


def _draft(label, brokers):
    """A DRAFT version with the given [(broker, sha, [servers], size)] artefacts (created while DRAFT)."""
    v = CatalogueVersion.objects.create(label=label, status=CatalogueVersion.Status.DRAFT)
    for broker, sha, servers, size in brokers:
        CatalogueArtefact.objects.create(
            version=v, broker_id=broker, display_name=broker.title(), servers=servers,
            artefact_ref=f"{broker}/{label}", sha256=sha, size_bytes=size,
            host_relpath=f"versions/{label}/{broker}/servers.dat", sanitisation_result="PASS")
    return v


def _approve_full(broker, label, sha, servers, size, sanitiser_passed=True):
    ArtefactApproval.objects.create(
        artefact_kind="broker_servers_dat", artefact_ref=f"{broker}/{label}", sha256=sha,
        status=ArtefactApproval.Status.APPROVED,
        metadata={"broker": broker.title(), "servers_intended": servers, "size_bytes": size,
                  "sanitiser": _san(sanitiser_passed)})


@override_settings(APPROVALS_ENABLED="1")
class ManifestIntegrityTests(TestCase):
    def test_strong_manifest_covers_servers_and_size(self):
        v = _draft("m1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        base = S.compute_manifest_sha(v)
        art = v.artefacts.get(broker_id="taurex")
        art.servers = ["Taurex-Demo", "Taurex-Live"]; art.save()      # DRAFT -> mutable
        self.assertNotEqual(base, S.compute_manifest_sha(v))          # servers change -> manifest change
        art.servers = ["Taurex-Demo"]; art.size_bytes = 200; art.save()
        self.assertNotEqual(base, S.compute_manifest_sha(v))          # size change -> manifest change

    def test_byte_mutation_after_activation_detected(self):
        v = _draft("m2", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "m2", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "m2")
        self.assertTrue(S.verify_version_integrity(S.resolve_active_version()))
        # Simulate a post-activation DB tamper (bypass the model save-guard with a raw UPDATE).
        CatalogueArtefact.objects.filter(version__label="m2", broker_id="taurex").update(sha256=_HB)
        v = CatalogueVersion.objects.get(label="m2")
        self.assertFalse(S.verify_version_integrity(v))
        plan = S.resolve_broker_preseed(_account("Taurex-Demo"))
        self.assertFalse(plan.preseed)
        self.assertEqual(plan.reason_code, S.PRESEED_MANIFEST_INVALID)

    def test_manifest_field_tamper_detected(self):
        v = _draft("m3", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "m3", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "m3")
        CatalogueVersion.objects.filter(label="m3").update(manifest_sha256=_HC)
        self.assertFalse(S.verify_version_integrity(CatalogueVersion.objects.get(label="m3")))

    def test_deterministic_and_order_independent(self):
        # The strong manifest does not include the version label and sorts artefacts, so the SAME broker
        # set/bytes/servers/size yields the SAME manifest regardless of insertion order.
        a = _draft("da", [("aa", _HA, ["AA-Demo"], 10), ("bb", _HB, ["BB-Demo"], 20)])
        b = _draft("db", [("bb", _HB, ["BB-Demo"], 20), ("aa", _HA, ["AA-Demo"], 10)])   # inserted reversed
        self.assertEqual(S.compute_manifest_sha(a), S.compute_manifest_sha(a))            # stable
        self.assertEqual(S.compute_manifest_sha(a), S.compute_manifest_sha(b))            # order-independent


@override_settings(APPROVALS_ENABLED="1")
class ImmutabilityTests(TestCase):
    def test_artefact_cannot_be_created_or_changed_on_active_version(self):
        v = _draft("i1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "i1", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "i1")
        v = CatalogueVersion.objects.get(label="i1")
        with self.assertRaises(ValidationError):                       # cannot add an artefact to an ACTIVE version
            CatalogueArtefact.objects.create(
                version=v, broker_id="new", display_name="New", servers=["New-Demo"],
                artefact_ref="new/i1", sha256=_HB, size_bytes=1, host_relpath="versions/i1/new/servers.dat")
        art = v.artefacts.get(broker_id="taurex")
        art.servers = ["Taurex-Demo", "Taurex-Live"]
        with self.assertRaises(ValidationError):                       # cannot mutate an artefact on an ACTIVE version
            art.save()


@override_settings(APPROVALS_ENABLED="1")
class ActivationGateHardeningTests(TestCase):
    def test_sanitiser_fail_blocks_activation(self):
        _draft("s1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "s1", _HA, ["Taurex-Demo"], 100, sanitiser_passed=False)
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "s1")
        self.assertIsNone(S.resolve_active_version())

    def test_missing_sanitiser_blocks_activation(self):
        _draft("s2", [("taurex", _HA, ["Taurex-Demo"], 100)])
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="taurex/s2",
                                        sha256=_HA, status=ArtefactApproval.Status.APPROVED, metadata={})
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "s2")

    def test_wrong_size_blocks_activation(self):
        _draft("z1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "z1", _HA, ["Taurex-Demo"], 999)       # approval size != artefact size
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "z1")

    def test_server_collision_blocks_activation(self):
        # Two brokers claim the same server name -> ambiguous routing -> activation refused (Gate 3).
        v = CatalogueVersion.objects.create(label="c1", status=CatalogueVersion.Status.DRAFT)
        for b, sha in (("x", _HA), ("y", _HB)):
            CatalogueArtefact.objects.create(version=v, broker_id=b, display_name=b, servers=["Same-Demo"],
                                             artefact_ref=f"{b}/c1", sha256=sha, size_bytes=1,
                                             host_relpath=f"versions/c1/{b}/servers.dat")
        _approve_full("x", "c1", _HA, ["Same-Demo"], 1)
        _approve_full("y", "c1", _HB, ["Same-Demo"], 1)
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "c1")

    def test_carried_over_broker_sha_change_blocked(self):
        _draft("v1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "v1", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "v1")
        _draft("v2", [("taurex", _HB, ["Taurex-Demo"], 100)])          # same broker, DIFFERENT sha
        _approve_full("taurex", "v2", _HB, ["Taurex-Demo"], 100)
        with self.assertRaises(CommandError):                          # silent byte change refused
            call_command("activate_catalogue_version", "--label", "v2")
        self.assertEqual(S.resolve_active_version().label, "v1")       # previous ACTIVE preserved
        call_command("activate_catalogue_version", "--label", "v2", "--allow-byte-change")   # explicit override
        self.assertEqual(S.resolve_active_version().label, "v2")

    def test_partial_activation_preserves_previous_active(self):
        _draft("p1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "p1", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "p1")
        # A new DRAFT that will FAIL activation (missing approval) must not disturb the ACTIVE version.
        _draft("p2", [("taurex", _HB, ["Taurex-Demo"], 100)])
        with self.assertRaises(CommandError):
            call_command("activate_catalogue_version", "--label", "p2")
        self.assertEqual(S.resolve_active_version().label, "p1")


@override_settings(APPROVALS_ENABLED="1")
class HostAttestationTests(TestCase):
    def _arm(self, verified):
        from broker_catalogue.management.commands import activate_catalogue_version as A

        class FakeAttest:
            def attest_broker_artefact(self, host_relpath, expected_sha256):
                return {"ok": verified is not None, "verified_sha256": verified or ""}
        return mock.patch.object(A, "_attest_executor", lambda: FakeAttest())

    def test_attest_host_pass(self):
        _draft("a1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "a1", _HA, ["Taurex-Demo"], 100)
        with self._arm(_HA):
            call_command("activate_catalogue_version", "--label", "a1", "--attest-host")
        self.assertEqual(S.resolve_active_version().label, "a1")

    def test_attest_host_sha_mismatch_blocks(self):
        _draft("a2", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "a2", _HA, ["Taurex-Demo"], 100)
        with self._arm(_HB):                                            # host bytes read back a different SHA
            with self.assertRaises(CommandError):
                call_command("activate_catalogue_version", "--label", "a2", "--attest-host")
        self.assertIsNone(S.resolve_active_version())

    def test_attest_host_missing_bytes_blocks(self):
        _draft("a3", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "a3", _HA, ["Taurex-Demo"], 100)
        with self._arm(None):                                          # host file missing -> ok False
            with self.assertRaises(CommandError):
                call_command("activate_catalogue_version", "--label", "a3", "--attest-host")

    def test_attest_host_without_transport_fails_closed(self):
        _draft("a4", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "a4", _HA, ["Taurex-Demo"], 100)
        with self.assertRaises(CommandError):                          # no attestation transport configured
            call_command("activate_catalogue_version", "--label", "a4", "--attest-host")


@override_settings(APPROVALS_ENABLED="1")
class RollbackTests(TestCase):
    def test_rollback_restores_previous_active(self):
        _draft("v1", [("taurex", _HA, ["Taurex-Demo"], 100)])
        _approve_full("taurex", "v1", _HA, ["Taurex-Demo"], 100)
        call_command("activate_catalogue_version", "--label", "v1")
        _draft("v2", [("taurex", _HA, ["Taurex-Demo"], 100), ("is6", _HB, ["IS6Technologies-Demo"], 50)])
        _approve_full("taurex", "v2", _HA, ["Taurex-Demo"], 100)
        _approve_full("is6", "v2", _HB, ["IS6Technologies-Demo"], 50)
        call_command("activate_catalogue_version", "--label", "v2")
        self.assertEqual(S.resolve_active_version().label, "v2")
        call_command("rollback_catalogue_version", "--to", "v1")
        self.assertEqual(S.resolve_active_version().label, "v1")

    def test_rollback_refuses_draft_target(self):
        # Rollback re-activates a RETIRED version only; a DRAFT target is refused (fail-closed).
        _draft("v3", [("taurex", _HA, ["Taurex-Demo"], 100)])
        with self.assertRaises(CommandError):
            call_command("rollback_catalogue_version", "--to", "v3")


class BuildHardeningTests(TestCase):
    def test_build_binds_single_approved_over_pending_duplicate(self):
        # Register an APPROVED SHA_A and a PENDING SHA_B for the same ref -> build binds SHA_A (no last-write-wins).
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="taurex/b1", sha256=_HA,
                                        status=ArtefactApproval.Status.APPROVED,
                                        metadata={"servers_intended": ["Taurex-Demo"], "size_bytes": 1,
                                                  "sanitiser": _san()})
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="taurex/b1", sha256=_HB,
                                        status=ArtefactApproval.Status.PENDING, metadata={})
        call_command("build_catalogue_version", "--label", "b1")
        art = CatalogueArtefact.objects.get(version__label="b1", broker_id="taurex")
        self.assertEqual(art.sha256, _HA)

    def test_two_approved_for_same_ref_is_db_prevented(self):
        # The approvals partial-unique constraint already forbids two APPROVED rows per (kind, ref) — the build's
        # >1-approved guard is defence-in-depth for a state the DB does not allow to exist.
        from django.db import IntegrityError, transaction
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="taurex/b2", sha256=_HA,
                                        status=ArtefactApproval.Status.APPROVED,
                                        metadata={"servers_intended": ["Taurex-Demo"], "sanitiser": _san()})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="taurex/b2",
                                                sha256=_HB, status=ArtefactApproval.Status.APPROVED, metadata={})

    def test_build_refuses_cross_broker_server_collision(self):
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="x/b3", sha256=_HA,
                                        status=ArtefactApproval.Status.APPROVED,
                                        metadata={"servers_intended": ["Same-Demo"], "sanitiser": _san()})
        ArtefactApproval.objects.create(artefact_kind="broker_servers_dat", artefact_ref="y/b3", sha256=_HB,
                                        status=ArtefactApproval.Status.APPROVED,
                                        metadata={"servers_intended": ["Same-Demo"], "sanitiser": _san()})
        with self.assertRaises(CommandError):
            call_command("build_catalogue_version", "--label", "b3")


class SanitiserTests(TestCase):
    def test_clean_bytes_pass(self):
        v = SAN.scan_artefact(b"PepperstoneUK-Demo\x00access-server-1.example\x00", identity_terms=["830227146"])
        self.assertTrue(v["passed"])
        self.assertEqual(v["identity_hits"], [])
        self.assertEqual(len(v["sha256"]), 64)
        self.assertEqual(len(v["evidence_sha256"]), 64)

    def test_login_ascii_detected(self):
        v = SAN.scan_artefact(b"junk-830227146-more", identity_terms=["830227146"])
        self.assertFalse(v["passed"])
        self.assertTrue(any(h["encoding"] == "ascii" for h in v["identity_hits"]))

    def test_login_int32le_detected(self):
        payload = b"AAAA" + (830227146).to_bytes(4, "little") + b"BBBB"
        v = SAN.scan_artefact(payload, identity_terms=["830227146"])
        self.assertFalse(v["passed"])

    def test_email_utf16_detected(self):
        v = SAN.scan_artefact("x support@guvfx.com y".encode("utf-16-le"), identity_terms=["support@guvfx.com"])
        self.assertFalse(v["passed"])

    def test_verdict_is_bounded_language(self):
        v = SAN.scan_artefact(b"clean", identity_terms=[])
        self.assertIn("does NOT prove", v["note"])
