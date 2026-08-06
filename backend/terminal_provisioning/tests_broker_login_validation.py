"""ADR-0027 — backend BrokerLoginValidator tests (fake transport + fake agent). No real MT5/agent/broker.

Proves the full customer-safe failure taxonomy, that the sealed password + binding reach the agent intact,
that the credential-access audit fires without a secret, and that NO plaintext/ciphertext ever appears in the
outcome or logs.
"""
import base64
import io
import json
import logging
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import BetaTester
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from terminal_provisioning import broker_cred_envelope as envelope
from terminal_provisioning.broker_login_validation import (
    BrokerLoginValidator, HEALTHY, NEEDS_ATTENTION, UNAVAILABLE)
from terminal_provisioning.mgmt_client import (
    ManagementChannelError, ManagementChannelTimeout, ManagementChannelUnreachable)

U = get_user_model()
SECRET = "s3cret-broker-pw!never-real"
_PRIV = X25519PrivateKey.generate()
_PUB_B64 = base64.b64encode(_PRIV.public_key().public_bytes_raw()).decode()
ENC = override_settings(BROKER_CRED_ENC_KEY_ID="e1",
                        BROKER_CRED_ENC_PUBKEYS=json.dumps({"e1": _PUB_B64}),
                        BETA_MAX_TESTERS=1000)
_CTR = {"n": 0}


def _account(*, server="IS6Technologies-Demo", password=SECRET, number="1302575"):
    _CTR["n"] += 1
    u = U.objects.create_user(username=f"blv{_CTR['n']}", email=f"blv{_CTR['n']}@x.invalid", password="x")
    BetaTester.objects.create(email=f"blv{_CTR['n']}@x.invalid")
    return TradingAccount.objects.create(
        user=u, name="A", account_number=number, broker_name=server, is_demo=True,
        password_enc=encrypt_password(password) if password is not None else "")


class _FakeAgent:
    """Records the request, PROVES the envelope opens to SECRET under the request's own binding, asserts no
    plaintext leaks, then returns a scripted outcome."""
    def __init__(self, script):
        self.script = script
        self.request = None

    def __call__(self, base, req):
        self.request = req
        # no plaintext password anywhere in the transmitted request
        assert SECRET not in json.dumps(req, default=str)
        pl = req["payload"]
        aad = envelope.bind_aad(operation=req["operation"], runtime_uuid=req["runtime_uuid"],
                                correlation_id=req["correlation_id"], nonce=req["nonce"])
        opened = envelope.open_envelope(pl["password_env"], aad=aad, recipient_private_key=_PRIV)
        assert opened == SECRET.encode()          # the agent recovers exactly the password, bound to THIS req
        return dict(self.script)


