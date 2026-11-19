"""Synthetic-only tests for scripts/tim_isolated_bridge_ctl.py
(GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121).

Standard-library unittest only (governance CI installs no dependencies). No host, no process,
no network, no MT5, no broker, no token: every host interaction is a fake, and the dedicated
state file is redirected to a temp directory. The lifecycle logic (double-start refusal, port
isolation, loopback-only bind proof, :8788 protected-control invariance, PID-identity teardown,
PID-reuse refusal, pre-existing-terminal protection, partial-start rollback) is exercised with
an injected fake host.
"""

import importlib.util
import json
import os
import re
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CTL_PATH = os.path.join(_HERE, "..", "scripts", "tim_isolated_bridge_ctl.py")

_spec = importlib.util.spec_from_file_location("tim_isolated_bridge_ctl", _CTL_PATH)
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)

TERMINAL = ctl.ACCOUNT001_TERMINAL
BRIDGE_SCRIPT = ctl.BRIDGE_SCRIPT
FIXED_PORT = ctl.FIXED_PORT
PROTECTED_PORT = ctl.PROTECTED_PORT


class FakeHost:
    """Deterministic in-memory host. Never touches a real process, port, or file."""

    def __init__(self, bridge_bind="loopback", protected_pid=100, preexisting_terminal=False):
        self._t = 1000.0
        self._next = 500
        self.bridge_bind = bridge_bind            # "loopback" | "wildcard" | "none"
        self.procs = {}                           # pid -> {exe, cmdline}
        self.listen = {}                          # port -> list[(addr, pid)]
        self.started = []                         # list of cmd lists
        self.terminated = []                      # list of pids
        if protected_pid is not None:
            self.procs[protected_pid] = {"exe": r"C:\GuvFX\mt5_signal_bridge_prod.py",
                                         "cmdline": "python prod bridge :8788"}
            self.listen[PROTECTED_PORT] = [("0.0.0.0", protected_pid)]
        if preexisting_terminal:
            self.procs[777] = {"exe": TERMINAL, "cmdline": TERMINAL + " /portable"}

    # --- HostOps surface ---
    def now(self):
        return self._t

    def sleep(self, seconds):
        self._t += max(seconds, 0.01)

    def listeners(self):
        return [(addr, port, pid) for port, lst in self.listen.items() for (addr, pid) in lst]

    def process(self, pid):
        return self.procs.get(pid)

    def find_pids_by_exe(self, exe_path):
        t = exe_path.lower()
        return [pid for pid, info in self.procs.items() if info["exe"].lower() == t]

    def start(self, cmd, env):
        pid = self._next
        self._next += 1
        self.started.append(list(cmd))
        if any(BRIDGE_SCRIPT == c for c in cmd):
            self.procs[pid] = {"exe": cmd[0], "cmdline": " ".join(cmd)}
            if self.bridge_bind == "loopback":
                self.listen[FIXED_PORT] = [("127.0.0.1", pid)]
            elif self.bridge_bind == "wildcard":
                self.listen[FIXED_PORT] = [("0.0.0.0", pid)]
            # "none" -> never binds
        elif cmd and cmd[0] == TERMINAL:
            self.procs[pid] = {"exe": TERMINAL, "cmdline": TERMINAL + " /portable"}
        else:
            self.procs[pid] = {"exe": cmd[0], "cmdline": " ".join(cmd)}
        return pid

    def terminate(self, pid):
        self.terminated.append(pid)
        self.procs.pop(pid, None)
        for port in list(self.listen):
            self.listen[port] = [(a, p) for (a, p) in self.listen[port] if p != pid]
            if not self.listen[port]:
                del self.listen[port]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._staging = self._tmp.name
        self._state = os.path.join(self._staging, "tim_isolated_state.json")
        self._orig = (ctl.STAGING_DIR, ctl.STATE_FILE)
        ctl.STAGING_DIR = self._staging
        ctl.STATE_FILE = self._state

    def tearDown(self):
        ctl.STAGING_DIR, ctl.STATE_FILE = self._orig
        self._tmp.cleanup()

    def _state_json(self):
        with open(self._state, encoding="ascii") as fh:
            return json.load(fh)


