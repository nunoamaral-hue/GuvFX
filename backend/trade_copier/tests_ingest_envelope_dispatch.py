"""Tests for the fail-closed INGEST-envelope algorithm dispatcher (GFX-173, ADR-0130).

PURE: the dispatcher is a control-flow selector; each verifier is injected. The two properties that matter most
are asserted directly: (1) existing HMAC behaviour is preserved on the legacy path, and (2) an Ed25519 failure
NEVER falls back to HMAC. No host, no network, no DB.
"""
from __future__ import annotations

import time

from django.test import SimpleTestCase, override_settings

from trade_copier import event_ingestion as ei
from trade_copier import ingest_envelope_dispatch as disp
from trade_copier import ingest_event_signing as ies
from trade_copier.flags import copier_ingest_hmac_legacy_enabled

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"   # 40 hex, synthetic — matches the HMAC test idiom


class _Spy:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, 0

    def __call__(self, envelope):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class SelectAlgTests(SimpleTestCase):
    def test_missing_alg_is_hmac(self):
        self.assertEqual(disp.select_alg({"signed": {}}), disp.ALG_HMAC)

    def test_explicit_hmac(self):
        self.assertEqual(disp.select_alg({"alg": "hmac"}), disp.ALG_HMAC)

    def test_ed25519(self):
        self.assertEqual(disp.select_alg({"alg": "ed25519"}), disp.ALG_ED25519)

    def test_unknown(self):
        self.assertEqual(disp.select_alg({"alg": "rsa"}), "")

    def test_non_dict_defaults_to_hmac_structure_reject(self):
        self.assertEqual(disp.select_alg("nope"), disp.ALG_HMAC)


class DispatchTests(SimpleTestCase):
    def test_no_alg_routes_to_hmac_only(self):
        h, e = _Spy(result={"ok": "hmac"}), _Spy(result={"ok": "ed"})
        out = disp.verify_ingest_envelope({"signed": {}}, hmac_verify=h, ed25519_verify=e,
                                          hmac_legacy_enabled=True)
        self.assertEqual(out, {"alg": "hmac", "result": {"ok": "hmac"}})
        self.assertEqual((h.calls, e.calls), (1, 0))

    def test_ed25519_routes_to_ed25519_only(self):
        h, e = _Spy(result={"ok": "hmac"}), _Spy(result={"ok": "ed"})
        out = disp.verify_ingest_envelope({"alg": "ed25519"}, hmac_verify=h, ed25519_verify=e)
        self.assertEqual(out, {"alg": "ed25519", "result": {"ok": "ed"}})
        self.assertEqual((h.calls, e.calls), (0, 1))

    def test_ed25519_failure_never_falls_back_to_hmac(self):
        h = _Spy(result={"ok": "hmac"})
        e = _Spy(raises=ies.IngestEventError(ies.R_BAD_SIGNATURE))
        with self.assertRaises(ies.IngestEventError) as c:
            disp.verify_ingest_envelope({"alg": "ed25519"}, hmac_verify=h, ed25519_verify=e)
        self.assertEqual(c.exception.reason, ies.R_BAD_SIGNATURE)
        self.assertEqual(h.calls, 0)          # the whole point: HMAC was NEVER attempted

    def test_unknown_alg_fails_closed_without_calling_either(self):
        h, e = _Spy(), _Spy()
        with self.assertRaises(disp.DispatchError) as c:
            disp.verify_ingest_envelope({"alg": "rsa"}, hmac_verify=h, ed25519_verify=e)
        self.assertEqual(c.exception.reason, disp.R_UNKNOWN_ALG)
        self.assertEqual((h.calls, e.calls), (0, 0))

    def test_hmac_legacy_disabled_fails_closed_without_ed25519(self):
        h, e = _Spy(result={"ok": "hmac"}), _Spy(result={"ok": "ed"})
        with self.assertRaises(disp.DispatchError) as c:
            disp.verify_ingest_envelope({"alg": "hmac"}, hmac_verify=h, ed25519_verify=e,
                                        hmac_legacy_enabled=False)
        self.assertEqual(c.exception.reason, disp.R_HMAC_LEGACY_DISABLED)
        self.assertEqual((h.calls, e.calls), (0, 0))

    def test_hmac_legacy_preserves_existing_behavior_byte_for_byte(self):
        """A legacy HMAC envelope routed through the dispatcher yields EXACTLY what a direct
        ``verify_ingest_request`` yields — the existing behaviour is preserved on the legacy path."""
        now = time.time()
        payload = {"revisions": [{"account_id": 1, "ticket": 1, "revision": 1, "side": "BUY", "volume": 0.1}]}
        env = ei.sign_ingest_request(account_id=1, secret=SECRET, nonce="hn1", now=now, payload=payload)
        keyring = {ei.DEFAULT_KEY_ID: SECRET}

        direct = ei.verify_ingest_request(env, keyring=keyring, now=now, nonce_burn=_mem())
        routed = disp.verify_ingest_envelope(
            env,
            hmac_verify=lambda ev: ei.verify_ingest_request(ev, keyring=keyring, now=now, nonce_burn=_mem()),
            ed25519_verify=lambda ev: (_ for _ in ()).throw(AssertionError("ed25519 must not run")),
            hmac_legacy_enabled=True)
        self.assertEqual(routed["alg"], "hmac")
        self.assertEqual(routed["result"], direct)

    @override_settings()
    def test_hmac_legacy_flag_defaults_on(self):
        self.assertTrue(copier_ingest_hmac_legacy_enabled())      # preserve existing HMAC behaviour by default

    @override_settings(COPIER_INGEST_HMAC_LEGACY_ENABLED="0")
    def test_hmac_legacy_flag_can_be_disabled(self):
        self.assertFalse(copier_ingest_hmac_legacy_enabled())


def _mem():
    seen = set()

    def burn(nonce, expiry):
        if nonce in seen:
            return False
        seen.add(nonce)
        return True
    return burn
