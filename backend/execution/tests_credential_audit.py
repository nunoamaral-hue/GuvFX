"""
GFX-PKT-SEC-CREDENTIAL-ROTATION tests.

The provision_shadow_worker command emits an append-only CREDENTIAL_* audit for
every credential lifecycle change (create / rotate / revoke), and NEVER records
the secret value. log_credential_event is fail-open.
"""

import os
from unittest import mock

from django.test import TestCase

from core.audit import CREDENTIAL_ACTIONS, log_credential_event
from core.models import AuditEvent
from execution.models import WorkerIdentity

TOKEN = "sec-rotation-token-value-should-never-be-logged"
SHADOW = "mt5-shadow-rotate-test"


def _cred_events():
    return AuditEvent.objects.filter(event_type__startswith="CREDENTIAL_")


class CredentialAuditTests(TestCase):
    def _provision(self, token=TOKEN, revoke=False):
        from django.core.management import call_command
        from io import StringIO
        env = {"MT5_SHADOW_WORKER_TOKEN": token}
        args = ["provision_shadow_worker", "--worker-id", SHADOW,
                "--normal-worker-id", "mt5-trade-ingest-1"]
        if revoke:
            args.append("--revoke")
        with mock.patch.dict(os.environ, env):
            call_command(*args, stdout=StringIO())

    def test_create_then_rotate_then_revoke_are_audited(self):
        self._provision()                      # CREATED
        self._provision(token="a-new-secret")  # ROTATED (secret hash changes)
        self._provision(revoke=True)           # REVOKED

        events = list(_cred_events())
        types = {e.event_type for e in events}
        self.assertEqual(
            types, {"CREDENTIAL_CREATED", "CREDENTIAL_ROTATED", "CREDENTIAL_REVOKED"}
        )
        self.assertEqual(len(events), 3)  # exactly one per lifecycle step
        for e in events:
            self.assertEqual(e.entity_type, "WorkerIdentity")
            self.assertEqual(e.entity_id, SHADOW)
            self.assertEqual(e.metadata.get("actor"), "provision_shadow_worker")

    def test_reprovision_same_token_is_not_a_rotation(self):
        self._provision(token=TOKEN)          # CREATED
        self._provision(token=TOKEN)          # same secret -> NOT a rotation
        types = [e.event_type for e in _cred_events()]
        self.assertEqual(types.count("CREDENTIAL_CREATED"), 1)
        self.assertEqual(types.count("CREDENTIAL_ROTATED"), 0)  # no spurious rotation

    def test_no_secret_value_in_any_audit_metadata(self):
        self._provision()
        self._provision(token=TOKEN)
        for e in _cred_events():
            blob = str(e.metadata)
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn(WorkerIdentity.hash_secret(TOKEN), blob)

    def test_helper_fail_open(self):
        # A metadata value json cannot serialise must not raise (fail-open).
        class Weird:
            def __repr__(self):
                return "weird"

        try:
            log_credential_event("ROTATED", entity_type="WorkerIdentity",
                                 entity_id="x", obj=Weird())
        except Exception as exc:  # pragma: no cover
            self.fail(f"log_credential_event raised: {exc!r}")

    def test_credential_actions_constant(self):
        self.assertEqual(CREDENTIAL_ACTIONS, ("CREATED", "ROTATED", "REVOKED"))
