"""Functional tests for the account-46 STANDING ingest-key SINGLE-ENTRY wrapper
(deploy/copier-emitter/operator/provision_and_open_ingest.py).

Proves, with ssh + getpass fully mocked (SYNTHETIC key only, never a real one):
  * ONE hidden entry is delivered BYTE-IDENTICALLY to the host provisioner (-KeyFromStdin, as
    "<phrase>\\n<key>") and the backend verifier window (--open, as "<key>");
  * the uncapped INGEST window is opened with --purpose ingest --max-uses 0 and LEFT OPEN on success;
  * the key is NEVER placed in argv/env/stdout/stderr;
  * fail-closed: every non-success path cleans up BOTH sides and INDEPENDENTLY verifies a DARK result, or
    reports CLEANUP_INCOMPLETE (never a false "rolled back");
  * the wrapper enables/triggers NO task/flag/arm and NEVER references account 48.

Run: python3 -m unittest discover -s tests -p 'test_provision_and_open_ingest.py'
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WRAP = os.path.join(_HERE, os.pardir, "deploy", "copier-emitter", "operator", "provision_and_open_ingest.py")
_spec = importlib.util.spec_from_file_location("provision_and_open_ingest", _WRAP)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

SYNTH = "SYNTH_INGEST_KEY_0123456789abcdef_pad"   # synthetic, >=32 chars; NOT a real key
ARGV = ["--windows-ssh", "win@host", "--backend-ssh", "ubuntu@vps", "--correlation-id", "t1"]


class HostBackend:
    """Stateful ssh replacement modelling the real host + backend: -Verify reflects whether a valid standing
    key is currently present; the window reflects open/close. Records every call and lets a test inject a
    failure at any step."""

    def __init__(self, *, window_open=False, key_present=False, provision_ok=True,
                 verify_after_provision_ok=True, window_open_ok=True, remove_effective=True,
                 close_effective=True, raise_on=None, status_override=None, open_override=None,
                 open_malformed=False, open_foreign=False, open_capped=False):
        self.window_open = window_open
        self.key_present = key_present
        self.provision_ok = provision_ok
        self.verify_after_provision_ok = verify_after_provision_ok
        self.window_open_ok = window_open_ok
        self.remove_effective = remove_effective
        self.close_effective = close_effective
        self.raise_on = raise_on
        self.status_override = status_override        # raw string to return for --status (pretty-JSON tests)
        self.open_override = open_override            # raw string to return for --open
        # open_malformed: the backend REALLY opens the window but returns unparseable JSON, so the wrapper
        # treats the open as failed and must then close the genuinely-open window during cleanup.
        self.open_malformed = open_malformed
        # open_foreign: a concurrent operator already holds a window; --open is refused on STDERR (we must not
        # clobber theirs). open_capped: the backend opened a USE-CAPPED window (wrong shape -> must be refused).
        self.open_foreign = open_foreign
        self.open_capped = open_capped
        self.calls = []

    def __call__(self, target, remote_argv, *, key_stdin=None, timeout=90):
        self.calls.append({"target": target, "argv": list(remote_argv), "key_stdin": key_stdin})
        joined = " ".join(remote_argv)
        if self.raise_on is not None and self.raise_on in joined:
            raise subprocess.TimeoutExpired(cmd=remote_argv, timeout=timeout)
        if "-KeyFromStdin" in joined:
            if self.provision_ok:
                self.key_present = True
                return (json.dumps({"ok": True, "action": "provision", "reason": "provisioned",
                                    "key_file_present": True, "acl_verified": True}), "", 0)
            return (json.dumps({"ok": False, "action": "provision", "reason": "task_contract_invalid",
                                "key_file_present": False}), "", 0)
        if "-Verify" in joined:
            if not self.key_present:
                return (json.dumps({"ok": False, "action": "verify", "acl_verified": False,
                                    "key_file_present": False, "reason": "key_file_absent"}), "", 0)
            valid = self.verify_after_provision_ok   # present-but-invalid when False (ACL/task drift)
            return (json.dumps({"ok": valid, "action": "verify", "acl_verified": valid,
                                "key_file_present": True,
                                "reason": "verified" if valid else "verify_failed"}), "", 0)
        if "-Remove" in joined:
            if self.remove_effective:
                self.key_present = False
                return (json.dumps({"ok": True, "action": "remove", "key_file_present": False,
                                    "reason": "removed"}), "", 0)
            # -Remove ran but the file is still on disk -> authoritative not-absent signal (ok tied to presence).
            return (json.dumps({"ok": False, "action": "remove", "key_file_present": True,
                                "reason": "remove_failed"}), "", 0)
        if "copier_verifier_window" in joined:
            if "--status" in joined:
                if self.status_override is not None:
                    return (self.status_override, "", 0)
                return (json.dumps({"action": "status", "status": {"open": self.window_open}}), "", 0)
            if "--open" in joined:
                if self.open_foreign:
                    # a concurrent operator already holds a window -> backend refuses on STDERR, exits non-zero
                    return ("", "CommandError: refused to open window: window_already_open\n", 1)
                if self.open_malformed:
                    self.window_open = True            # the backend really opened it...
                    return ('{"action":"open"', "", 0)  # ...but returned truncated JSON -> parse fails
                if self.open_capped:
                    self.window_open = True            # opened, but USE-CAPPED (wrong shape for the arm)
                    return (json.dumps({"action": "open", "window_id": 6, "purpose": "INGEST",
                                        "max_uses": 1, "expires_at": "z", "status": {"open": True}}), "", 0)
                if self.open_override is not None:
                    return (self.open_override, "", 0)
                if self.window_open_ok:
                    self.window_open = True
                    return (json.dumps({"action": "open", "window_id": 5, "purpose": "INGEST",
                                        "max_uses": None, "expires_at": "2026-11-17T18:00:00+00:00",
                                        "key_file_written": True, "status": {"open": True}}), "", 0)
                return (json.dumps({"action": "rejected", "status": {"open": False}}), "", 0)
            if "--close" in joined:
                if self.close_effective:
                    self.window_open = False
                return (json.dumps({"action": "close", "closed_windows": 1,
                                    "status": {"open": self.window_open}}), "", 0)
        return ("{}", "", 0)

    def key_sent_to(self, sub):
        for c in self.calls:
            if sub in " ".join(c["argv"]):
                return c["key_stdin"]
        return "__NO_CALL__"

    def count(self, sub):
        return sum(1 for c in self.calls if sub in " ".join(c["argv"]))


def _run(hb, keys=(SYNTH, SYNTH), argv=ARGV):
    """Run main() with _ssh=hb and getpass returning keys in order; capture stdout. Returns (rc, stdout)."""
    orig_ssh, orig_getpass = _mod._ssh, _mod.getpass.getpass
    seq = list(keys)
    _mod._ssh = hb
    _mod.getpass.getpass = lambda *a, **k: (seq.pop(0) if seq else "")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _mod.main(argv)
    finally:
        _mod._ssh = orig_ssh
        _mod.getpass.getpass = orig_getpass
    return rc, buf.getvalue()


class HappyPathTests(unittest.TestCase):
    def test_same_key_to_both_never_argv_window_left_open(self):
        hb = HostBackend()
        rc, out = _run(hb)
        self.assertEqual(rc, 0, out)
        # provisioner got "<phrase>\n<key>"; window got exactly "<key>"
        prov_stdin = hb.key_sent_to("-KeyFromStdin")
        win_stdin = hb.key_sent_to("copier_verifier_window --open")
        self.assertEqual(prov_stdin, _mod.CONFIRM_PHRASE + "\n" + SYNTH)
        self.assertEqual(win_stdin, SYNTH)
        # identical bytes to both (the whole point)
        self.assertEqual(prov_stdin.split("\n", 1)[1], win_stdin)
        # the key is NEVER in argv of ANY call
        for c in hb.calls:
            self.assertNotIn(SYNTH, " ".join(c["argv"]), "key must never appear in argv")
        # result is uncapped INGEST, left open; no key in output
        res = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(res["ok"])
        self.assertEqual(res["purpose"], "INGEST")
        self.assertTrue(res["uncapped"])
        self.assertIsNone(res["max_uses"])
        self.assertTrue(res["window_open"])
        self.assertEqual(res["account_id"], 46)
        self.assertTrue(res["host_key_provisioned"])
        self.assertNotIn(SYNTH, out)
        # success -> no cleanup: the window was NOT closed and the host key NOT removed after the open
        self.assertTrue(hb.window_open, "success must leave the window OPEN")
        self.assertTrue(hb.key_present, "success must leave the standing host key in place")
        self.assertEqual(hb.count("copier_verifier_window --close"), 0)

    def test_open_uses_purpose_ingest_and_max_uses_0(self):
        hb = HostBackend()
        _run(hb)
        open_calls = [c for c in hb.calls if "copier_verifier_window --open" in " ".join(c["argv"])]
        self.assertEqual(len(open_calls), 1)
        joined = " ".join(open_calls[0]["argv"])
        self.assertIn("--purpose ingest", joined)
        self.assertIn("--max-uses 0", joined)
        self.assertIn("--ttl 900", joined)

    def test_default_ttl_within_bound(self):
        self.assertLessEqual(_mod.DEFAULT_TTL, 900)
        self.assertGreaterEqual(_mod.DEFAULT_TTL, 30)

    def test_pretty_multiline_backend_json_still_succeeds(self):
        pretty_status = '{\n "action": "status",\n "status": {\n  "open": false\n }\n}'
        pretty_open = ('{\n "action": "open",\n "window_id": 5,\n "purpose": "INGEST",\n "max_uses": null,\n'
                       ' "expires_at": "z",\n "key_file_written": true,\n "status": {\n  "open": true\n }\n}')
        hb = HostBackend(status_override=pretty_status, open_override=pretty_open)
        rc, out = _run(hb)
        self.assertEqual(rc, 0, out)
        self.assertEqual(hb.key_sent_to("copier_verifier_window --open"), SYNTH)


class PreflightRefusalTests(unittest.TestCase):
    def test_refuses_when_window_already_open(self):
        hb = HostBackend(window_open=True)
        rc, out = _run(hb)
        self.assertEqual(rc, 2)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")
        self.assertIn("window_already_open", out)

    def test_refuses_when_standing_key_already_present(self):
        hb = HostBackend(key_present=True)
        rc, out = _run(hb)
        self.assertEqual(rc, 2)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")
        self.assertIn("standing_host_key_already_present", out)

    def test_refuses_when_standing_key_present_but_invalid(self):
        # a present-but-unverifiable key (ACL/task drift: -Verify ok:false, reason != key_file_absent) must be
        # REFUSED, not silently clobbered -> fail closed before key entry.
        hb = HostBackend(key_present=True, verify_after_provision_ok=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 2)
        self.assertIn("host_key_state_ambiguous", out)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")

    def test_refuses_when_window_status_unreadable(self):
        hb = HostBackend(status_override="could not connect to docker daemon")
        rc, out = _run(hb)
        self.assertEqual(rc, 2)
        self.assertIn("window_status_unreadable", out)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")

    def test_refuses_when_ttl_out_of_bounds(self):
        hb = HostBackend()
        rc, out = _run(hb, argv=ARGV + ["--ttl", "1200"])
        self.assertEqual(rc, 2)
        self.assertIn("ttl_out_of_bounds", out)
        # no host/backend contact at all on a bad ttl
        self.assertEqual(hb.calls, [])


class HiddenEntryTests(unittest.TestCase):
    def test_key_mismatch_aborts_before_any_secret_delivery(self):
        hb = HostBackend()
        rc, out = _run(hb, keys=(SYNTH, SYNTH + "x"))
        self.assertEqual(rc, 2)
        self.assertIn("key_mismatch", out)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")
        self.assertEqual(hb.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")

    def test_short_key_aborts(self):
        hb = HostBackend()
        rc, out = _run(hb, keys=("short", "short"))
        self.assertEqual(rc, 2)
        self.assertIn("key_too_short", out)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")

    def test_non_ascii_key_aborts(self):
        bad = "k" * 31 + chr(233)   # 32 chars incl. a non-ASCII char (ord 233 > 126) -> must be rejected
        hb = HostBackend()
        rc, out = _run(hb, keys=(bad, bad))
        self.assertEqual(rc, 2)
        self.assertIn("key_not_printable_ascii_or_has_space", out)
        self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")

    def test_space_padded_key_rejected_before_delivery(self):
        # the backend stores key.strip() while the host provisioner keeps surrounding spaces -> divergence.
        # a space anywhere must be rejected BEFORE any secret delivery (prevents the silent bad_signature).
        for bad in ("  " + "a" * 40 + "  ", "a" * 20 + " " + "b" * 20):
            hb = HostBackend()
            rc, out = _run(hb, keys=(bad, bad))
            self.assertEqual(rc, 2, bad)
            self.assertIn("key_not_printable_ascii_or_has_space", out)
            self.assertEqual(hb.key_sent_to("-KeyFromStdin"), "__NO_CALL__")
            self.assertEqual(hb.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")


class FailClosedCleanupTests(unittest.TestCase):
    def test_provision_failure_no_window_and_clean(self):
        hb = HostBackend(provision_ok=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 1)
        self.assertEqual(hb.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")
        res = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "host_provision_or_verify_failed")
        self.assertEqual(res["cleanup"], "verified_dark")

    def test_verify_failure_shreds_and_no_window(self):
        hb = HostBackend(verify_after_provision_ok=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 1)
        self.assertEqual(hb.key_sent_to("copier_verifier_window --open"), "__NO_CALL__")
        self.assertTrue(hb.count("-Remove") >= 1)
        self.assertFalse(hb.key_present)

    def test_window_open_failure_cleans_both_sides(self):
        hb = HostBackend(window_open_ok=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 1)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["reason"], "window_open_failed")
        self.assertEqual(res["cleanup"], "verified_dark")
        # both sides are DARK after cleanup
        self.assertFalse(hb.window_open)
        self.assertFalse(hb.key_present)

    def test_partial_host_then_backend_failure_removes_host_key(self):
        hb = HostBackend(window_open_ok=False)
        _run(hb)
        # the host key was provisioned, then removed in cleanup (a -Remove AFTER the open attempt)
        idx_open = [i for i, c in enumerate(hb.calls) if "copier_verifier_window --open" in " ".join(c["argv"])]
        removed_after = [i for i, c in enumerate(hb.calls)
                         if "-Remove" in " ".join(c["argv"]) and i > idx_open[0]]
        self.assertTrue(removed_after, "a window-open failure must shred the host key")
        self.assertFalse(hb.key_present)

    def test_ssh_failure_fails_closed_into_cleanup(self):
        hb = HostBackend(raise_on="-KeyFromStdin")
        rc, out = _run(hb)
        self.assertIn(rc, (1, 3))
        res = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"].startswith("ssh_or_runtime_error"))

    def test_cleanup_close_failure_reports_incomplete(self):
        # the backend REALLY opened the window but returned malformed JSON (wrapper -> failure); then the
        # cleanup close does not take, so the independent check still sees an open window -> CLEANUP_INCOMPLETE.
        hb = HostBackend(open_malformed=True, close_effective=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 3)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["cleanup"], "CLEANUP_INCOMPLETE")
        self.assertIn("remediation", res)
        self.assertIs(res["detail"]["window_closed"], False)

    def test_cleanup_host_remove_failure_reports_incomplete(self):
        hb = HostBackend(window_open_ok=False, remove_effective=False)
        rc, out = _run(hb)
        self.assertEqual(rc, 3)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["cleanup"], "CLEANUP_INCOMPLETE")

    def test_cleanup_absence_signal_is_remove_not_verify(self):
        # HIGH regression: -Verify can report ok:false WITH the key file still on disk (task-contract drift sets
        # key_file_present only AFTER Assert-TaskContract). The cleanup MUST decide absence from the -Remove
        # result (ok True / key_file_present False), which has no task-contract gate.
        import types
        a = types.SimpleNamespace(backend_ssh="b", backend_container="guvfx-backend", remote_python="python")
        clean, detail = _mod._verify_cleanup(a, {"ok": True, "key_file_present": False}, foreign_window=True)
        self.assertTrue(clean)
        self.assertTrue(detail["host_key_absent"])
        bad, detail2 = _mod._verify_cleanup(a, {"ok": False, "key_file_present": True}, foreign_window=True)
        self.assertFalse(bad, "a -Remove that left the file on disk must NOT be reported clean")
        none_res, detail3 = _mod._verify_cleanup(a, None, foreign_window=True)
        self.assertFalse(none_res, "an unreadable -Remove result must NOT be reported clean")

    def test_foreign_concurrent_window_not_clobbered(self):
        # a concurrent operator opened a window in the preflight->open gap: our open is refused on stderr. We
        # must shred OUR host key but must NOT close THEIR window.
        hb = HostBackend(open_foreign=True)
        rc, out = _run(hb)
        self.assertEqual(rc, 1)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["reason"], "window_already_open_concurrent")
        self.assertEqual(res["cleanup"], "verified_dark")
        self.assertEqual(res["detail"]["window"], "foreign_left_intact")
        self.assertEqual(hb.count("copier_verifier_window --close"), 0, "must NOT close a foreign window")
        self.assertFalse(hb.key_present, "our own host key must still be shredded")

    def test_capped_window_shape_refused_and_cleaned(self):
        # defence-in-depth: the backend returned a USE-CAPPED window; the arm would reject it, so the wrapper
        # must treat it as a failure and tear it down rather than leave it staged as 'uncapped'.
        hb = HostBackend(open_capped=True)
        rc, out = _run(hb)
        self.assertEqual(rc, 1)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["reason"], "window_unexpected_shape")
        self.assertFalse(hb.window_open, "the wrong-shape window must be closed")
        self.assertFalse(hb.key_present)

    def test_user_interrupt_cleans_up(self):
        class _Interrupt(HostBackend):
            def __call__(self, target, remote_argv, *, key_stdin=None, timeout=90):
                if "-KeyFromStdin" in " ".join(remote_argv):
                    raise KeyboardInterrupt()
                return super().__call__(target, remote_argv, key_stdin=key_stdin, timeout=timeout)
        hb = _Interrupt()
        rc, out = _run(hb)
        self.assertIn(rc, (1, 3))
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["reason"], "interrupted")


class SecretHygieneTests(unittest.TestCase):
    def test_no_secret_in_stdout_any_path(self):
        for hb in (HostBackend(), HostBackend(window_open_ok=False), HostBackend(provision_ok=False),
                   HostBackend(verify_after_provision_ok=False)):
            _rc, out = _run(hb)
            self.assertNotIn(SYNTH, out, "key must never reach stdout")

    def test_key_only_on_stdin_never_argv_or_env(self):
        hb = HostBackend()
        _run(hb)
        for c in hb.calls:
            self.assertNotIn(SYNTH, " ".join(c["argv"]))
        # the key is passed ONLY via key_stdin to exactly two calls (provision + open)
        with_key = [c for c in hb.calls if c["key_stdin"] and SYNTH in c["key_stdin"]]
        self.assertEqual(len(with_key), 2)

    def test_wrapper_is_ascii_and_no_key_param(self):
        with open(_WRAP, "rb") as _fh:
            data = _fh.read()
        self.assertTrue(all(b <= 0x7F for b in data), "operator wrapper must be ASCII-only (RULE 9)")
        head = data.decode("utf-8")
        for bad in ["--key", "--secret", "--password", "--ingest-key"]:
            self.assertNotIn(bad, head, "wrapper must never take a key/secret as an argument")


class ScopeTests(unittest.TestCase):
    def test_never_targets_48_task_flag_arm_or_broker(self):
        hb = HostBackend()
        _run(hb)                       # happy path exercises provision + open
        hb2 = HostBackend(window_open_ok=False)
        _run(hb2)                      # failure path exercises cleanup
        for c in hb.calls + hb2.calls:
            joined = " ".join(c["argv"]).lower()
            for bad in [" 48", "_48", "enable-scheduledtask", "register-scheduledtask", "schtasks",
                        "arm_copier", "grant_copier", "run_copier", "request_copier", "provision_authorized",
                        "restore_copier", "execute", "place_order", "broker_order", "mutation", "live_execute"]:
                self.assertNotIn(bad, joined, "wrapper command must not reference %r: %s" % (bad, joined))

    def test_only_manage_subcommand_is_verifier_window(self):
        hb = HostBackend(window_open_ok=False)
        _run(hb)
        subs = set()
        for c in hb.calls:
            if "manage.py" in c["argv"]:
                subs.add(c["argv"][c["argv"].index("manage.py") + 1])
        self.assertEqual(subs, {"copier_verifier_window"})

    def test_only_powershell_file_is_the_emitter_provisioner(self):
        hb = HostBackend()
        _run(hb)
        for c in hb.calls:
            if "-File" in c["argv"]:
                path = c["argv"][c["argv"].index("-File") + 1]
                self.assertTrue(path.endswith("Provision-GuvfxCopierEmitterKey.ps1"), path)

    def test_helpers_never_embed_key(self):
        pc = _mod._win_provision_cmd("P.ps1", "-KeyFromStdin")
        self.assertIn("-File", pc)
        self.assertNotIn(SYNTH, " ".join(pc))
        wc = _mod._backend_window_cmd("c", "python", "--open")
        self.assertEqual(wc[:3], ["docker", "exec", "-i"])
        self.assertEqual(_mod.CONFIRM_PHRASE, "provision-emitter-key-account-46")


class LastJsonParserTests(unittest.TestCase):
    """_last_json behaviour matrix (copied parser): compact AND pretty multi-line, tolerate a LEADING banner,
    FAIL CLOSED (None) on empty / garbage / partial / ambiguous / trailing-garbage / non-object."""

    def test_compact_single_line(self):
        self.assertEqual(_mod._last_json('{"action":"status","status":{"open":false}}'),
                         {"action": "status", "status": {"open": False}})

    def test_pretty_multiline(self):
        pretty = '{\n "action": "status",\n "status": {\n  "open": false\n }\n}'
        self.assertEqual(_mod._last_json(pretty), {"action": "status", "status": {"open": False}})

    def test_leading_banner_then_json(self):
        self.assertEqual(_mod._last_json('MOTD\nWARNING: pq banner\n{\n "status": {\n  "open": false\n }\n}'),
                         {"status": {"open": False}})

    def test_empty_and_whitespace_are_none(self):
        self.assertIsNone(_mod._last_json(""))
        self.assertIsNone(_mod._last_json("   \n\t  "))

    def test_garbage_is_none(self):
        self.assertIsNone(_mod._last_json("could not connect to docker daemon"))

    def test_partial_truncated_is_none(self):
        self.assertIsNone(_mod._last_json('{"status": {"open":'))

    def test_two_payloads_ambiguous_is_none(self):
        self.assertIsNone(_mod._last_json('{"a":1}{"b":2}'))

    def test_trailing_garbage_is_none(self):
        self.assertIsNone(_mod._last_json('{"status":{"open":false}} rogue'))

    def test_non_object_top_level_is_none(self):
        self.assertIsNone(_mod._last_json('true'))
        self.assertIsNone(_mod._last_json('[{"open": false}]'))


if __name__ == "__main__":
    unittest.main()