@ENC
class BrokerLoginValidatorTests(TestCase):
    def _validate(self, account, script=None, transport=None):
        t = transport or _FakeAgent(script or {"outcome": "ok", "reason_code": "demo_ok", "is_demo": True})
        v = BrokerLoginValidator(transport=t, base_url="http://agent.invalid",
                                 keyring={"k1": "sig"}, key_id="k1")
        return v.validate(account), t

    def test_successful_demo_validation(self):
        out, _ = self._validate(_account(), {"outcome": "ok", "reason_code": "demo_ok", "is_demo": True})
        self.assertEqual((out.status, out.reason, out.is_demo), (HEALTHY, "demo_ok", True))
        self.assertEqual(out.server, "IS6Technologies-Demo")
        self.assertEqual(out.login_masked, "***575")

    def test_live_account_detected(self):
        out, _ = self._validate(_account(), {"outcome": "ok", "reason_code": "live_detected", "is_demo": False})
        self.assertEqual((out.status, out.reason, out.is_demo), (HEALTHY, "live_detected", False))

    def test_taxonomy_failures(self):
        cases = {
            "invalid_password": (NEEDS_ATTENTION, False), "invalid_login": (NEEDS_ATTENTION, False),
            "server_not_found": (NEEDS_ATTENTION, False), "account_disabled": (NEEDS_ATTENTION, False),
            "classification_mismatch": (NEEDS_ATTENTION, False), "server_unavailable": (UNAVAILABLE, True),
            "mt5_unavailable": (UNAVAILABLE, True),
        }
        for reason, (status, retryable) in cases.items():
            out, _ = self._validate(_account(), {"outcome": "denied", "reason_code": reason})
            self.assertEqual((out.status, out.retryable), (status, retryable), reason)
            self.assertEqual(out.reason, reason)

    def test_connect_timeout_maps_to_agent_unreachable_not_login_timeout(self):
        # WS-B (2026-08-05): a CONNECT timeout means the request was never sent — no login was attempted.
        # It must classify as validation_agent_unreachable, NEVER login_timeout.
        def t(base, req):
            raise ManagementChannelUnreachable()
        out, _ = self._validate(_account(), transport=t)
        self.assertEqual((out.status, out.reason, out.retryable),
                         (UNAVAILABLE, "validation_agent_unreachable", True))
        self.assertNotEqual(out.reason, "login_timeout")

    def test_read_timeout_maps_to_agent_timeout_not_login_timeout(self):
        # WS-B: a READ timeout means the agent was reached but didn't answer — MT5 status unknown. It must
        # classify as validation_agent_timeout, NEVER login_timeout (the backend has no login evidence).
        def t(base, req):
            raise ManagementChannelTimeout()
        out, _ = self._validate(_account(), transport=t)
        self.assertEqual((out.status, out.reason, out.retryable),
                         (UNAVAILABLE, "validation_agent_timeout", True))
        self.assertNotEqual(out.reason, "login_timeout")

    def test_login_timeout_only_from_agent_reason(self):
        # login_timeout is now ONLY legitimate when the AGENT itself returns it (MT5 reported a login-phase
        # timeout) — never synthesised by the backend transport layer.
        out, _ = self._validate(_account(), {"outcome": "denied", "reason_code": "login_timeout"})
        self.assertEqual((out.status, out.reason), (UNAVAILABLE, "login_timeout"))

    def test_transport_error_maps_safely(self):
        def t(base, req):
            raise OSError("bridge down")
        out, _ = self._validate(_account(), transport=t)
        self.assertEqual((out.status, out.reason, out.retryable), (UNAVAILABLE, "bridge_unavailable", True))
        def t2(base, req):
            raise ManagementChannelError("agent_denied")
        out2, _ = self._validate(_account(), transport=t2)
        self.assertEqual(out2.reason, "bridge_unavailable")

    def test_missing_credential(self):
        out, _ = self._validate(_account(password=None))
        self.assertEqual((out.status, out.reason), (NEEDS_ATTENTION, "credential_missing"))

    def test_missing_server(self):
        with patch("terminal_provisioning.broker_login_validation.resolve_broker_server",
                   return_value=(None, "broker_server_missing")):
            out, t = self._validate(_account())
        self.assertEqual((out.status, out.reason), (NEEDS_ATTENTION, "broker_server_missing"))
        self.assertIsNone(t.request)                 # never contacted the agent

    def test_unconfigured_envelope_fails_closed(self):
        with override_settings(BROKER_CRED_ENC_KEY_ID="", BROKER_CRED_ENC_PUBKEYS=""):
            out, t = self._validate(_account())
        self.assertEqual((out.status, out.reason), (UNAVAILABLE, "validation_unconfigured"))
        self.assertIsNone(t.request)

    def test_backend_with_private_keys_refuses_to_seal(self):
        # SEAL-ONLY invariant: if the backend is (mis)configured with envelope PRIVATE keys it must refuse to
        # validate and never contact the agent — the "backend cannot decrypt" property is code-enforced.
        with override_settings(BROKER_CRED_ENC_PRIVKEYS=json.dumps(
                {"e1": base64.b64encode(_PRIV.private_bytes_raw()).decode()})):
            out, t = self._validate(_account())
        self.assertEqual((out.status, out.reason), (UNAVAILABLE, "validation_unconfigured"))
        self.assertIsNone(t.request)

    def test_seal_failure_fails_closed_without_raising(self):
        # even with the envelope "configured", a seal error must become a ValidationOutcome (validate never
        # raises) with the agent never contacted.
        def boom(*a, **k):
            raise envelope.EnvelopeError("envelope_unknown_key_id")
        with patch("terminal_provisioning.broker_login_validation.envelope.seal", boom):
            out, t = self._validate(_account())
        self.assertEqual((out.status, out.reason), (UNAVAILABLE, "validation_unconfigured"))
        self.assertIsNone(t.request)

    def test_credential_access_audited_without_secret(self):
        with patch("core.audit.log_customer_credential_event") as audit:
            self._validate(_account())
        self.assertTrue(audit.called)
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs.get("purpose"), "login-validation")
        self.assertNotIn(SECRET, json.dumps({k: str(v) for k, v in kwargs.items()}))

    def test_outcome_and_logs_carry_no_secret(self):
        buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); root.addHandler(h)
        try:
            out, t = self._validate(_account(), {"outcome": "ok", "reason_code": "demo_ok", "is_demo": True})
        finally:
            root.removeHandler(h)
        blob = json.dumps(out.as_dict()) + buf.getvalue()
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("password", out.as_dict())           # no password/ciphertext field on the outcome
        self.assertNotIn("password_enc", json.dumps(out.as_dict()))
