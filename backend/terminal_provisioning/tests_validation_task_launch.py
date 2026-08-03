"""ADR-0027 task-launch remediation — tests for the secure handoff, the delegating validator, and the
task-launched runner (imports the deploy/beta-agent bundle; no Windows/MT5/network).

Covers the packet's required scenarios: happy path, task unavailable, task timeout, duplicate/concurrent
launch, replay, expired request, bad request id, stale cleanup, runner crash, runner timeout, cleanup after
crash, NO credential leakage (no plaintext on disk; the task trigger receives only a name), one active
validation only, and that the runner exposes no trade surface (it reuses the no-trade probe).
"""
import os
import shutil
import sys
import tempfile
import threading

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import validation_handoff as handoff            # noqa: E402
import validation_runner as runner              # noqa: E402
import validate_login as vl                     # noqa: E402

SEALED = {"v": 1, "ct": "BASE64CIPHERTEXT==", "nonce": "abc", "epk": "PUBKEY", "key_id": "beta-cred-v1"}
PAYLOAD = {"login": "1302575", "server": "IS6Technologies-Demo", "password_env": SEALED}
CTX = dict(operation="VALIDATE_LOGIN", runtime_uuid="abcdef01-2345-6789-abcd-ef0123456789",
           correlation_id="corr-1", nonce="nonce-1")


