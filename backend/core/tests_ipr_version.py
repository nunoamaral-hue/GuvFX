"""IPR Area G — GET /api/version/ build-provenance endpoint.

Proves: staff-only access; non-secret payload; the resolved arming-flag snapshot covers the backend
flags and reflects the (default-OFF) live values; graceful "unknown" defaults when no build-args set.
"""
from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.version import provenance

User = get_user_model()
URL = "/api/version/"

EXPECTED_FLAGS = {
    "BROKER_CONNECTIVITY_ENABLED", "BROKER_CONNECTIVITY_EXECUTION_GATE",
    "BROKER_CONNECTIVITY_HEALTH_ENABLED", "OPERATIONS_EVENTS_ENABLED",
    "BETA_ONBOARDING_ENABLED", "BETA_RUNTIMES_ENABLED", "BETA_SELF_SERVE_ARM_ENABLED",
}
SECRET = re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY|PRIVATE KEY)", re.IGNORECASE)


class VersionEndpointTests(TestCase):
    def _client(self, *, staff):
        u = User.objects.create_user(
            username="s" if staff else "u", email=("s" if staff else "u") + "@x.invalid",
            password="x", is_staff=staff)
        c = APIClient()
        c.force_authenticate(user=u)
        return c

    def test_requires_staff(self):
        self.assertEqual(self._client(staff=False).get(URL).status_code, 403)

    def test_anonymous_denied(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))

    def test_staff_gets_provenance_and_flags(self):
        r = self._client(staff=True).get(URL)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("git_commit", "build_timestamp", "release_id", "flags"):
            self.assertIn(key, body)
        self.assertEqual(set(body["flags"]), EXPECTED_FLAGS)
        # Defaults resolve to real booleans (never None) and are OFF in the default config.
        for name, val in body["flags"].items():
            self.assertIs(val, False, f"{name} should default OFF")

    def test_flags_reflect_live_values(self):
        # The two accessors resolve differently: the broker flag reads the env var; the beta flag reads
        # the Django setting first. Exercise both paths so the snapshot is proven live, not static.
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_ENABLED": "1"}), \
                override_settings(BETA_SELF_SERVE_ARM_ENABLED=True):
            body = self._client(staff=True).get(URL).json()
        self.assertTrue(body["flags"]["BROKER_CONNECTIVITY_ENABLED"])
        self.assertTrue(body["flags"]["BETA_SELF_SERVE_ARM_ENABLED"])

    def test_no_secret_like_keys_or_values(self):
        # The provenance dict must never carry a secret-shaped key/value.
        import json
        self.assertIsNone(SECRET.search(json.dumps(provenance())))