class TestStartHappyPath(_Base):
    def test_start_ok_loopback_and_state(self):
        host = FakeHost(bridge_bind="loopback")
        res = ctl.start_observation(host, env={})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "isolated_bridge_started")
        self.assertTrue(res["loopback_only"])
        self.assertTrue(res["protected_8788_unchanged"])
        self.assertTrue(res["terminal_created_by_us"])
        self.assertEqual(res["fixed_port"], FIXED_PORT)
        self.assertEqual(res["bind_host"], "127.0.0.1")
        st = self._state_json()
        self.assertEqual(st["schema"], "tim-isolated-ctl/1")
        self.assertTrue(st["bridge"]["created_by_us"])
        self.assertTrue(st["terminal"]["created_by_us"])
        self.assertEqual(st["protected_pid"], 100)
        self.assertNotIn(100, host.terminated)  # :8788 never touched

    def test_bridge_started_with_unique_argv_nonce(self):
        host = FakeHost()
        ctl.start_observation(host, env={})
        bridge_cmds = [c for c in host.started if BRIDGE_SCRIPT in c]
        self.assertEqual(len(bridge_cmds), 1)
        cmd = bridge_cmds[0]
        self.assertIn("--tim-run-id", cmd)
        nonce = cmd[cmd.index("--tim-run-id") + 1]
        self.assertRegex(nonce, r"^[0-9a-f]{16}$")   # secrets.token_hex(8)
        self.assertEqual(self._state_json()["bridge"]["run_nonce"], nonce)


class TestStartRefusals(_Base):
    def test_protected_8788_absent(self):
        host = FakeHost(protected_pid=None)
        res = ctl.start_observation(host, env={})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason_code"], "protected_8788_not_single_listener")
        self.assertFalse(os.path.exists(self._state))

    def test_fixed_port_in_use(self):
        host = FakeHost()
        host.listen[FIXED_PORT] = [("127.0.0.1", 321)]
        res = ctl.start_observation(host, env={})
        self.assertEqual(res["reason_code"], "fixed_port_in_use")

    def test_double_start_refused(self):
        host = FakeHost()
        ctl.start_observation(host, env={})
        res2 = ctl.start_observation(FakeHost(), env={})
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["reason_code"], "isolated_observation_already_active_or_stale_state")

    def test_wildcard_bind_rejected_and_rolled_back(self):
        host = FakeHost(bridge_bind="wildcard")
        res = ctl.start_observation(host, env={})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason_code"], "loopback_bind_unproven")
        self.assertFalse(os.path.exists(self._state))
        # the bridge we started was torn down; :8788 untouched
        self.assertNotIn(100, host.terminated)
        self.assertTrue(host.terminated, "partial-start bridge should have been terminated")

    def test_no_bind_times_out_and_rolls_back(self):
        host = FakeHost(bridge_bind="none")
        res = ctl.start_observation(host, env={})
        self.assertEqual(res["reason_code"], "loopback_bind_unproven")
        self.assertFalse(os.path.exists(self._state))


class TestPreExistingTerminal(_Base):
    def test_attach_not_own_preexisting_terminal(self):
        host = FakeHost(preexisting_terminal=True)
        res = ctl.start_observation(host, env={})
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["terminal_created_by_us"])
        st = self._state_json()
        self.assertFalse(st["terminal"]["created_by_us"])
        self.assertEqual(st["terminal"]["pid"], 777)
        # Stopping must NOT kill the owner's terminal.
        stop = ctl.stop_observation(host)
        self.assertTrue(stop["ok"], stop)
        self.assertNotIn(777, host.terminated)


class TestStop(_Base):
    def _start(self, **kw):
        host = FakeHost(**kw)
        res = ctl.start_observation(host, env={})
        self.assertTrue(res["ok"], res)
        return host

    def test_stop_targeted_teardown(self):
        host = self._start()
        st = self._state_json()
        bpid, tpid = st["bridge"]["pid"], st["terminal"]["pid"]
        res = ctl.stop_observation(host)
        self.assertTrue(res["ok"], res)
        self.assertIn(bpid, host.terminated)
        self.assertIn(tpid, host.terminated)
        self.assertNotIn(100, host.terminated)    # :8788 untouched
        self.assertFalse(os.path.exists(self._state))

    def test_stop_no_state(self):
        res = ctl.stop_observation(FakeHost())
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason_code"], "no_state_file")

    def test_stop_malformed_state(self):
        os.makedirs(self._staging, exist_ok=True)
        with open(self._state, "w", encoding="ascii") as fh:
            fh.write("{not json")
        res = ctl.stop_observation(FakeHost())
        self.assertEqual(res["reason_code"], "malformed_state_file")

    def test_stop_wrong_schema(self):
        os.makedirs(self._staging, exist_ok=True)
        with open(self._state, "w", encoding="ascii") as fh:
            json.dump({"schema": "other"}, fh)
        res = ctl.stop_observation(FakeHost())
        self.assertEqual(res["reason_code"], "unrecognized_state_schema")

    def test_stop_refuses_pid_reuse(self):
        host = self._start()
        st = self._state_json()
        bpid = st["bridge"]["pid"]
        # Simulate PID reuse: a DIFFERENT process now holds the bridge pid.
        host.procs[bpid] = {"exe": r"C:\Windows\System32\lsass.exe", "cmdline": "lsass.exe"}
        res = ctl.stop_observation(host)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason_code"], "bridge_identity_mismatch_refused")
        self.assertNotIn(bpid, host.terminated)   # the reuser is NOT killed
        self.assertTrue(os.path.exists(self._state))

    def test_stop_already_dead_bridge_cleans_up(self):
        host = self._start()
        st = self._state_json()
        bpid, tpid = st["bridge"]["pid"], st["terminal"]["pid"]
        # Bridge already exited (pid gone, port released); terminal still ours.
        host.procs.pop(bpid, None)
        host.listen.pop(FIXED_PORT, None)
        res = ctl.stop_observation(host)
        self.assertTrue(res["ok"], res)
        self.assertIn(tpid, host.terminated)
        self.assertFalse(os.path.exists(self._state))


