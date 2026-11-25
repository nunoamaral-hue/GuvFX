"""Synthetic tests for the fail-closed #48 follower admission / identity / flatness probe (GFX-172, ADR-0129).
The read-only evidence provider is injected with synthetic dicts; the probe must PASS only on fresh, authoritative,
identity-matched, flat, admission-off evidence and FAIL CLOSED on everything else. No host, no network.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from trade_copier import follower_admission_probe as fap

NOW = 1_800_000_000
FID, IDENT = 48, "5176383@IS6Technologies-Live"


def _reader(**over):
    ev = dict(ok=True, follower_account_id=FID, follower_identity=IDENT,
              admission_off=True, flat=True, open_positions=0, observed_at_epoch=NOW - 5, source="synthetic")
    ev.update(over)
    return lambda: ev


def _eval(reader, *, now=NOW, max_age_s=60):
    return fap.evaluate_follower_admission(
        reader, expected_follower_account_id=FID, expected_identity=IDENT, now_epoch=now, max_age_s=max_age_s)


class FollowerAdmissionProbeTests(SimpleTestCase):
    def test_fresh_authoritative_flat_admission_off_passes(self):
        res = _eval(_reader())
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["off"])
        self.assertTrue(res["identity_ok"] and res["flat"])

    def test_none_reader_is_unavailable(self):
        self.assertEqual(_eval(None)["reason"], fap.R_UNAVAILABLE)

    def test_reader_error_is_unavailable(self):
        def _boom():
            raise RuntimeError("host down")
        res = _eval(_boom)
        self.assertEqual(res["reason"], fap.R_UNAVAILABLE)
        self.assertFalse(res["off"])

    def test_not_ok_evidence_is_unavailable(self):
        self.assertEqual(_eval(_reader(ok=False))["reason"], fap.R_UNAVAILABLE)

    def test_stale_evidence_fails(self):
        res = _eval(_reader(observed_at_epoch=NOW - 10_000))
        self.assertEqual(res["reason"], fap.R_STALE)
        self.assertFalse(res["off"])

    def test_missing_observed_at_is_stale(self):
        self.assertEqual(_eval(_reader(observed_at_epoch=None))["reason"], fap.R_STALE)

    def test_identity_mismatch_fails(self):
        self.assertEqual(_eval(_reader(follower_identity="9999@Other-Live"))["reason"], fap.R_IDENTITY_MISMATCH)

    def test_account_id_mismatch_fails(self):
        self.assertEqual(_eval(_reader(follower_account_id=49))["reason"], fap.R_IDENTITY_MISMATCH)

    def test_ambiguous_identity_fails(self):
        self.assertEqual(_eval(_reader(follower_identity=""))["reason"], fap.R_AMBIGUOUS)

    def test_ambiguous_flatness_fails(self):
        # An absent position count is AMBIGUOUS, never "assumed flat".
        self.assertEqual(_eval(_reader(open_positions=None))["reason"], fap.R_AMBIGUOUS)

    def test_non_flat_fails(self):
        res = _eval(_reader(open_positions=1, flat=False))
        self.assertEqual(res["reason"], fap.R_NON_FLAT)
        self.assertFalse(res["off"])

    def test_non_flat_even_if_flag_true_but_positions_open(self):
        # Defence in depth: a flat=True flag with open positions is still NON_FLAT.
        self.assertEqual(_eval(_reader(open_positions=2, flat=True))["reason"], fap.R_NON_FLAT)

    def test_admission_not_off_fails(self):
        res = _eval(_reader(admission_off=False))
        self.assertEqual(res["reason"], fap.R_ADMISSION_NOT_OFF)
        self.assertFalse(res["off"])

    def test_production_reader_is_unavailable_dark(self):
        reader = fap.production_follower_reader(FID)
        self.assertEqual(_eval(reader)["reason"], fap.R_UNAVAILABLE)

    def test_verdict_is_value_blind(self):
        res = _eval(_reader())
        blob = str(res).lower()
        for needle in ("5176383", "is6technologies", "password", "hmac", "keyring", "secret"):
            self.assertNotIn(needle, blob)   # no identity secret / login / server echoed in the verdict
