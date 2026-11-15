"""Functional tests for the account-46 one-shot attestation SINGLE-ENTRY wrapper
(deploy/copier-emitter/operator/provision_and_open_oneshot.py).

Proves, with SSH + getpass fully mocked (SYNTHETIC key, never a real one):
  * the operator's single hidden entry is delivered BYTE-IDENTICALLY to both the host provisioner
    (-KeyFromStdin, as "<phrase>\\n<key>") and the backend verifier window (--open, as "<key>");
  * the key is NEVER placed in argv (only on stdin);
  * fail-closed: a provision/verify/window failure never leaves a window open, and shreds the host key.

Run: python3 -m unittest discover -s tests -p 'test_provision_and_open_oneshot.py'
"""
import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WRAP = os.path.join(_HERE, os.pardir, "deploy", "copier-emitter", "operator", "provision_and_open_oneshot.py")
_spec = importlib.util.spec_from_file_location("provision_and_open_oneshot", _WRAP)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

SYNTH = "SYNTH_ONESHOT_KEY_0123456789abcdef_pad"   # synthetic, >=32 chars; NOT a real key
ARGV = ["--windows-ssh", "win@host", "--backend-ssh", "ubuntu@vps", "--correlation-id", "t1"]


class _Recorder:
    """Replaces module._ssh; returns canned (stdout,stderr,rc) per matched remote command, and records calls."""
    def __init__(self, responses):
        self.responses = responses      # list of (match_substr, stdout_obj) in priority order
        self.calls = []                 # list of dicts: {target, argv, key_stdin}

    def __call__(self, target, remote_argv, *, key_stdin=None, timeout=90):
        self.calls.append({"target": target, "argv": list(remote_argv), "key_stdin": key_stdin})
        joined = " ".join(remote_argv)
        for sub, out in self.responses:
            if sub in joined:
                body = out if isinstance(out, str) else json.dumps(out)
                return (body, "", 0)
        return ("{}", "", 0)

    def key_sent_to(self, sub):
        for c in self.calls:
            if sub in " ".join(c["argv"]):
                return c["key_stdin"]
        return "__NO_CALL__"


def _run(recorder, keys=(SYNTH, SYNTH)):
    """Run main() with _ssh=recorder and getpass returning keys in order."""
    orig_ssh, orig_getpass = _mod._ssh, _mod.getpass.getpass
    seq = list(keys)
    _mod._ssh = recorder
    _mod.getpass.getpass = lambda *a, **k: seq.pop(0)
    try:
        return _mod.main(ARGV)
    finally:
        _mod._ssh = orig_ssh
        _mod.getpass.getpass = orig_getpass


_HAPPY = [
    ("copier_verifier_window --status", {"action": "status", "status": {"open": False}}),
    ("-KeyFromStdin", {"ok": True, "action": "provision", "reason": "provisioned", "acl_verified": True}),
    ("-Verify", {"ok": True, "action": "verify", "acl_verified": True, "reason": "verified"}),
    ("copier_verifier_window --open", {"action": "open", "window_id": 7, "purpose": "ATTEST",
                                       "max_uses": 1, "expires_at": "z", "status": {"open": True}}),
    ("-Remove", {"ok": True, "action": "remove", "reason": "removed"}),
]

# The backend ``copier_verifier_window`` command emits PRETTY-printed multi-line JSON (json.dumps indent=1).
# These are byte-for-byte the live-captured shapes. The reviewed _HAPPY above uses json.dumps (compact),
# which is exactly why the earlier line-by-line parser passed CI yet false-refused in production.
_PRETTY_STATUS_OPEN_FALSE = '{\n "action": "status",\n "status": {\n  "open": false\n }\n}'
_PRETTY_OPEN_RESULT = ('{\n "action": "open",\n "window_id": 7,\n "purpose": "ATTEST",\n "max_uses": 1,\n'
                       ' "expires_at": "z",\n "key_file_written": true,\n "status": {\n  "open": true\n }\n}')
_HAPPY_PRETTY = [
    ("copier_verifier_window --status", _PRETTY_STATUS_OPEN_FALSE),   # pretty multi-line, like prod
    ("-KeyFromStdin", {"ok": True, "action": "provision", "reason": "provisioned", "acl_verified": True}),
    ("-Verify", {"ok": True, "action": "verify", "acl_verified": True, "reason": "verified"}),
    ("copier_verifier_window --open", _PRETTY_OPEN_RESULT),           # pretty multi-line, like prod
    ("-Remove", {"ok": True, "action": "remove", "reason": "removed"}),
]


