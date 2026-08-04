"""IPR Area A — customer-safe messaging for the terminal-session service errors.

Proves ``_safe_service_message`` maps each service-layer exception TYPE to a customer-safe string and
never echoes the raw exception text (which may carry internal service detail).
"""
from __future__ import annotations

import unittest

from mt5.views_interaction import _map_service_error, _safe_service_message
from mt5.services.authorization_validation_service import AuthorizationDenied
from mt5.services.binding_resolution_service import BindingResolutionError
from mt5.services.binding_occupancy_enforcement_service import OccupancyError
from mt5.services.session_launch_orchestration_service import LaunchError
from mt5.services.session_resume_service import ResumeError
from mt5.services.session_terminate_service import TerminateError

SECRET = "INTERNAL windows_username=guvfx_u_9 host=10.50.0.2 token=abc"


class SafeServiceMessageTests(unittest.TestCase):
    def test_never_echoes_raw_exception_text(self):
        for exc_cls in (AuthorizationDenied, BindingResolutionError, OccupancyError,
                        LaunchError, ResumeError, TerminateError, ValueError):
            msg = _safe_service_message(exc_cls(SECRET))
            self.assertNotIn(SECRET, msg)
            self.assertNotIn("guvfx_u_9", msg)
            self.assertNotIn("10.50.0.2", msg)
            self.assertNotIn("token", msg.lower())
            self.assertTrue(msg.strip())

    def test_type_specific_customer_safe_messages(self):
        self.assertIn("access", _safe_service_message(AuthorizationDenied("x")).lower())
        self.assertIn("wasn't found", _safe_service_message(BindingResolutionError("x")))
        self.assertIn("in use", _safe_service_message(OccupancyError("x")))
        self.assertIn("started", _safe_service_message(LaunchError("x")))
        self.assertIn("resumed", _safe_service_message(ResumeError("x")))
        self.assertIn("closed", _safe_service_message(TerminateError("x")))
        # Unknown → generic, still safe.
        self.assertIn("Something went wrong", _safe_service_message(RuntimeError("x")))

    def test_status_codes_still_mapped_by_type(self):
        self.assertEqual(_map_service_error(AuthorizationDenied("x")), 403)
        self.assertEqual(_map_service_error(BindingResolutionError("x")), 404)
        self.assertEqual(_map_service_error(OccupancyError("x")), 409)
        self.assertEqual(_map_service_error(LaunchError("x")), 409)
        self.assertEqual(_map_service_error(RuntimeError("x")), 400)


if __name__ == "__main__":
    unittest.main()