class TestBridgeEnv(_Base):
    def test_env_is_read_only_posture(self):
        src = {"MT5_ALLOW_LIVE": "1", "GUVFX_API_URL": "https://api.guvfx.com",
               "GUVFX_AGENT_TOKEN": "placeholder-synthetic-token-value"}
        e = ctl._bridge_env(src)
        self.assertEqual(e["BRIDGE_BIND_HOST"], "127.0.0.1")
        self.assertEqual(e["BRIDGE_HTTP_ONLY"], "1")
        self.assertEqual(e["HTTP_SERVER_PORT"], str(FIXED_PORT))
        self.assertEqual(e["MT5_TERMINAL_PATH"], TERMINAL)
        self.assertEqual(e["MT5_GUARDED_ATTACH"], "1")
        self.assertEqual(e["MT5_EXPECTED_SERVER"], "TradersWay-Demo")
        self.assertNotIn("MT5_ALLOW_LIVE", e)       # never live
        self.assertNotIn("GUVFX_API_URL", e)        # no poller transport
        # the opaque token is passed THROUGH untouched (controller never reads it)
        self.assertEqual(e["GUVFX_AGENT_TOKEN"], "placeholder-synthetic-token-value")


class TestSafetyAndValueBlind(_Base):
    def test_results_are_value_blind(self):
        host = FakeHost()
        start = ctl.start_observation(host, env={"GUVFX_AGENT_TOKEN": "placeholder-sekrit-leak-xyz"})
        blob = json.dumps(start) + json.dumps(ctl.stop_observation(host))
        for leak in ("sekrit", "token", "balance", "equity", "login", "password"):
            self.assertNotIn(leak, blob.lower())

    def test_source_no_broad_kill_or_trading(self):
        with open(_CTL_PATH, encoding="utf-8") as fh:
            src = fh.read()
        # No broad (name-based) process termination in CODE (quoted forms / PowerShell -Name).
        for bad in ('"/IM"', "'/IM'", '"/im"', "'/im'", "-Name ", "Stop-Process -Name", "/F /IM"):
            self.assertNotIn(bad, src, "controller must not broad-kill: %r" % bad)
        # No trading / execution primitives anywhere.
        for bad in ("order_send", "order_check", "place_order", "withdraw",
                    "import MetaTrader5", "import requests", "/mt5/order"):
            self.assertNotIn(bad, src, "controller must not contain %r" % bad)
        # Teardown terminates ONLY an explicit single PID.
        self.assertIn('["taskkill", "/PID", str(int(pid)), "/F"]', src)

    def test_source_does_not_read_the_token(self):
        with open(_CTL_PATH, encoding="utf-8") as fh:
            src = fh.read()
        # Mentioning the token in a comment is fine; the controller must never READ it by name.
        for bad in ('getenv("GUVFX_AGENT_TOKEN"', "getenv('GUVFX_AGENT_TOKEN'",
                    '["GUVFX_AGENT_TOKEN"]', "['GUVFX_AGENT_TOKEN']",
                    '.get("GUVFX_AGENT_TOKEN"', ".get('GUVFX_AGENT_TOKEN'"):
            self.assertNotIn(bad, src, "controller must not read the token: %r" % bad)

    def test_stdlib_only_imports(self):
        with open(_CTL_PATH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("requests", src)
        self.assertNotIn("MetaTrader5", src)


class TestSafeTerminateUnit(_Base):
    def test_gone_pid_is_safe_noop(self):
        host = FakeHost()
        self.assertTrue(ctl._safe_terminate(host, 99999, {"exe": "x", "cmdline": "y"}))
        self.assertEqual(host.terminated, [])

    def test_mismatch_refused(self):
        host = FakeHost()
        host.procs[4242] = {"exe": "a.exe", "cmdline": "a"}
        self.assertFalse(ctl._safe_terminate(host, 4242, {"exe": "b.exe", "cmdline": "b"}))
        self.assertEqual(host.terminated, [])

    def test_match_terminates(self):
        host = FakeHost()
        host.procs[4242] = {"exe": "a.exe", "cmdline": "a"}
        self.assertTrue(ctl._safe_terminate(host, 4242, {"exe": "a.exe", "cmdline": "a"}))
        self.assertEqual(host.terminated, [4242])


if __name__ == "__main__":
    unittest.main()