class SingleEntryWrapperTests(unittest.TestCase):
    def test_happy_path_same_key_to_both_and_never_argv(self):
        rec = _Recorder(_HAPPY)
        rc = _run(rec)
        self.assertEqual(rc, 0)
        # provisioner got "<phrase>\n<key>"; window got exactly "<key>"
        prov_stdin = rec.key_sent_to("-KeyFromStdin")
        win_stdin = rec.key_sent_to("copier_verifier_window --open")
        self.assertEqual(prov_stdin, _mod.CONFIRM_PHRASE + "\n" + SYNTH)
        self.assertEqual(win_stdin, SYNTH)
        # the key bytes delivered to each side are IDENTICAL (the whole point)
        self.assertEqual(prov_stdin.split("\n", 1)[1], win_stdin)
        # the key is NEVER in argv of ANY call
        for c in rec.calls:
            self.assertNotIn(SYNTH, " ".join(c["argv"]), "key must never appear in argv")

    def test_key_mismatch_aborts_before_any_secret_delivery(self):
        rec = _Recorder(_HAPPY)
        rc = _run(rec, keys=(SYNTH, SYNTH + "x"))   # confirm differs
        self.assertEqual(rc, 2)
        # only the preflight --status call happened; no key was ever sent
        self.assertNotIn("__NO_CALL__", "")  # sanity
        self.assertEqual(rec.key_sent_to("-KeyFromStdin"), "__NO_CALL__")
        self.assertEqual(rec.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")

    def test_refuses_when_window_already_open(self):
        rec = _Recorder([("copier_verifier_window --status", {"action": "status", "status": {"open": True}})])
        rc = _run(rec)
        self.assertEqual(rc, 2)
        self.assertEqual(rec.key_sent_to("-KeyFromStdin"), "__NO_CALL__")

    def test_provision_failure_never_opens_window(self):
        resp = [r for r in _HAPPY if "-KeyFromStdin" not in r[0]]
        resp.append(("-KeyFromStdin", {"ok": False, "reason": "key_file_exists"}))
        rec = _Recorder(resp)
        rc = _run(rec)
        self.assertEqual(rc, 1)
        self.assertEqual(rec.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")

    def test_verify_failure_shreds_and_no_window(self):
        resp = [r for r in _HAPPY if "-Verify" not in r[0]]
        resp.append(("-Verify", {"ok": False, "acl_verified": False, "reason": "verify_failed"}))
        rec = _Recorder(resp)
        rc = _run(rec)
        self.assertEqual(rc, 1)
        self.assertEqual(rec.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")
        # -Remove was invoked to shred the unverified key
        self.assertTrue(any("-Remove" in " ".join(c["argv"]) for c in rec.calls))

    def test_window_failure_shreds_host_key(self):
        resp = [r for r in _HAPPY if "copier_verifier_window --open" not in r[0]]
        resp.append(("copier_verifier_window --open", {"action": "rejected", "status": {"open": False}}))
        rec = _Recorder(resp)
        rc = _run(rec)
        self.assertEqual(rc, 1)
        # after a failed window open, the host key is shredded (at least one -Remove AFTER the open attempt)
        opened = [i for i, c in enumerate(rec.calls) if "copier_verifier_window --open" in " ".join(c["argv"])]
        removed_after = [i for i, c in enumerate(rec.calls) if "-Remove" in " ".join(c["argv"]) and i > opened[0]]
        self.assertTrue(removed_after, "a window-open failure must shred the host key")

    def test_helpers_do_not_embed_key(self):
        pc = _mod._win_provision_cmd("P.ps1", "-KeyFromStdin")
        self.assertIn("-File", pc)
        self.assertIn("-KeyFromStdin", pc)
        self.assertNotIn(SYNTH, " ".join(pc))
        wc = _mod._backend_window_cmd("c", "python", "--open")
        self.assertEqual(wc[:3], ["docker", "exec", "-i"])


class PrettyOutputRegressionTests(unittest.TestCase):
    """Regression for the production failure: the backend emits PRETTY multi-line JSON. The whole
    single-entry flow must proceed through the preflight (and the window-open result) with pretty output."""

    def test_preflight_accepts_real_pretty_status_and_proceeds(self):
        rec = _Recorder(_HAPPY_PRETTY)
        rc = _run(rec)
        self.assertEqual(rc, 0, "pretty-printed --status must pass preflight, not false-refuse")
        # the single key still reached BOTH sides byte-identically
        self.assertEqual(rec.key_sent_to("-KeyFromStdin"), _mod.CONFIRM_PHRASE + "\n" + SYNTH)
        self.assertEqual(rec.key_sent_to("copier_verifier_window --open"), SYNTH)

    def test_pretty_open_showing_still_open_is_accepted(self):
        # the window-open result is also pretty; success requires action==open AND status.open True
        rec = _Recorder(_HAPPY_PRETTY)
        self.assertEqual(_run(rec), 0)

    def test_pretty_status_open_true_still_refuses(self):
        pretty_open_true = '{\n "action": "status",\n "status": {\n  "open": true\n }\n}'
        rec = _Recorder([("copier_verifier_window --status", pretty_open_true)])
        rc = _run(rec)
        self.assertEqual(rc, 2, "an already-open window (pretty) must still refuse")
        self.assertEqual(rec.key_sent_to("-KeyFromStdin"), "__NO_CALL__")


class LastJsonParserTests(unittest.TestCase):
    """_last_json behaviour matrix: parse compact AND pretty multi-line, tolerate a LEADING banner,
    and FAIL CLOSED (None) on empty / garbage / partial / ambiguous-multiple / trailing-garbage / non-object."""

    def test_compact_single_line(self):
        self.assertEqual(_mod._last_json('{"action":"status","status":{"open":false}}'),
                         {"action": "status", "status": {"open": False}})

    def test_pretty_multiline_real_shape(self):
        pretty = '{\n "action": "status",\n "status": {\n  "open": false\n }\n}'
        self.assertEqual(_mod._last_json(pretty), {"action": "status", "status": {"open": False}})
        self.assertIs(_mod._last_json(pretty).get("status", {}).get("open"), False)

    def test_pretty_with_trailing_newline_ok(self):
        self.assertEqual(_mod._last_json('{\n "ok": true\n}\n'), {"ok": True})

    def test_leading_banner_then_pretty_json_ok(self):
        self.assertEqual(_mod._last_json('MOTD\nWARNING: pq banner\n{\n "status": {\n  "open": false\n }\n}'),
                         {"status": {"open": False}})

    def test_compact_windows_verify_ok(self):
        v = '{"ok":true,"action":"verify","acl_verified":true,"reason":"verified"}'
        self.assertIs(_mod._last_json(v).get("acl_verified"), True)

    def test_empty_and_whitespace_are_none(self):
        self.assertIsNone(_mod._last_json(""))
        self.assertIsNone(_mod._last_json("   \n\t  "))

    def test_garbage_is_none(self):
        self.assertIsNone(_mod._last_json("could not connect to docker daemon"))

    def test_partial_truncated_is_none(self):
        self.assertIsNone(_mod._last_json('{"status": {"open":'))
        self.assertIsNone(_mod._last_json('banner\n{"status": {"open":'))

    def test_two_payloads_ambiguous_is_none(self):
        self.assertIsNone(_mod._last_json('{"action":"status","status":{"open":false}}'
                                          '{"action":"status","status":{"open":true}}'))
        self.assertIsNone(_mod._last_json('{\n "a": 1\n}\n{\n "b": 2\n}'))

    def test_trailing_garbage_after_value_is_none(self):
        self.assertIsNone(_mod._last_json('{"status":{"open":false}} rogue-trailing'))
        self.assertIsNone(_mod._last_json('{"status":{"open":false}}\n{"injected":true} x'))

    def test_non_object_top_level_is_none(self):
        # a bare scalar / array is valid JSON but not a command result; must not reach a caller's .get()
        self.assertIsNone(_mod._last_json('true'))
        self.assertIsNone(_mod._last_json('123'))
        self.assertIsNone(_mod._last_json('[{"open": false}]'))

    def test_leading_parseable_token_then_object_fails_closed(self):
        # a leading job-control-style token that is itself valid JSON (e.g. "[1]") makes the output
        # ambiguous (two top-level values) -> fail closed. The driven BatchMode-SSH/docker commands never
        # emit such banners, but the refusal is safe: it can only refuse, never wrongly accept a gate.
        self.assertIsNone(_mod._last_json('[1] {"status": {"open": false}}'))
        self.assertIsNone(_mod._last_json('[1]\n{"status": {"open": false}}'))


if __name__ == "__main__":
    unittest.main()
