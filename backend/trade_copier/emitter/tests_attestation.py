"""ADR-0077 — pure attestation envelope: positive + the full negative-control matrix (synthetic key only)."""
import time

from django.test import SimpleTestCase

from trade_copier.emitter import attestation as att

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
KEYRING = {"copier-ingest": SECRET}


class _Burn:
    def __init__(self):
        self.seen = set()

    def __call__(self, nonce, expiry):
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


def _env(**over):
    kw = dict(secret=SECRET, account_id=46, broker_login="55442", broker_server="Tradersway-Live",
              host_id="host-A", terminal_fp="tfp", bridge_id="127.0.0.1:8806", build_sha="fb4ee402",
              config_digest="cd", nonce="n-" + str(time.time()), now=time.time())
    kw.update(over)
    return att.build_attestation(**kw)


def _expected(**over):
    exp = dict(account_id=46, broker_login_fp=att.fingerprint("55442"), broker_server="Tradersway-Live",
               host_id="host-A", terminal_fp="tfp", bridge_id="127.0.0.1:8806")
    exp.update(over)
    return exp


class AttestationTests(SimpleTestCase):
    def test_positive_roundtrip_with_expected(self):
        out = att.verify_attestation(_env(), keyring=KEYRING, now=time.time(), nonce_burn=_Burn(),
                                     expected=_expected())
        self.assertEqual(out["signed"]["account_id"], 46)

    def test_bad_signature(self):
        env = _env()
        env["signature"] = "0" * 64
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(env, keyring=KEYRING, now=time.time(), nonce_burn=_Burn())
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_tampered_payload(self):
        env = _env()
        env["signed"]["account_id"] = 999       # mutate after signing
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(env, keyring=KEYRING, now=time.time(), nonce_burn=_Burn())
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_unknown_key(self):
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(_env(), keyring={"other": SECRET}, now=time.time(), nonce_burn=_Burn())
        self.assertEqual(c.exception.reason, "unknown_key_id")

    def test_stale_and_future_and_expired(self):
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(_env(now=time.time() - 10_000), keyring=KEYRING, now=time.time(), nonce_burn=_Burn())
        self.assertIn(c.exception.reason, ("attestation_stale", "attestation_expired"))
        with self.assertRaises(att.AttestError) as c2:
            att.verify_attestation(_env(now=time.time() + 10_000), keyring=KEYRING, now=time.time(), nonce_burn=_Burn())
        self.assertEqual(c2.exception.reason, "timestamp_in_future")

    def test_replay(self):
        burn = _Burn()
        env = _env(nonce="fixed")
        att.verify_attestation(env, keyring=KEYRING, now=time.time(), nonce_burn=burn)
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(env, keyring=KEYRING, now=time.time(), nonce_burn=burn)
        self.assertEqual(c.exception.reason, "nonce_replayed")

    def test_identity_mismatches_fail_closed(self):
        cases = {
            "account_mismatch": _expected(account_id=48),
            "broker_login_mismatch": _expected(broker_login_fp=att.fingerprint("99999")),
            "broker_server_mismatch": _expected(broker_server="IS6Technologies-Live"),
            "host_mismatch": _expected(host_id="host-Z"),
            "terminal_mismatch": _expected(terminal_fp="other"),
            "bridge_mismatch": _expected(bridge_id="127.0.0.1:9999"),
            "build_not_allowed": _expected(build_sha_allow=["deadbeef"]),
        }
        for reason, exp in cases.items():
            with self.assertRaises(att.AttestError) as c:
                att.verify_attestation(_env(), keyring=KEYRING, now=time.time(), nonce_burn=_Burn(), expected=exp)
            self.assertEqual(c.exception.reason, reason)

    def test_missing_field(self):
        env = _env()
        env["signed"].pop("host_id")
        with self.assertRaises(att.AttestError) as c:
            att.verify_attestation(env, keyring=KEYRING, now=time.time(), nonce_burn=_Burn())
        self.assertEqual(c.exception.reason, "missing_signed_field")

    def test_login_fingerprinted_never_raw(self):
        env = _env()
        self.assertNotIn("55442", str(env))         # raw login never in the envelope
        self.assertEqual(len(env["signed"]["broker_login_fp"]), 32)
