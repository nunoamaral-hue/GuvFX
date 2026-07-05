"""
GFX-PKT-SIGNAL-ACQUISITION-LISTENER-DRYRUN-VALIDATE — end-to-end fixture validation.

Runs the repo-built listener over the CERTIFIED Wayond corpus as fixtures (no Telegram)
and proves: normalisation + provider lookup work, dry-run writes nothing, fixture
replay flows into acquisition with the right outcomes, the watermark advances
correctly, certification stays clean, and no execution boundary is crossed.
"""

import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from signal_intake import certification as cert
from signal_intake.listener.fixtures import corpus_to_fixtures
from signal_intake.models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalProvider,
    SignalUpdate,
)

CHAT_ID = "1001"


class ListenerDryrunValidateTests(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id=CHAT_ID, chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED,
            acquisition_window_seconds=600,
        )
        self.entries = cert.load_corpus()  # the certified real corpus (source of truth)
        self.fixtures = corpus_to_fixtures(
            self.entries, chat_id=CHAT_ID, timestamp=timezone.now().timestamp())

    def _fixture_file(self):
        path = os.path.join(tempfile.mkdtemp(), "wayond_fixture.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"messages": self.fixtures}, fh)
        return path

    # 3. dry-run writes nothing --------------------------------------------
    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("run_wayond_listener", "--fixture", self._fixture_file(),
                     "--dry-run", stdout=out)
        self.assertEqual(AcquiredMessage.objects.count(), 0)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        self.assertEqual(SignalUpdate.objects.count(), 0)
        self.assertIn("DRY-RUN", out.getvalue())

    # 1,2,4,5. normalisation + lookup + replay + watermark -----------------
    def test_replay_flows_into_acquisition_with_expected_outcomes(self):
        call_command("run_wayond_listener", "--fixture", self._fixture_file(), stdout=StringIO())
        O = AcquiredMessage.Outcome
        n = len(self.fixtures)
        self.assertEqual(AcquiredMessage.objects.count(), n)   # every message acquired
        counts = {o: AcquiredMessage.objects.filter(outcome=o).count()
                  for o in (O.INTAKEN, O.UPDATE, O.QUARANTINED, O.STALE, O.DROPPED_NOT_ARMED)}
        self.assertEqual(counts[O.STALE], 0)                   # fresh dates → none stale
        self.assertEqual(counts[O.DROPPED_NOT_ARMED], 0)       # provider armed → none dropped

        exp_intaken = sum(1 for e in self.entries if e["expected_type"] == "ENTRY_SIGNAL")
        exp_update = sum(1 for e in self.entries if e["expected_type"] == "UPDATE")
        self.assertEqual(counts[O.INTAKEN], exp_intaken)       # every ENTRY_SIGNAL → intake
        self.assertEqual(counts[O.UPDATE], exp_update)         # every UPDATE → recorded
        self.assertEqual(counts[O.QUARANTINED], n - exp_intaken - exp_update)  # rest quarantined

        # watermark advanced to the last fixture message id
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id,
                         str(self.fixtures[-1]["message_id"]))
        # the edited entry surfaced to the human gate, flagged (never auto-traded)
        edited = PendingSignalApproval.objects.filter(source_edited=True)
        self.assertTrue(edited.exists())
        self.assertTrue(all(a.status == PendingSignalApproval.Status.PENDING_APPROVAL
                            for a in PendingSignalApproval.objects.all()))

    # 5. watermark: a second identical replay is idempotent (no re-write) ---
    def test_second_replay_is_idempotent(self):
        f = self._fixture_file()
        call_command("run_wayond_listener", "--fixture", f, stdout=StringIO())
        first = AcquiredMessage.objects.count()
        call_command("run_wayond_listener", "--fixture", f, stdout=StringIO())  # replay again
        self.assertEqual(AcquiredMessage.objects.count(), first)   # deduped, no new rows

    # 6. certification stays clean -----------------------------------------
    def test_certification_still_clean(self):
        report = cert.build_report()
        self.assertTrue(report["summary"]["certified"])
        self.assertEqual(report["summary"]["unsafe"], [])

    # 7. boundary (no execution / no order) --------------------------------
    def test_replay_creates_no_execution_job(self):
        from execution.models import ExecutionJob
        before = ExecutionJob.objects.count()
        call_command("run_wayond_listener", "--fixture", self._fixture_file(), stdout=StringIO())
        self.assertEqual(ExecutionJob.objects.count(), before)   # replay places no order/job
