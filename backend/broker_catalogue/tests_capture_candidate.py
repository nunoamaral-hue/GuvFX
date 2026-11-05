"""Stream A / ADR-0078 — Dominion NEEDS_CAPTURE candidate workflow (scaffolding only; DARK/default-OFF)."""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from approvals.models import ArtefactApproval

from .capture import register_needs_capture
from .models import CatalogueArtefact, CatalogueCaptureCandidate, CatalogueVersion

ARMED = dict(BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED="1")
DISARMED = dict(BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED="0")


class CaptureCandidateTests(TestCase):
    def _no_catalogue_state(self):
        # The candidate workflow NEVER creates an artefact / approval / version.
        self.assertEqual(CatalogueArtefact.objects.count(), 0)
        self.assertEqual(ArtefactApproval.objects.count(), 0)
        self.assertEqual(CatalogueVersion.objects.count(), 0)

    @override_settings(**DISARMED)
    def test_dry_run_default_writes_nothing(self):
        res = register_needs_capture(broker_id="dominion_markets", display_name="Dominion Markets",
                                     server_identity_hint="DominionMarkets-Live")  # commit defaults False
        self.assertFalse(res["written"])
        self.assertTrue(res["would_write"])
        self.assertEqual(CatalogueCaptureCandidate.objects.count(), 0)
        self._no_catalogue_state()

    @override_settings(**DISARMED)
    def test_commit_without_flag_writes_nothing(self):
        res = register_needs_capture(broker_id="dominion_markets", display_name="Dominion Markets", commit=True)
        self.assertFalse(res["written"])
        self.assertFalse(res["flag_armed"])
        self.assertEqual(CatalogueCaptureCandidate.objects.count(), 0)
        self._no_catalogue_state()

    @override_settings(**ARMED)
    def test_commit_with_flag_creates_needs_capture_only(self):
        res = register_needs_capture(broker_id="dominion_markets", display_name="Dominion Markets",
                                     server_identity_hint="DominionMarkets-Live", commit=True)
        self.assertTrue(res["written"] and res["created"])
        row = CatalogueCaptureCandidate.objects.get(broker_id="dominion_markets")
        self.assertEqual(row.capture_status, CatalogueCaptureCandidate.Status.NEEDS_CAPTURE)
        self.assertEqual(row.server_identity_hint, "DominionMarkets-Live")
        self._no_catalogue_state()  # still no artefact/approval/version — scaffolding only

    @override_settings(**ARMED)
    def test_re_register_refreshes_but_never_regresses_status(self):
        register_needs_capture(broker_id="dominion_markets", display_name="Dominion Markets", commit=True)
        # A separate authorised step later advances it to CAPTURED:
        CatalogueCaptureCandidate.objects.filter(broker_id="dominion_markets").update(
            capture_status=CatalogueCaptureCandidate.Status.CAPTURED)
        # Re-running register must refresh descriptive fields but NOT regress CAPTURED -> NEEDS_CAPTURE.
        res = register_needs_capture(broker_id="dominion_markets", display_name="Dominion Markets Ltd",
                                     commit=True)
        self.assertTrue(res["written"] and not res["created"])
        row = CatalogueCaptureCandidate.objects.get(broker_id="dominion_markets")
        self.assertEqual(row.capture_status, CatalogueCaptureCandidate.Status.CAPTURED)
        self.assertEqual(row.display_name, "Dominion Markets Ltd")

    def test_blank_broker_id_rejected(self):
        with self.assertRaises(ValueError):
            register_needs_capture(broker_id="   ", display_name="x", commit=True)

    @override_settings(**DISARMED)
    def test_command_dry_run_reports_and_writes_nothing(self):
        out = StringIO()
        call_command("register_capture_candidate", "--broker-id", "dominion_markets",
                     "--display-name", "Dominion Markets", "--server-hint", "DominionMarkets-Live",
                     "--commit", stdout=out)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertEqual(CatalogueCaptureCandidate.objects.count(), 0)
        self._no_catalogue_state()

    @override_settings(**ARMED)
    def test_command_commit_with_flag_writes_candidate(self):
        out = StringIO()
        call_command("register_capture_candidate", "--broker-id", "dominion_markets",
                     "--display-name", "Dominion Markets", "--commit", stdout=out)
        self.assertIn("CREATED", out.getvalue())
        self.assertTrue(CatalogueCaptureCandidate.objects.filter(
            broker_id="dominion_markets",
            capture_status=CatalogueCaptureCandidate.Status.NEEDS_CAPTURE).exists())
        self._no_catalogue_state()

    def test_report_command_runs_read_only(self):
        CatalogueCaptureCandidate.objects.create(broker_id="dominion_markets", display_name="Dominion Markets")
        out = StringIO()
        call_command("report_catalogue_readiness", "--broker", "dominion_markets", "--json", stdout=out)
        self.assertIn("NEEDS_CAPTURE", out.getvalue())
        self._no_catalogue_state()
