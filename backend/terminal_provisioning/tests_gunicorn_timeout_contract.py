"""Regression guard: the gunicorn worker --timeout MUST exceed the synchronous VALIDATE_LOGIN read timeout.

Paid for by a real incident (2026-08-05, disposable acct #13): a browser Test connection whose broker login
took >120s was killed by gunicorn's `--timeout 120` mid-request; the reset connection surfaced to the
customer as a raw ``TypeError: "Failed to fetch"``. The backend VALIDATE_LOGIN read budget is 175s (which
itself must exceed the Agent's 165s result-wait floor), so gunicorn must allow at least that long.
"""
import pathlib
import re

from django.test import SimpleTestCase

from terminal_provisioning.beta_worker import (
    OP_TRANSPORT_TIMEOUTS,
    VALIDATE_LOGIN_AGENT_WAIT_FLOOR_S,
)

DOCKERFILE = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"


class GunicornTimeoutContractTests(SimpleTestCase):
    def _gunicorn_timeout(self) -> int:
        text = DOCKERFILE.read_text()
        m = re.search(r'--timeout"?\s*,?\s*"?(\d+)', text)
        self.assertIsNotNone(m, "gunicorn --timeout not found in backend/Dockerfile")
        return int(m.group(1))

    def test_gunicorn_worker_timeout_exceeds_validate_login_read_budget(self):
        validate_login = int(OP_TRANSPORT_TIMEOUTS["VALIDATE_LOGIN"])
        # sanity: the backend read budget already exceeds the Agent's result-wait floor (existing contract)
        self.assertGreater(validate_login, VALIDATE_LOGIN_AGENT_WAIT_FLOOR_S)
        gunicorn_timeout = self._gunicorn_timeout()
        self.assertGreater(
            gunicorn_timeout, validate_login,
            f"gunicorn --timeout ({gunicorn_timeout}s) must EXCEED the VALIDATE_LOGIN read timeout "
            f"({validate_login}s), or the worker is killed mid-validation and the browser fetch() "
            f'rejects with a raw "Failed to fetch".',
        )
