"""Phase 3 (P3-C) — customer-credential ACCESS audit at the MT5 launch-handoff sites.

Both launch views decrypt the customer broker password to write it into launch_account.json. Each
such read must emit a redacted CREDENTIAL_ACCESSED audit (purpose="launch-handoff") — never the
plaintext. The environment-heavy tail (guac link, handoff pool) is patched so the test reaches and
asserts the audit deterministically.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import AuditEvent
from mt5.models import Mt5Instance
from mt5.views import Mt5DesktopLinkView, Mt5LaunchApplyView
from trading.crypto import encrypt_password
from trading.models import TradingAccount

U = get_user_model()
_KEY = {"GUVFX_FERNET_KEY": Fernet.generate_key().decode(), "DJANGO_SECRET_KEY": "unit-test-secret"}


class LaunchHandoffAccessAuditTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="lu", email="lu@x.invalid", password="x")
        self.inst = Mt5Instance.objects.create(hostname="host-1")
        with mock.patch.dict(os.environ, _KEY):
            self.acct = TradingAccount.objects.create(
                user=self.user, name="A", account_number="55443322", broker_name="DemoBroker",
                is_demo=True, is_active=True, mt5_instance=self.inst,
                password_enc=encrypt_password("s3cret-pw"))
        self.factory = APIRequestFactory()

    def _assert_access_audit(self):
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(self.acct.id))
        self.assertEqual(ev.metadata["purpose"], "launch-handoff")
        self.assertEqual(ev.metadata["account_number_suffix"], "****3322")
        self.assertNotIn("s3cret-pw", json.dumps(ev.metadata or {}))
        self.assertEqual(ev.user_id, self.user.id)   # acting user captured

    def test_desktop_link_view_audits_access(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {**_KEY, "GUAC_JSON_SECRET_KEY_HEX": "deadbeef"}), \
             mock.patch("mt5.views.lease_instance_for_user", return_value=self.inst), \
             mock.patch("mt5.views.HANDOFF_POOL", tmp), \
             mock.patch("mt5.views.build_mt5_desktop_payload", return_value={}), \
             mock.patch("mt5.views.sign_and_encrypt_json", return_value="b64"), \
             mock.patch("mt5.views.build_guac_data_url", return_value="http://guac/x"):
            req = self.factory.post("/api/mt5/desktop-link/")
            force_authenticate(req, user=self.user)
            resp = Mt5DesktopLinkView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self._assert_access_audit()

    def test_launch_apply_view_audits_access(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, _KEY), \
             mock.patch("trading.views._get_user_mt5_instance", return_value=self.inst), \
             mock.patch("mt5.views.HANDOFF_POOL", tmp):
            req = self.factory.post("/api/mt5/launch-apply/")
            force_authenticate(req, user=self.user)
            resp = Mt5LaunchApplyView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self._assert_access_audit()

    def test_no_password_enc_emits_no_access_audit(self):
        # An active, instance-bound account with no stored credential must NOT record an access
        # (no real decrypt happened) — consistency with the provisioner guard.
        self.acct.password_enc = ""
        self.acct.save(update_fields=["password_enc"])
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, _KEY), \
             mock.patch("trading.views._get_user_mt5_instance", return_value=self.inst), \
             mock.patch("mt5.views.HANDOFF_POOL", tmp):
            req = self.factory.post("/api/mt5/launch-apply/")
            force_authenticate(req, user=self.user)
            resp = Mt5LaunchApplyView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ACCESSED", entity_id=str(self.acct.id)).exists())