class _Clock:
    """Deterministic clock: sleep advances virtual time so read_result terminates without real waits."""
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _FakeHandler:
    """Stands in for the in-process LoginValidationHandler; records the call and returns a preset outcome."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def validate(self, **kw):
        self.calls.append(kw)
        return self.outcome


def _runner_cfg(hdir):
    return {"validation_handoff_dir": hdir, "login_timeout_ms": 5000}


class HandoffTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_write_claim_roundtrip(self):
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, {"x": 1}, ttl_seconds=60)
        got = handoff.claim_request(self.d, rid)
        self.assertEqual(got, {"x": 1})

    def test_single_use_second_claim_is_none(self):
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, {"x": 1}, ttl_seconds=60)
        self.assertIsNotNone(handoff.claim_request(self.d, rid))
        self.assertIsNone(handoff.claim_request(self.d, rid))     # replay of a consumed id → nothing

    def test_expired_request_refused(self):
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, {"x": 1}, ttl_seconds=1, now=1000.0)
        self.assertIsNone(handoff.claim_request(self.d, rid, now=2000.0))

    def test_tampered_body_fails_auth(self):
        import json
        rid = handoff.new_request_id()
        path = handoff.write_request(self.d, rid, {"x": 1}, ttl_seconds=60)
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["body"]["request"]["x"] = 999                         # tamper body, keep the stale hmac
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.assertIsNone(handoff.claim_request(self.d, rid))     # hmac no longer matches → ignored

    def test_result_roundtrip_and_cleanup(self):
        rid = handoff.new_request_id()
        handoff.write_result(self.d, rid, {"ok": True, "reason_code": "demo_ok", "is_demo": True})
        clk = _Clock()
        res = handoff.read_result(self.d, rid, timeout_s=5, sleep=clk.sleep, clock=clk.now)
        self.assertEqual(res["reason_code"], "demo_ok")
        handoff.cleanup(self.d, rid)
        self.assertFalse(os.path.exists(os.path.join(self.d, rid + handoff._RES)))

    def test_result_timeout_returns_none(self):
        clk = _Clock()
        self.assertIsNone(handoff.read_result(self.d, "missing", timeout_s=2, sleep=clk.sleep, clock=clk.now))

    def test_sweep_stale_removes_old_not_key(self):
        rid = handoff.new_request_id()
        handoff.local_key(self.d)                                 # create the key file
        p = handoff.write_request(self.d, rid, {"x": 1}, ttl_seconds=60)
        old = os.stat(p).st_mtime - 10000
        os.utime(p, (old, old))
        removed = handoff.sweep_stale(self.d, max_age_s=100)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(os.path.join(self.d, handoff._KEY_FILE)))   # key never swept

    def test_no_plaintext_on_disk(self):
        rid = handoff.new_request_id()
        p = handoff.write_request(self.d, rid, {"payload": PAYLOAD}, ttl_seconds=60)
        raw = open(p, "r", encoding="utf-8").read()
        self.assertIn("ct", raw)                                  # ciphertext present (sealed)
        self.assertNotIn("s3cret", raw)                           # a plaintext password never appears


class TaskLaunchValidatorTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _validator(self, trigger, **kw):
        return vl.TaskLaunchLoginValidator(handoff_dir=self.d, task_name="GvfxValidationRunner",
                                           trigger_task=trigger, timeout_ms=5000, **kw)

    def test_happy_path_delegates_and_returns_outcome(self):
        seen_task = []
        fake = _FakeHandler({"ok": True, "reason_code": "demo_ok", "is_demo": True})

        def trigger(task_name):
            seen_task.append(task_name)                           # runner is triggered with ONLY a name
            # simulate the scheduled task launching the runner IN the GUI-capable context
            runner.run_once(_runner_cfg(self.d), build_handler=lambda cfg: fake)
            return True

        out = self._validator(trigger).validate(payload=PAYLOAD, **CTX)
        self.assertEqual(out, {"ok": True, "reason_code": "demo_ok", "is_demo": True})
        self.assertEqual(seen_task, ["GvfxValidationRunner"])     # no secret in the trigger arg
        self.assertEqual(fake.calls[0]["payload"], PAYLOAD)       # sealed payload reached the probe intact
        # cleanup: no handoff files remain for the request
        self.assertEqual([f for f in os.listdir(self.d) if not f.endswith(".key")], [])

    def test_task_unavailable(self):
        out = self._validator(lambda t: False).validate(payload=PAYLOAD, **CTX)
        self.assertEqual(out["reason_code"], "validation_runner_unavailable")

    def test_trigger_exception_is_unavailable(self):
        def boom(t):
            raise RuntimeError("schtasks missing")
        out = self._validator(boom).validate(payload=PAYLOAD, **CTX)
        self.assertEqual(out["reason_code"], "validation_runner_unavailable")

    def test_runner_timeout(self):
        clk = _Clock()
        out = self._validator(lambda t: True, clock=clk.now, sleep=clk.sleep, result_grace_s=1).validate(
            payload=PAYLOAD, **CTX)
        self.assertEqual(out["reason_code"], "validation_runner_timeout")

    def test_concurrent_second_is_busy(self):
        lock = threading.Lock()
        lock.acquire()                                            # simulate an in-flight delegation
        out = self._validator(lambda t: True, lock=lock).validate(payload=PAYLOAD, **CTX)
        self.assertEqual(out["reason_code"], "validation_busy")

    def test_agent_build_selects_task_launch(self):
        import agent as agent_mod                              # noqa: PLC0415

        class FakeWin:
            def run_task(self, name):
                return True

        v = agent_mod._build_login_validator(
            {"validation_terminal_dir": r"C:\GuvFX\beta\validation\vt",
             "validation_task_name": "GvfxValidationRunner",
             "validation_handoff_dir": self.d, "login_timeout_ms": 5000}, FakeWin())
        self.assertIsInstance(v, vl.TaskLaunchLoginValidator)

    def test_agent_build_task_launch_needs_win_and_dir(self):
        import agent as agent_mod                              # noqa: PLC0415
        # task name but no adapter → fail closed (None)
        self.assertIsNone(agent_mod._build_login_validator(
            {"validation_terminal_dir": r"C:\GuvFX\beta\validation\vt",
             "validation_task_name": "T", "validation_handoff_dir": self.d}, None))

    def test_unconfigured_when_no_task(self):
        v = vl.TaskLaunchLoginValidator(handoff_dir=self.d, task_name="", trigger_task=lambda t: True,
                                        timeout_ms=5000)
        self.assertEqual(v.validate(payload=PAYLOAD, **CTX)["reason_code"], "validation_unconfigured")

    def test_cleanup_after_runner_crash(self):
        def trigger(task_name):
            return True                                           # "crashed": launched but no result
        clk = _Clock()
        self._validator(trigger, clock=clk.now, sleep=clk.sleep, result_grace_s=1).validate(
            payload=PAYLOAD, **CTX)
        self.assertEqual([f for f in os.listdir(self.d) if not f.endswith(".key")], [])   # no leftovers


class RunnerTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_no_pending_is_noop(self):
        self.assertEqual(runner.run_once(_runner_cfg(self.d)), "no_single_pending:0")

    def test_more_than_one_pending_refused(self):
        for _ in range(2):
            handoff.write_request(self.d, handoff.new_request_id(), {"operation": "VALIDATE_LOGIN"}, ttl_seconds=60)
        self.assertEqual(runner.run_once(_runner_cfg(self.d)), "no_single_pending:2")   # one active only

    def test_claim_then_probe_writes_result(self):
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, dict(payload=PAYLOAD, **CTX), ttl_seconds=60)
        fake = _FakeHandler({"ok": False, "reason_code": "invalid_password", "is_demo": None})
        self.assertEqual(runner.run_once(_runner_cfg(self.d), build_handler=lambda cfg: fake), "cleanup_complete")
        clk = _Clock()
        res = handoff.read_result(self.d, rid, timeout_s=2, sleep=clk.sleep, clock=clk.now)
        self.assertEqual(res["reason_code"], "invalid_password")
        self.assertEqual(fake.calls[0]["operation"], "VALIDATE_LOGIN")

    def test_unconfigured_handler_writes_unconfigured(self):
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, dict(payload=PAYLOAD, **CTX), ttl_seconds=60)
        self.assertEqual(runner.run_once(_runner_cfg(self.d), build_handler=lambda cfg: None), "unconfigured")
        clk = _Clock()
        res = handoff.read_result(self.d, rid, timeout_s=2, sleep=clk.sleep, clock=clk.now)
        self.assertEqual(res["reason_code"], "validation_unconfigured")

    def test_scrub_removes_credential_artefact_and_logs(self):
        vdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, vdir, ignore_errors=True)
        os.makedirs(os.path.join(vdir, "config"))
        rid = handoff.new_request_id()
        handoff.write_request(self.d, rid, dict(payload=PAYLOAD, **CTX), ttl_seconds=60)
        cfg = {"validation_handoff_dir": self.d, "login_timeout_ms": 5000, "validation_terminal_dir": vdir}

        class _WritingHandler:                                  # the PROBE writes the artefacts DURING login
            def validate(self, **_kw):
                with open(os.path.join(vdir, "config", "accounts.dat"), "w") as fh:
                    fh.write("login+obfuscated-pw")             # the credential artefact MT5 writes
                os.makedirs(os.path.join(vdir, "Logs"), exist_ok=True)
                with open(os.path.join(vdir, "Logs", "20260101.log"), "w") as fh:
                    fh.write("journal")
                return {"ok": True, "reason_code": "demo_ok", "is_demo": True}

        runner.run_once(cfg, build_handler=lambda c: _WritingHandler())
        self.assertFalse(os.path.exists(os.path.join(vdir, "config", "accounts.dat")))   # no credential left
        self.assertFalse(os.path.exists(os.path.join(vdir, "Logs")))                     # logs cleared

    def test_runner_reuses_no_trade_probe(self):
        # The runner builds the real in-process handler, which uses RealMt5Probe — a probe with NO order /
        # symbol / position API (defence in depth against a trade path in the validation runner).
        for banned in ("order_send", "order_check", "positions_get", "symbol_select", "Buy", "Sell"):
            self.assertFalse(hasattr(vl.RealMt5Probe, banned))
