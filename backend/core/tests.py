import os
import subprocess
import sys

from django.conf import settings
from django.test import TestCase


class GuvfxDataRootSettingTest(TestCase):
    """GFX-PKT-006C: GUVFX_DATA_ROOT is wired with no default (None when unset)."""

    def test_setting_exists(self):
        # The attribute must exist regardless of environment.
        self.assertTrue(hasattr(settings, "GUVFX_DATA_ROOT"))

    def test_is_none_when_env_removed(self):
        # In a fresh subprocess with GUVFX_DATA_ROOT removed, the resolved
        # setting must be None — proving there is no default fallback.
        env = dict(os.environ)
        env.pop("GUVFX_DATA_ROOT", None)
        code = (
            "import django, os;"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'guvfx_backend.settings');"
            "django.setup();"
            "from django.conf import settings;"
            "print(repr(settings.GUVFX_DATA_ROOT))"
        )
        out = subprocess.check_output([sys.executable, "-c", code], env=env)
        self.assertEqual(out.decode("utf-8").strip(), "None")
