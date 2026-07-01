"""
EXEC-E2b-PERSIST tests — provision_shadow_worker management command.

Proves the dedicated shadow identity is created with the shadow_worker grant, the
secret is taken from the environment (never a CLI arg) and only its hash stored,
the command refuses to reuse the normal worker identity, is idempotent, and can
revoke (rollback).
"""

import os
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from execution.models import WorkerIdentity


class ProvisionShadowWorkerTests(TestCase):
    SHADOW = "mt5-shadow-worker-1"
    NORMAL = "mt5-trade-ingest-1"
    TOKEN = "s3cr3t-shadow-token-value"

    def _run(self, **kwargs):
        call_command("provision_shadow_worker", **kwargs)

    def test_creates_distinct_identity_with_shadow_grant(self):
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": self.TOKEN}):
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        w = WorkerIdentity.objects.get(worker_id=self.SHADOW)
        self.assertEqual(w.status, WorkerIdentity.Status.ACTIVE)
        self.assertTrue((w.worker_permissions or {}).get("shadow_worker"))
        # Only the hash is stored — never the raw secret.
        self.assertEqual(w.worker_secret_hash, WorkerIdentity.hash_secret(self.TOKEN))
        self.assertNotIn(self.TOKEN, w.worker_secret_hash)

    def test_refuses_to_reuse_normal_worker_identity(self):
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": self.TOKEN}):
            with self.assertRaises(CommandError):
                self._run(worker_id=self.NORMAL, normal_worker_id=self.NORMAL)
        self.assertFalse(WorkerIdentity.objects.filter(worker_id=self.NORMAL).exists())

    def test_requires_token_in_env(self):
        env = {k: v for k, v in os.environ.items() if k != "MT5_SHADOW_WORKER_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(CommandError):
                self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        self.assertFalse(WorkerIdentity.objects.filter(worker_id=self.SHADOW).exists())

    def test_idempotent_update_single_row(self):
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": self.TOKEN}):
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        self.assertEqual(WorkerIdentity.objects.filter(worker_id=self.SHADOW).count(), 1)
        w = WorkerIdentity.objects.get(worker_id=self.SHADOW)
        self.assertTrue((w.worker_permissions or {}).get("shadow_worker"))

    def test_rotates_secret_on_reprovision(self):
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": self.TOKEN}):
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": "new-token"}):
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        w = WorkerIdentity.objects.get(worker_id=self.SHADOW)
        self.assertEqual(w.worker_secret_hash, WorkerIdentity.hash_secret("new-token"))

    def test_revoke_drops_grant_and_sets_revoked(self):
        with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER_TOKEN": self.TOKEN}):
            self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL)
        self._run(worker_id=self.SHADOW, normal_worker_id=self.NORMAL, revoke=True)
        w = WorkerIdentity.objects.get(worker_id=self.SHADOW)
        self.assertEqual(w.status, WorkerIdentity.Status.REVOKED)
        self.assertFalse((w.worker_permissions or {}).get("shadow_worker"))
