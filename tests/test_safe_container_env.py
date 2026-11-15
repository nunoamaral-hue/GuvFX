"""Regression tests for scripts/safe_container_env.py — proving a secret value can NEVER be
emitted by the safe env probe (the defect that leaked two *_KEYRING production values).

Run: python3 -m unittest discover -s tests -p 'test_safe_container_env.py'
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, os.pardir, "scripts", "safe_container_env.py")
_spec = importlib.util.spec_from_file_location("safe_container_env", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

safe_env_lines = _mod.safe_env_lines
is_secret_name = _mod.is_secret_name

# A sentinel that must never appear in output. Not a real secret.
_LEAK = "LEAK_VALUE_MUST_NOT_APPEAR_0123456789"


class SafeContainerEnvTests(unittest.TestCase):
    def test_keyring_named_values_never_emitted(self):
        # The exact class of names that leaked: compound *_KEYRING.
        env = {
            "HOSTED_EXECUTOR_KEYRING": _LEAK,
            "BETA_AGENT_KEYRING": _LEAK,
            "GUVFX_GIT_COMMIT": "abc1234",
        }
        joined = "\n".join(safe_env_lines(sorted(env), env=env))
        self.assertNotIn(_LEAK, joined)
        self.assertIn("HOSTED_EXECUTOR_KEYRING=<redacted:secret-named>", joined)
        self.assertIn("BETA_AGENT_KEYRING=<redacted:secret-named>", joined)
        # a genuinely non-secret var is shown in full
        self.assertIn("GUVFX_GIT_COMMIT=abc1234", joined)

    def test_every_secret_token_is_redacted_anywhere_in_the_name(self):
        tokens = ["KEY", "KEYRING", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
                  "SIGNING", "HMAC", "PRIVATE", "CERT"]
        for tok in tokens:
            for name in (f"{tok}", f"X_{tok}", f"{tok}_Y", f"A_{tok}_B"):
                with self.subTest(name=name):
                    out = safe_env_lines([name], env={name: _LEAK})
                    self.assertNotIn(_LEAK, out[0], f"{name} leaked its value")
                    self.assertTrue(is_secret_name(name))

    def test_allow_list_only_never_dumps_full_env(self):
        # Only the requested name is considered; a non-requested secret is never emitted.
        env = {"GUVFX_GIT_COMMIT": "abc", "SOME_OTHER_SECRET": _LEAK, "DB_PASSWORD": _LEAK}
        joined = "\n".join(safe_env_lines(["GUVFX_GIT_COMMIT"], env=env))
        self.assertNotIn(_LEAK, joined)
        self.assertEqual(joined, "GUVFX_GIT_COMMIT=abc")

    def test_unset_name_is_marked_not_crashed(self):
        out = safe_env_lines(["DEFINITELY_NOT_SET_12345"], env={})
        self.assertEqual(out, ["DEFINITELY_NOT_SET_12345=<unset>"])

    def test_non_secret_names_pass_through(self):
        for name in ("GUVFX_GIT_COMMIT", "GUVFX_RELEASE_ID", "DB_HOST", "DB_NAME", "DB_PORT"):
            self.assertFalse(is_secret_name(name), name)


if __name__ == "__main__":
    unittest.main()
