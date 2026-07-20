"""GFX-BETA-PHASE0 (C2/C17/C19) — _get_user_mt5_instance must fail closed: a user must NEVER resolve
to an MT5 instance leased to a DIFFERENT user (which would drive that user's terminal)."""
from django.test import TestCase
from django.contrib.auth import get_user_model

from mt5.models import Mt5Instance
from trading.views import _get_user_mt5_instance

U = get_user_model()


class GetUserMt5InstanceFailCloseTests(TestCase):
    def setUp(self):
        self.a = U.objects.create_user(username="a", email="a@x.invalid", password="x")
        self.b = U.objects.create_user(username="b", email="b@x.invalid", password="x")

    def _inst(self, host, **kw):
        return Mt5Instance.objects.create(
            hostname=host, platform="WINDOWS", windows_username="Administrator", **kw)

    def test_instance_leased_to_user_is_returned(self):
        i = self._inst("h1", is_leased=True, leased_to=self.a)
        self.assertEqual(_get_user_mt5_instance(self.a), i)

    def test_instance_leased_to_other_user_is_NOT_returned(self):
        # A's leased box must never resolve for B (the dangerous C2/C17/C19 path).
        self._inst("h1", is_leased=True, leased_to=self.a)
        self.assertIsNone(_get_user_mt5_instance(self.b))

    def test_unleased_instance_is_an_acceptable_fallback(self):
        i = self._inst("h1", is_leased=False)
        self.assertEqual(_get_user_mt5_instance(self.b), i)

    def test_prefers_unleased_over_another_users_lease(self):
        self._inst("h1", is_leased=True, leased_to=self.a)  # A's box (must be skipped for B)
        free = self._inst("h2", is_leased=False)            # unleased fallback
        self.assertEqual(_get_user_mt5_instance(self.b), free)

    def test_no_windows_instance_returns_none(self):
        Mt5Instance.objects.create(hostname="lin", platform="LINUX", windows_username="")
        self.assertIsNone(_get_user_mt5_instance(self.a))
