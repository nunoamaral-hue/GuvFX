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


if __name__ == "__main__":
    unittest.main()
