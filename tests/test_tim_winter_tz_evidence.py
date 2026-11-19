"""Evidence-contract tests for the VERIFIED winter broker-timezone observation.

Validates the recorded value-blind observation
(``tests/fixtures/market_data/tim/winter_tz_observation_2026-11-19.json``) against the
winter-timezone evidence contract, and that the evidence document supersedes — without
erasing — the earlier NOT_VERIFIED verdict while keeping the summer observation separate.

The offset/consistency/residual/value-blind checks reuse the *real* probe contract code
(``scripts/tim_winter_tz_probe.py``) rather than reimplementing it, so the recorded
evidence is held to exactly the invariants the probe enforces on its own output.

Standard library only (runs in the CI governance lane, no third-party deps).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import tim_winter_tz_probe as probe  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "market_data" / "tim" / "winter_tz_observation_2026-11-19.json"
WINTER_DOC = REPO / "docs" / "evidence" / "broker_timezone_evidence_v2_winter.md"

WINTER_OFFSET_S = 7200
SUMMER_OFFSET_S = 10800


class WinterTzEvidenceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.p = cls.rec["probe"]

    # --- DEMO-only --------------------------------------------------------
    def test_demo_only(self):
        self.assertEqual(self.p["demo_classification"], "DEMO")
        self.assertEqual(self.p["assessment_status"], "VERIFIED")
        self.assertEqual(self.p["broker_server_identifier"], "TradersWay-Demo")
        self.assertEqual(self.p["broker_server_identifier"], self.rec["mapping"]["server"])
        self.assertEqual(self.rec["verdict"], "TIM_WINTER_TIMEZONE_VERIFIED")

    # --- exactly three completed M1 bars (forming bar excluded) -----------
    def test_exactly_three_completed_bars(self):
        bars = self.p["completed_bar_server_epochs"]
        self.assertEqual(len(bars), probe.REQUIRED_COMPLETED)
        self.assertEqual(len(bars), 3)
        diffs = [b - a for a, b in zip(bars, bars[1:])]
        self.assertTrue(all(d == probe.BAR_SECONDS for d in diffs), diffs)
        self.assertEqual(self.p["samples"], 3)
        self.assertEqual(len(self.p["independent_utc_epochs"]), 3)
        self.assertEqual(len(self.p["implied_offsets_seconds"]), 3)
        self.assertEqual(len(self.p["residuals_seconds"]), 3)

    # --- consistent whole-hour offset (reuse probe.consistency_verdict) ---
    def test_consistent_whole_hour_offset(self):
        offsets = self.p["implied_offsets_seconds"]
        for off in offsets:
            self.assertEqual(off % 3600, 0, "offset not a whole hour: %r" % off)
        observations = [{"implied_offset_seconds": off} for off in offsets]
        verdict = probe.consistency_verdict(observations)
        self.assertTrue(verdict["consistent"])
        self.assertEqual(verdict["offset_seconds"], WINTER_OFFSET_S)
        self.assertEqual(verdict["samples"], 3)
        self.assertEqual(
            self.p["consistency"],
            {"consistent": True, "offset_seconds": WINTER_OFFSET_S, "samples": 3},
        )
        self.assertEqual(self.p["offset_hours"], WINTER_OFFSET_S // 3600)
        self.assertEqual(self.p["offset_hours"], 2)
        self.assertEqual(self.rec["mapping"]["offset_seconds"], WINTER_OFFSET_S)
        self.assertEqual(self.rec["mapping"]["offset_hours"], 2)

    # --- bounded residuals, reproduced from the RECORDED bars ------------
    def test_bounded_residuals_reproduce_through_compute_offset(self):
        utcs = self.p["independent_utc_epochs"]
        offsets = self.p["implied_offsets_seconds"]
        residuals = self.p["residuals_seconds"]
        bars = self.p["completed_bar_server_epochs"]
        self.assertEqual(len(utcs), len(offsets))
        self.assertEqual(len(offsets), len(residuals))
        # The probe's server-time anchor is the forming bar's open = last completed bar
        # + one bar. Deriving the anchor from the RECORDED completed_bar_server_epochs
        # (not from the answer) ties the bars to the recorded offsets/residuals: shifting
        # the bars would change the computed offset and fail here. (All three tight reads
        # fall in the same forming minute, so they share this anchor.)
        server_anchor = bars[-1] + probe.BAR_SECONDS
        for utc, offset, residual in zip(utcs, offsets, residuals):
            self.assertGreaterEqual(residual, 0)
            self.assertLessEqual(residual, probe.RESIDUAL_MAX_S)
            got_offset, got_residual = probe.compute_offset(server_anchor, utc)
            self.assertEqual(got_offset, offset)
            self.assertEqual(got_residual, residual)
        # UTC samples are independent and monotonic.
        self.assertTrue(all(b > a for a, b in zip(utcs, utcs[1:])), utcs)

    # --- summer and winter evidence remain separate ----------------------
    def test_summer_and_winter_separate(self):
        winter = self.rec["mapping"]
        summer = self.rec["summer_reference"]
        self.assertEqual(winter["season"], "winter")
        self.assertEqual(summer["season"], "summer")
        self.assertEqual(winter["offset_seconds"], WINTER_OFFSET_S)
        self.assertEqual(summer["offset_seconds"], SUMMER_OFFSET_S)
        self.assertNotEqual(winter["offset_seconds"], summer["offset_seconds"])
        self.assertTrue(winter["season_bound"])
        self.assertTrue(winter["single_observation_bound"])
        self.assertEqual(summer["verdict"], "VERIFIED_SEASON_ONLY")
        pair = self.rec["seasonal_pair"].lower()
        self.assertIn("eet/eest", pair)
        self.assertIn("year-round", pair)  # explicitly NOT generalised year-round

    # --- isolation + teardown proof --------------------------------------
    def test_isolation_and_teardown(self):
        iso = self.rec["isolation"]
        self.assertEqual(iso["bind_host"], "127.0.0.1")
        self.assertEqual(iso["fixed_port"], 8977)
        self.assertTrue(iso["loopback_only"])
        self.assertEqual(iso["protected_port"], 8788)
        self.assertTrue(iso["protected_8788_unchanged"])
        self.assertFalse(iso["terminal_created_by_us"])
        td = self.rec["teardown"]
        self.assertTrue(td["isolated_bridge_stopped"])
        self.assertTrue(td["temp_listener_gone"])
        self.assertTrue(td["protected_8788_unchanged"])
        hc = self.rec["host_post_check"]
        self.assertEqual(hc["listen_8977_count"], 0)
        self.assertEqual(hc["tim_staging_proc_count"], 0)
        self.assertFalse(hc["state_file_exists"])
        self.assertEqual(hc["protected_8788_listener_count"], 1)
        self.assertEqual(hc["protected_8788_owning_pid"], 10272)

    # --- no sensitive field names/values (reuse the probe's own guards) ---
    def test_probe_block_value_blind_contract(self):
        # allow-list keys + forbidden-substring guard, exactly as the probe enforces.
        probe.sanitize_result(dict(self.p))

    def test_whole_record_has_no_forbidden_keys(self):
        probe._assert_no_forbidden_keys(self.rec)

    def test_no_secret_like_blobs_in_record(self):
        blob = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"[0-9a-fA-F]{32,}", blob), [], "hex blob (token/hash)")
        self.assertEqual(re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", blob), [], "base64 blob")
        self.assertTrue(self.rec["value_blind"])

    # --- doc supersedes without erasing; current verdict unambiguous ------
    def test_doc_supersedes_without_erasing(self):
        doc = WINTER_DOC.read_text(encoding="utf-8")
        self.assertIn("TIM_WINTER_TIMEZONE_VERIFIED", doc)
        self.assertIn("TIM_WINTER_TIMEZONE_NOT_VERIFIED", doc)  # history retained
        self.assertIn("TradersWay-Demo = UTC+2 / 7200 s", doc)
        self.assertIn("UTC+3", doc)  # summer preserved
        self.assertIn("EET/EEST", doc)
        self.assertRegex(doc, r"Current verdict[^\n]*TIM_WINTER_TIMEZONE_VERIFIED")


if __name__ == "__main__":
    unittest.main()
