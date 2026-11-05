"""Stream A / ADR-0078 — READ-ONLY catalogue-readiness evaluator tests (every reason code + read-only proof)."""
from __future__ import annotations

from django.test import TestCase

from approvals.models import ArtefactApproval

from . import readiness as R
from .models import CatalogueArtefact, CatalogueCaptureCandidate, CatalogueVersion


def _approved(ref, sha, metadata=None):
    return ArtefactApproval.objects.create(
        artefact_kind=R.ARTEFACT_KIND, artefact_ref=ref, sha256=sha,
        status=ArtefactApproval.Status.APPROVED, metadata=metadata or {})


def _version(label, status):
    return CatalogueVersion.objects.create(label=label, status=status)


def _artefact(version, broker, sha, *, servers, sanitiser="PASS"):
    return CatalogueArtefact.objects.create(
        version=version, broker_id=broker, display_name=broker.title(), servers=servers,
        artefact_ref=f"{broker}/v1", sha256=sha, size_bytes=100,
        host_relpath=f"versions/{version.label}/{broker}/servers.dat", sanitisation_result=sanitiser)


def _rc(rows, broker):
    return next(r for r in rows if r.broker_id == broker).reason_code


class ReadinessEvaluatorTests(TestCase):
    def test_eligible_approved_pass_not_active(self):
        v = _version("vdraft", CatalogueVersion.Status.DRAFT)
        _artefact(v, "alpha", "a" * 64, servers=["Alpha-Live"], sanitiser="PASS")
        _approved("alpha/v1", "a" * 64)
        r = R.evaluate_catalogue_readiness(["alpha"])[0]
        self.assertTrue(r.eligible)
        self.assertEqual(r.reason_code, R.RC_ELIGIBLE)
        self.assertEqual(r.sha256_suffix, ("a" * 64)[-12:])

    def test_already_active(self):
        # Build in DRAFT (the ORM guard forbids creating an artefact directly in a non-DRAFT version), then
        # activate the version via a version-row update (the trigger guards artefact rows, not version rows).
        v = _version("vactive", CatalogueVersion.Status.DRAFT)
        _artefact(v, "beta", "b" * 64, servers=["Beta-Live"])
        _approved("beta/v1", "b" * 64)
        CatalogueVersion.objects.filter(pk=v.pk).update(status=CatalogueVersion.Status.ACTIVE)
        rows = R.evaluate_catalogue_readiness(["beta"])
        self.assertEqual(_rc(rows, "beta"), R.RC_ALREADY_ACTIVE)
        self.assertFalse(rows[0].eligible)
        self.assertTrue(rows[0].in_active_catalogue)

    def test_sanitiser_not_pass(self):
        v = _version("vdraft2", CatalogueVersion.Status.DRAFT)
        _artefact(v, "gamma", "c" * 64, servers=["Gamma-Live"], sanitiser="FAIL")
        _approved("gamma/v1", "c" * 64)
        rows = R.evaluate_catalogue_readiness(["gamma"])
        self.assertEqual(_rc(rows, "gamma"), R.RC_SANITISER_NOT_PASS)
        self.assertFalse(rows[0].eligible)

    def test_no_approved_artefact_when_only_pending(self):
        ArtefactApproval.objects.create(artefact_kind=R.ARTEFACT_KIND, artefact_ref="delta/v1",
                                        sha256="d" * 64, status=ArtefactApproval.Status.PENDING)
        rows = R.evaluate_catalogue_readiness(["delta"])
        self.assertEqual(_rc(rows, "delta"), R.RC_NO_APPROVED_ARTEFACT)
        self.assertFalse(rows[0].eligible)

    def test_needs_capture(self):
        CatalogueCaptureCandidate.objects.create(
            broker_id="dominion_markets", display_name="Dominion Markets",
            server_identity_hint="DominionMarkets-Live",
            capture_status=CatalogueCaptureCandidate.Status.NEEDS_CAPTURE)
        rows = R.evaluate_catalogue_readiness(["dominion_markets"])
        self.assertEqual(_rc(rows, "dominion_markets"), R.RC_NEEDS_CAPTURE)
        self.assertFalse(rows[0].eligible)

    def test_not_captured_unknown_broker(self):
        rows = R.evaluate_catalogue_readiness(["ghost_broker"])
        self.assertEqual(_rc(rows, "ghost_broker"), R.RC_NOT_CAPTURED)

    def test_duplicate_server_conflict_flips_both(self):
        v = _version("vdup", CatalogueVersion.Status.DRAFT)
        _artefact(v, "ex", "e" * 64, servers=["Shared-Live"])
        _artefact(v, "fx", "f" * 64, servers=["Shared-Live"])  # same server name!
        _approved("ex/v1", "e" * 64)
        _approved("fx/v1", "f" * 64)
        rows = R.evaluate_catalogue_readiness(["ex", "fx"])
        self.assertEqual(_rc(rows, "ex"), R.RC_DUPLICATE_SERVER_CONFLICT)
        self.assertEqual(_rc(rows, "fx"), R.RC_DUPLICATE_SERVER_CONFLICT)
        self.assertFalse(any(r.eligible for r in rows))

    def test_eligible_from_approval_metadata_without_built_artefact(self):
        # An APPROVED approval with a PASS sanitiser in metadata but no built CatalogueArtefact is still eligible
        # for a future additive build (approved + pass).
        _approved("hx/v1", "0" * 64, metadata={"sanitisation": "PASS", "servers": ["Hx-Live"]})
        rows = R.evaluate_catalogue_readiness(["hx"])
        self.assertEqual(_rc(rows, "hx"), R.RC_ELIGIBLE)

    def test_pending_approval_is_never_eligible(self):
        # Adversarial: a PENDING approval (even with PASS metadata) must NOT be reported eligible.
        ArtefactApproval.objects.create(artefact_kind=R.ARTEFACT_KIND, artefact_ref="ix/v1", sha256="1" * 64,
                                        status=ArtefactApproval.Status.PENDING,
                                        metadata={"sanitisation": "PASS"})
        rows = R.evaluate_catalogue_readiness(["ix"])
        self.assertEqual(_rc(rows, "ix"), R.RC_NO_APPROVED_ARTEFACT)
        self.assertFalse(rows[0].eligible)

    def test_evaluator_is_read_only(self):
        v = _version("vro", CatalogueVersion.Status.DRAFT)
        _artefact(v, "alpha", "a" * 64, servers=["Alpha-Live"])
        _approved("alpha/v1", "a" * 64)
        CatalogueCaptureCandidate.objects.create(broker_id="dominion_markets", display_name="Dominion Markets")
        before = (CatalogueVersion.objects.count(), CatalogueArtefact.objects.count(),
                  ArtefactApproval.objects.count(), CatalogueCaptureCandidate.objects.count())
        R.evaluate_catalogue_readiness(["alpha", "dominion_markets", "ghost"])
        R.evaluate_catalogue_readiness()  # whole-catalogue path too
        after = (CatalogueVersion.objects.count(), CatalogueArtefact.objects.count(),
                 ArtefactApproval.objects.count(), CatalogueCaptureCandidate.objects.count())
        self.assertEqual(before, after)  # nothing created/deleted by evaluation

    def test_all_reason_codes_are_stable_strings(self):
        self.assertEqual(set(R.ALL_REASON_CODES), {
            R.RC_ELIGIBLE, R.RC_ALREADY_ACTIVE, R.RC_SANITISER_NOT_PASS, R.RC_NO_APPROVED_ARTEFACT,
            R.RC_NEEDS_CAPTURE, R.RC_NOT_CAPTURED, R.RC_DUPLICATE_SERVER_CONFLICT})
