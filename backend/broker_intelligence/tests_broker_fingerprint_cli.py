"""ADR-0099 (Lane A) — PARSER-LEVEL regression tests for the ``broker_fingerprint`` management command.

These exercise the REAL command argument parser and dispatch (not only the service functions), which is exactly
the gap that let the production ``--version`` argparse collision ship: the Lane-A suite tested
``fingerprint_onboarding`` directly and ``manage.py check`` never builds command parsers.

``test_parser_builds_without_collision`` fails against the original implementation (custom ``--version``), which
raised ``argparse.ArgumentError`` on parser construction. SYNTHETIC only — no production data, no mutation.

NB: a ``CommandParser`` built via ``create_parser`` raises ``CommandError`` (not ``SystemExit``) on a parse error
when it is not invoked from the command line; ``--help``/``--version`` still ``SystemExit(0)`` via ``parser.exit()``.
"""
from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from broker_intelligence.management.commands.broker_fingerprint import Command
from broker_intelligence.models import BrokerFingerprint


def _parser():
    return Command().create_parser("manage.py", "broker_fingerprint")


class ParserConstruction(SimpleTestCase):
    def test_parser_builds_without_collision(self):
        # Regression for the production defect: the original custom --version collided with Django's built-in
        # --version and raised argparse.ArgumentError HERE. Building the parser must now succeed.
        self.assertIsNotNone(_parser())

    def test_django_builtin_version_preserved(self):
        opts = {o for a in _parser()._actions for o in a.option_strings}
        self.assertIn("--version", opts)              # Django's framework-version option — not shadowed
        self.assertIn("--fingerprint-version", opts)  # our renamed fingerprint-schema version

    def test_django_version_action_is_builtin_not_our_int(self):
        # The --version action must be Django's VersionAction (store nothing in fp opts), never our fingerprint int.
        act = next(a for a in _parser()._actions if "--version" in a.option_strings)
        self.assertNotEqual(getattr(act, "dest", None), "fingerprint_version")

    def test_help_builds(self):
        # --help triggers argparse to print help and SystemExit(0); building/formatting it must not raise.
        with self.assertRaises(SystemExit) as cm:
            _parser().parse_args(["list", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_fingerprint_version_parses(self):
        ns = _parser().parse_args(["validate", "--name", "acme", "--fingerprint-version", "3"])
        self.assertEqual(ns.fingerprint_version, 3)
        self.assertEqual(ns.fp_action, "validate")

    def test_missing_action_fails_closed(self):
        with self.assertRaises(CommandError):          # fp_action is a required positional
            _parser().parse_args([])

    def test_unknown_action_rejected(self):
        with self.assertRaises(CommandError):
            _parser().parse_args(["frobnicate"])

    def test_invalid_fingerprint_version_rejected(self):
        for bad in ("0", "-1", "abc"):
            with self.assertRaises(CommandError):
                _parser().parse_args(["validate", "--name", "acme", "--fingerprint-version", bad])

    def test_invalid_target_version_rejected(self):
        for bad in ("0", "-5", "x"):
            with self.assertRaises(CommandError):
                _parser().parse_args(["rollback", "--name", "acme", "--target-version", bad])


class DispatchReachesHandlers(TestCase):
    """call_command dispatches to the intended action handler and stays fail-closed."""

    def _run(self, *args):
        out = StringIO()
        call_command("broker_fingerprint", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_list_reaches_handler_no_mutation(self):
        res = json.loads(self._run("list"))
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["fingerprints"], [])
        self.assertEqual(BrokerFingerprint.objects.count(), 0)   # list never creates anything

    def test_readonly_paths_never_certify_or_activate(self):
        # Read-only list + parser construction must leave the DARK zero-state untouched (invariant: no parser path
        # certifies or activates automatically).
        self._run("list")
        _parser()
        self.assertEqual(BrokerFingerprint.objects.filter(certified_capability=True).count(), 0)
        self.assertEqual(BrokerFingerprint.objects.filter(is_active=True).count(), 0)

    def test_certify_reaches_handler_fail_closed(self):
        # Dispatches to certify → resolve_fp → CommandError (no such fingerprint). Proves the handler was reached and
        # nothing was certified.
        with self.assertRaises(CommandError) as cm:
            self._run("certify", "--name", "acme", "--fingerprint-version", "1", "--actor", "user:1",
                      "--reason", "r", "--ticket", "T")
        self.assertIn("no fingerprint", str(cm.exception).lower())
        self.assertEqual(BrokerFingerprint.objects.filter(certified_capability=True).count(), 0)

    def test_activate_reaches_handler_fail_closed(self):
        with self.assertRaises(CommandError) as cm:
            self._run("activate", "--name", "acme", "--fingerprint-version", "1", "--actor", "user:1")
        self.assertIn("no fingerprint", str(cm.exception).lower())
        self.assertEqual(BrokerFingerprint.objects.filter(is_active=True).count(), 0)

    def test_validate_reaches_handler_requires_name_and_version(self):
        with self.assertRaises(CommandError) as cm:
            self._run("validate")
        self.assertIn("--name", str(cm.exception))
        self.assertIn("--fingerprint-version", str(cm.exception))

    def test_rollback_reaches_handler_requires_name_and_target(self):
        with self.assertRaises(CommandError) as cm:
            self._run("rollback", "--name", "acme")   # missing --target-version
        self.assertIn("--target-version", str(cm.exception))

    def test_seed_reaches_handler_and_is_idempotent(self):
        # seed is the ONE explicit mutating action (operator-invoked). Whatever it seeds against the DB's active
        # broker profiles is certified+active by design; a re-seed of the SAME content is idempotent (ALREADY_SEEDED).
        first = json.loads(self._run("seed"))["seeded"]
        self.assertTrue(first)   # handler reached; per-broker results returned
        seeded = [r["name"] for r in first if r["status"] == "SEEDED"]
        if seeded:   # only assert idempotency for brokers that actually seeded (depends on active profiles)
            for r in first:
                if r["status"] == "SEEDED":
                    self.assertTrue(r["certified"] and r["active"] and r["checksum_match"])
            second = {r["name"]: r for r in json.loads(self._run("seed"))["seeded"]}
            for nm in seeded:
                self.assertEqual(second[nm]["status"], "ALREADY_SEEDED")
