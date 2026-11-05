"""ADR-0077 — emitter config + host-side gates. Fail-closed identity + default-OFF gates (synthetic only)."""
from django.test import SimpleTestCase

from trade_copier.emitter.config import ConfigError, EmitterConfig, load_config

BASE = dict(account_id=46, broker_login="55442", broker_server="Tradersway-Live", host_id="host-A",
            bridge_base_url="http://127.0.0.1:8806", ingest_url="https://api.example.invalid/api/copy-trading/event-ingest/")


class ConfigTests(SimpleTestCase):
    def test_gates_default_off_and_malformed_off(self):
        cfg = load_config(BASE)
        self.assertFalse(cfg.attestation_allowed)
        self.assertFalse(cfg.observation_allowed)
        self.assertFalse(cfg.delivery_allowed)
        cfg2 = load_config({**BASE, "attestation_allowed": "maybe", "observation_allowed": "", "delivery_allowed": None})
        self.assertFalse(cfg2.attestation_allowed)
        self.assertFalse(cfg2.observation_allowed)
        self.assertFalse(cfg2.delivery_allowed)

    def test_gates_on_only_when_truthy(self):
        cfg = load_config({**BASE, "attestation_allowed": "true", "observation_allowed": "1", "delivery_allowed": "on"})
        self.assertTrue(cfg.attestation_allowed and cfg.observation_allowed and cfg.delivery_allowed)

    def test_require_complete_identity(self):
        load_config(BASE).require_complete_identity()   # ok
        with self.assertRaises(ConfigError):
            load_config({**BASE, "broker_server": ""}).require_complete_identity()
        with self.assertRaises(ConfigError):
            load_config({**BASE, "account_id": 1}).require_complete_identity()   # reserved id refused
        with self.assertRaises(ConfigError):
            load_config({**BASE, "bridge_base_url": "http://10.0.0.5:8806"}).require_complete_identity()  # not loopback

    def test_config_digest_stable_and_sensitive(self):
        a = load_config(BASE).config_digest()
        self.assertEqual(a, load_config(dict(BASE)).config_digest())
        self.assertNotEqual(a, load_config({**BASE, "host_id": "host-B"}).config_digest())

    def test_fast_interval_off_by_default_and_floored(self):
        self.assertEqual(load_config(BASE).effective_fast_interval(), 0.0)      # disabled until configured
        self.assertEqual(load_config({**BASE, "fast_interval_seconds": 0.1, "min_interval_seconds": 1.0})
                         .effective_fast_interval(), 1.0)                        # floored at the minimum

    def test_secret_env_names_only_no_secret_values(self):
        cfg = load_config(BASE)
        # config holds env-var NAMES, never values; the digest/identity carry no secret.
        self.assertEqual(cfg.ingest_key_env, "COPIER_INGEST_HMAC_KEY")
        ident = cfg.identity_fingerprints()
        self.assertNotIn("55442", str(ident))          # raw login never present (fingerprinted)
        self.assertEqual(len(ident["broker_login_fp"]), 32)
