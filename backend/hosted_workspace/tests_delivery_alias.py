"""Beta Readiness Stream 6 (M2 / Phase 5) — the delivery descriptor uses the server-derived per-account alias.

Proves the RemoteApp program in the signed descriptor is DERIVED from ``trading_account.id`` (never the browser),
that account N's descriptor can reference only account N's alias (no IDOR / program injection), and that
Customer Zero keeps its legacy ``terminal64`` alias. The descriptor builder is exercised with the crypto/guac
primitives mocked so the test needs no secret or host.
"""
from unittest import mock

from django.test import SimpleTestCase

from hosted_workspace.delivery import _build_signed_descriptor


class DescriptorAliasTests(SimpleTestCase):
    def _captured_alias(self, account_id):
        captured = {}

        def fake_payload(**kw):
            captured.update(kw)
            return {"expires": 123, "remote-app": f"||{kw.get('remote_app')}"}

        prov = mock.Mock(trading_account_id=account_id, windows_username=f"guvfx_u_{account_id}",
                         runtime_root=rf"C:\GuvFX\accounts\{account_id}", password_enc="enc")
        node = mock.Mock(rdp_host="10.9.9.9")
        workspace = mock.Mock(workspace_uuid=f"uuid-{account_id}")
        with mock.patch("mt5.guac_json.build_remoteapp_rdp_payload", side_effect=fake_payload), \
                mock.patch("mt5.guac_json.sign_and_encrypt_json", return_value="TOKEN"), \
                mock.patch("mt5.guac_json.build_guac_data_url", return_value="https://guac.invalid/#/x"), \
                mock.patch("trading.crypto.decrypt_password", return_value="pw"):
            _build_signed_descriptor(workspace=workspace, prov=prov, node=node,
                                     base_url="https://guac.invalid", secret_hex="ab" * 16)
        return captured["remote_app"]

    def test_customer_zero_keeps_legacy_alias(self):
        self.assertEqual(self._captured_alias(1), "terminal64")

    def test_account_two_uses_its_own_alias(self):
        self.assertEqual(self._captured_alias(2), "guvfx_mt5_2")

    def test_each_account_can_only_reference_its_own_program(self):
        # The alias is a pure function of prov.trading_account_id — there is no path for account 2's descriptor
        # to name account 3's program (no browser-chosen program, no full-desktop fallback).
        self.assertEqual(self._captured_alias(3), "guvfx_mt5_3")
        self.assertNotEqual(self._captured_alias(2), self._captured_alias(3))
