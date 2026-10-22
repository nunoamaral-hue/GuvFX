"""ADR-0054 F-5 — per-operator executor login naming + lifecycle tests (positive + negative controls)."""
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from broker_intelligence.erasure_operators import (OperatorNamingError, operator_login_name,
                                                   validate_operator_login_name)


class OperatorNamingValidatorTests(TestCase):
    def test_builds_conforming_name(self):
        self.assertEqual(operator_login_name("op1"), "guvfx_erx_op1")
        self.assertEqual(operator_login_name("alice_2026"), "guvfx_erx_alice_2026")

    def test_rejects_bad_suffixes(self):
        for bad in ("Op1", "o p", "o';DROP ROLE x;--", "", "a-b", "é", "x" * 41):
            with self.assertRaises(OperatorNamingError, msg=f"accepted bad suffix {bad!r}"):
                operator_login_name(bad)

    def test_validate_full_name(self):
        self.assertEqual(validate_operator_login_name("guvfx_erx_op1"), "guvfx_erx_op1")

    def test_validate_rejects_nonconforming(self):
        for bad in ("op1", "guvfx_erx_", "guvfx_erx_OP", "guvfx_erasure_exec", "guvfx_erx_a;b",
                    "guvfx_erx_" + "x" * 41, "GUVFX_ERX_op"):
            with self.assertRaises(OperatorNamingError, msg=f"accepted bad name {bad!r}"):
                validate_operator_login_name(bad)


class MintErasureOperatorCommandTests(TestCase):
    def setUp(self):
        # The group the operator joins must exist (IN ROLE guvfx_erasure_exec).
        with connection.cursor() as cur:
            cur.execute("DROP ROLE IF EXISTS guvfx_erasure_exec")
            cur.execute("CREATE ROLE guvfx_erasure_exec NOLOGIN")
        self.addCleanup(self._drop, "guvfx_erasure_exec")

    def _drop(self, name):
        with connection.cursor() as cur:
            cur.execute(f'DROP ROLE IF EXISTS "{name}"')

    def test_non_conforming_suffix_refused_without_touching_db(self):
        with self.assertRaises(CommandError):
            call_command("mint_erasure_operator", "--suffix", "Bad!Name")
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'guvfx_erx_%'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_create_requires_password_env(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ERASURE_OPERATOR_PW", None)
            with self.assertRaises(CommandError):
                call_command("mint_erasure_operator", "--suffix", "op1")

    def test_mints_conforming_login_with_expiry_and_membership(self):
        with mock.patch.dict("os.environ", {"ERASURE_OPERATOR_PW": "disposable-" + "x" * 8}):
            call_command("mint_erasure_operator", "--suffix", "op1", "--valid-minutes", "30")
        self.addCleanup(self._drop, "guvfx_erx_op1")
        with connection.cursor() as cur:
            cur.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, "
                        "rolvaliduntil IS NOT NULL FROM pg_roles WHERE rolname='guvfx_erx_op1'")
            row = cur.fetchone()
            self.assertIsNotNone(row, "operator login was not created")
            canlogin, super_, createdb, createrole, bypassrls, has_expiry = row
            self.assertTrue(canlogin)
            self.assertFalse(super_ or createdb or createrole or bypassrls)  # least privilege
            self.assertTrue(has_expiry, "VALID UNTIL expiry not set")
            cur.execute("SELECT pg_has_role('guvfx_erx_op1', 'guvfx_erasure_exec', 'MEMBER')")
            self.assertTrue(cur.fetchone()[0], "operator is not a member of guvfx_erasure_exec")

    def test_revoke_drops_the_login(self):
        with mock.patch.dict("os.environ", {"ERASURE_OPERATOR_PW": "disposable-" + "y" * 8}):
            call_command("mint_erasure_operator", "--suffix", "op2")
        self.addCleanup(self._drop, "guvfx_erx_op2")
        call_command("mint_erasure_operator", "--suffix", "op2", "--revoke")
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname='guvfx_erx_op2'")
            self.assertEqual(cur.fetchone()[0], 0)
