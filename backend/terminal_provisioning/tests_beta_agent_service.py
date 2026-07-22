"""CVM-Inc-3 B2 — beta provisioning agent SERVICE tests (imports the deploy/beta-agent bundle).

Proves the required B2 properties: bind-guard, invalid UUID, path/reparse escape, production refusal,
port-8788 exclusion, tamper/expiry/replay, replay survives restart, conflicting duplicate, checksum
mismatch blocks mutation, START-once, STOP/TOMBSTONE process/dir ownership, response allowlist, and
accurate NEGOTIATE. Also asserts the bundle's shared modules are byte-identical to the backend originals.
"""
import hashlib
import http.client
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import agent as agent_mod            # noqa: E402
import config as agent_config        # noqa: E402
import manifest as agent_manifest    # noqa: E402
from lib import mgmt_protocol as proto   # noqa: E402
from op_impls import OpError             # noqa: E402
from stores import RuntimeLockManager, SqliteStore   # noqa: E402

KEYRING = {"k1": "agent-secret-key"}
RUUID = "abcdef01-2345-6789-abcd-ef0123456789"
BETA_ROOT = r"C:\GuvFX\beta\accounts"
CANON = rf"{BETA_ROOT}\{RUUID}\terminal"


class FakeWin:
    def __init__(self):
        self.dirs, self.owners, self.procs = set(), {}, {}
        self.reparse, self.stopped, self.moved, self.golden = {}, [], [], []

    def path_exists(self, p): return p in self.dirs
    def real_path(self, p): return self.reparse.get(p)
    def read_owner_tag(self, d): return self.owners.get(d)
    def write_owner_tag(self, d, u): self.owners[d] = str(u)
    def make_dirs(self, p): self.dirs.add(p)
    def copy_golden(self, d): self.golden.append(d)

    def launch_runtime(self, d, u):
        self.procs[d] = {"pid": 13020, "session_id": 1, "image": rf"{d}\terminal64.exe"}
        return self.procs[d]

    def find_runtime_process(self, d): return self.procs.get(d)

    def stop_pid(self, pid):
        self.stopped.append(pid)
        for k, v in list(self.procs.items()):
            if v["pid"] == pid:
                del self.procs[k]

    def same_volume(self, a, b): return True
    def move_dir(self, s, dst): self.moved.append((s, dst)); self.dirs.discard(s)


def _cfg(state_db):
    return {"keyring": KEYRING, "key_id": "k1", "beta_root": BETA_ROOT,
            "tombstone_base": r"C:\GuvFX\beta\tombstones", "state_db": state_db, "manifest_path": ""}


def _build(win=None, store=None, manifest_path=""):
    win = win or FakeWin()
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    store = store or SqliteStore(tmp.name)
    a = agent_mod.build_agent(_cfg(tmp.name), win=win, store=store,
                              locks=RuntimeLockManager(), manifest_path=manifest_path)
    a.now_fn = lambda: int(__import__("time").time())
    return a, win, store


def _req(op, *, job_id=1, ruuid=RUUID, ttl=30, nonce=None, now=None):
    now = now if now is not None else int(__import__("time").time())
    return proto.sign_request(provisioning_job_id=job_id, runtime_uuid=ruuid, operation=op,
                              correlation_id="c", keyring=KEYRING, key_id="k1", now=now,
                              ttl_seconds=ttl, nonce=nonce)


class BindGuardTests(SimpleTestCase):
    def test_refuses_public_and_wildcard(self):
        for bad in ("0.0.0.0", "::", "8.8.8.8", ""):
            with self.assertRaises(agent_config.ConfigError):
                agent_config.assert_private_bind(bad)

    def test_allows_private_and_tailscale(self):
        for good in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "100.79.101.19"):
            agent_config.assert_private_bind(good)   # no raise


class BundleIntegrityTests(SimpleTestCase):
    def test_bundle_shared_modules_match_backend(self):
        def sha(p):
            return hashlib.sha256(open(p, "rb").read()).hexdigest()
        for m in ("mgmt_protocol.py", "mgmt_agent_core.py"):
            backend = os.path.join(os.path.dirname(__file__), m)
            bundle = os.path.join(_BUNDLE, "lib", m)
            self.assertEqual(sha(backend), sha(bundle), f"{m} drifted from backend")

    def test_manifest_matches_on_disk_implementation(self):
        approved = agent_manifest.load_manifest(os.path.join(_BUNDLE, "manifest.json")).get("checksums", {})
        actual = agent_manifest.compute_checksums(_BUNDLE)
        self.assertTrue(agent_manifest.integrity_ok(approved, actual))


class AgentServiceTests(SimpleTestCase):
    def test_negotiate_reports_versions_and_ops(self):
        a, _, _ = _build()
        expected_version = agent_manifest.load_manifest(
            os.path.join(_BUNDLE, "manifest.json")).get("manifest_version")
        r = a.handle(_req("NEGOTIATE", ruuid=proto.NIL_UUID))
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(r["protocol_version"], proto.PROTOCOL_VERSION)
        self.assertEqual(r["manifest_version"], expected_version)
        self.assertEqual(set(r["supported_operations"]), set(proto.PROVISIONING_OPERATIONS))
        self.assertTrue(r["agent_version"])

    def test_invalid_uuid_rejected(self):
        a, _, _ = _build()
        r = a.handle(_req("MATERIALISE", ruuid="not-a-uuid"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "bad_runtime_uuid"))

    def test_reparse_escape_rejected(self):
        win = FakeWin(); win.reparse[CANON] = r"C:\Windows"
        a, _, _ = _build(win=win)
        win.dirs.add(CANON)
        r = a.handle(_req("VERIFY"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "reparse_escape"))

    def test_tampered_and_expired_rejected(self):
        a, _, _ = _build()
        bad = _req("VERIFY"); bad["operation"] = "STOP"
        self.assertEqual(a.handle(bad)["reason_code"], "bad_signature")
        exp = _req("VERIFY", ttl=1, now=int(__import__("time").time()) - 100)
        self.assertIn(a.handle(exp)["reason_code"], ("request_expired", "timestamp_skew"))

    def test_replay_rejected_and_survives_restart(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); tmp.close()
        store = SqliteStore(tmp.name)
        a, _, _ = _build(store=store)
        req = _req("VERIFY", nonce="n-1")
        self.assertEqual(a.handle(req)["outcome"], "ok")
        self.assertEqual(a.handle(req)["reason_code"], "nonce_replayed")
        # NEW store on the SAME file (simulates agent restart) still knows the nonce.
        store2 = SqliteStore(tmp.name)
        self.assertTrue(store2.seen("n-1"))

    def test_conflicting_duplicate_fails_closed(self):
        a, _, _ = _build()
        a.handle(_req("MATERIALISE", job_id=5, ruuid=RUUID))
        other = "11111111-2222-3333-4444-555555555555"
        r = a.handle(_req("MATERIALISE", job_id=5, ruuid=other))   # same job+op, different runtime
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "job_op_conflict"))

    def test_checksum_mismatch_blocks_mutation(self):
        # a manifest whose op_impls checksum is wrong → mutating op refused
        bad_manifest = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        import json
        json.dump({"agent_version": "x", "protocol_version": 1, "manifest_version": "m",
                   "supported_operations": list(proto.PROVISIONING_OPERATIONS),
                   "checksums": {"op_impls.py": "WRONG"}}, bad_manifest)
        bad_manifest.close()
        a, _, _ = _build(manifest_path=bad_manifest.name)
        r = a.handle(_req("MATERIALISE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "impl_integrity_mismatch"))

    def test_start_cannot_launch_twice(self):
        win = FakeWin(); win.dirs.add(CANON); win.owners[CANON] = RUUID
        a, _, _ = _build(win=win)
        r1 = a.handle(_req("START", job_id=1))
        r2 = a.handle(_req("START", job_id=2, nonce="s2"))   # different job → new (job,op)
        self.assertEqual(r1["pid"], 13020)
        self.assertEqual(r2["pid"], 13020)
        self.assertEqual(len([d for d in win.procs]), 1)     # only ONE process ever launched

    def test_stop_refuses_unrelated_process(self):
        win = FakeWin(); win.dirs.add(CANON); win.owners[CANON] = RUUID
        win.procs[CANON] = {"pid": 999, "session_id": 1, "image": r"C:\Windows\notepad.exe"}
        a, _, _ = _build(win=win)
        r = a.handle(_req("STOP"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "image_not_owned"))
        self.assertEqual(win.stopped, [])   # never killed an unrelated PID

    def test_tombstone_moves_only_canonical_dir_and_is_idempotent(self):
        win = FakeWin(); win.dirs.add(CANON); win.owners[CANON] = RUUID
        a, _, _ = _build(win=win)
        r = a.handle(_req("TOMBSTONE", job_id=1))
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(len(win.moved), 1)
        src, dst = win.moved[0]
        self.assertEqual(src, CANON)
        self.assertTrue(dst.startswith(r"C:\GuvFX\beta\tombstones"))
        # idempotent: a second TOMBSTONE (dir now gone) does not move anything else
        r2 = a.handle(_req("TOMBSTONE", job_id=2, nonce="t2"))
        self.assertEqual(r2["outcome"], "ok")
        self.assertEqual(len(win.moved), 1)

    def test_tombstone_refuses_cross_volume(self):
        win = FakeWin(); win.dirs.add(CANON); win.owners[CANON] = RUUID
        win.same_volume = lambda a, b: False
        a, _, _ = _build(win=win)
        r = a.handle(_req("TOMBSTONE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "cross_volume_move_refused"))
        self.assertEqual(win.moved, [])

    def test_response_allowlist_strips_secrets(self):
        win = FakeWin()
        win.launch_runtime = lambda d, u: {"pid": 1, "session_id": 1, "image": rf"{d}\t.exe",
                                           "password": "SECRET", "cmdline": "x", "env": {"K": "V"}}
        win.dirs.add(CANON); win.owners[CANON] = RUUID
        a, _, _ = _build(win=win)
        r = a.handle(_req("START"))
        self.assertNotIn("password", r)
        self.assertNotIn("cmdline", r)
        self.assertNotIn("SECRET", str(r))

    def test_request_schema_has_no_path_or_port_field(self):
        # structural: nothing in the signed schema can carry a path, port (e.g. 8788), or command.
        req = _req("MATERIALISE")
        for forbidden in ("path", "port", "command", "cmd", "exe", "args", "script", "env"):
            self.assertNotIn(forbidden, req)


class HardenedB2Tests(SimpleTestCase):
    """Additional coverage for the B2 review fixes (S1 win_ops integrity, S2 parent reparse, S3, bind
    boundaries, tombstone_dir stripping)."""

    def test_win_ops_drift_blocks_mutation(self):
        # S1: tampering win_ops.py (not op_impls.py) must still block every mutating op.
        import json as _json
        good = agent_manifest.load_manifest(os.path.join(_BUNDLE, "manifest.json"))
        bad = dict(good); bad_checks = dict(good["checksums"]); bad_checks["win_ops.py"] = "TAMPERED"
        bad["checksums"] = bad_checks
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        _json.dump(bad, f); f.close()
        a, _, _ = _build(manifest_path=f.name)
        r = a.handle(_req("MATERIALISE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "impl_integrity_mismatch"))

    def test_materialise_reparse_via_parent_junction_rejected(self):
        # S2: a junction planted at the (not-yet-materialised) <uuid> parent must be caught before write.
        win = FakeWin()
        parent = CANON.rsplit("\\", 1)[0]           # C:\GuvFX\beta\accounts\<uuid>
        win.reparse[parent] = r"C:\Windows\System32"
        a, _, _ = _build(win=win)
        r = a.handle(_req("MATERIALISE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "reparse_escape"))
        self.assertEqual(win.golden, [])             # never copied the golden image through the junction

    def test_bind_guard_ipv6_and_tailscale_boundary(self):
        # mapped-public + global IPv6 refused; Tailscale 100.64/10 boundary enforced.
        for bad in ("::ffff:8.8.8.8", "2001:4860:4860::8888", "100.63.255.255", "100.128.0.1"):
            with self.assertRaises(agent_config.ConfigError, msg=bad):
                agent_config.assert_private_bind(bad)
        for good in ("100.64.0.1", "100.127.255.254"):
            agent_config.assert_private_bind(good)   # Tailscale CGNAT allowed

    def test_atomic_nonce_burn(self):
        # S3: burn is atomic single-use — first True, replay False.
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        s = SqliteStore(f.name)
        self.assertTrue(s.burn("n", 9_999_999_999))
        self.assertFalse(s.burn("n", 9_999_999_999))

    def test_tombstone_dir_not_leaked_in_response(self):
        win = FakeWin(); win.dirs.add(CANON); win.owners[CANON] = RUUID
        a, _, _ = _build(win=win)
        r = a.handle(_req("TOMBSTONE"))
        self.assertEqual(r["outcome"], "ok")
        self.assertNotIn("tombstone_dir", r)         # full path stripped by the response allowlist
        self.assertNotIn("tombstoned", r)


def _server_cfg(state_db, *, host="127.0.0.1", port=0, log_dir=None):
    """Full config for the AgentServer lifecycle tests — a loopback bind (host == expected_bind_host)."""
    return {
        "bind_host": host, "expected_bind_host": host, "bind_port": port,
        "keyring": KEYRING, "key_id": "k1", "beta_root": BETA_ROOT,
        "tombstone_base": r"C:\GuvFX\beta\tombstones", "state_db": state_db, "manifest_path": "",
        "log_dir": log_dir,
        "max_body_bytes": 16384, "max_connections": 8, "request_timeout_s": 5, "drain_timeout_s": 5,
    }


class ExactBindPinTests(SimpleTestCase):
    """B-9: the LIVE bind is pinned to the exact expected management address, not merely 'some private'."""

    def test_exact_bind_accepts_only_expected(self):
        agent_config.assert_exact_bind("100.79.101.19", "100.79.101.19")   # no raise
        for other in ("127.0.0.1", "10.0.0.5", "100.79.101.20"):
            with self.assertRaises(agent_config.ConfigError):
                agent_config.assert_exact_bind(other, "100.79.101.19")

    def test_exact_bind_still_requires_private(self):
        with self.assertRaises(agent_config.ConfigError):
            agent_config.assert_exact_bind("8.8.8.8", "8.8.8.8")

    def test_load_config_pins_bind_and_relocates_state(self):
        env = {"BETA_AGENT_BIND_HOST": "100.79.101.19", "BETA_AGENT_KEYRING": json.dumps(KEYRING),
               "BETA_AGENT_KEY_ID": "k1"}
        cfg = agent_config.load_config(env)
        self.assertEqual(cfg["bind_host"], "100.79.101.19")
        self.assertTrue(cfg["state_db"].startswith(r"C:\GuvFX\beta\agent-state"))
        self.assertTrue(cfg["log_dir"].endswith(r"agent-state\logs"))

    def test_load_config_rejects_non_expected_bind(self):
        env = {"BETA_AGENT_BIND_HOST": "127.0.0.1", "BETA_AGENT_KEYRING": json.dumps(KEYRING),
               "BETA_AGENT_KEY_ID": "k1"}   # private but NOT the expected management address
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load_config(env)

    def test_load_config_refuses_reserved_ports(self):
        for bad in ("8788", "8787", "3389"):
            env = {"BETA_AGENT_BIND_HOST": "100.79.101.19", "BETA_AGENT_BIND_PORT": bad,
                   "BETA_AGENT_KEYRING": json.dumps(KEYRING), "BETA_AGENT_KEY_ID": "k1"}
            with self.assertRaises(agent_config.ConfigError):
                agent_config.load_config(env)


class FullBundleIntegrityTests(SimpleTestCase):
    """B-7: a drift in a non-op module (config.py — the bind-guard) must fail the mutation gate too."""

    def test_config_drift_blocks_mutation(self):
        good = agent_manifest.load_manifest(os.path.join(_BUNDLE, "manifest.json"))
        bad = dict(good); checks = dict(good["checksums"]); checks["config.py"] = "TAMPERED"
        bad["checksums"] = checks
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(bad, f); f.close()
        a, _, _ = _build(manifest_path=f.name)
        r = a.handle(_req("MATERIALISE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "impl_integrity_mismatch"))

    def test_manifest_covers_all_executable_modules(self):
        approved = agent_manifest.load_manifest(os.path.join(_BUNDLE, "manifest.json")).get("checksums", {})
        for m in ("agent.py", "config.py", "stores.py", "service.py", "manifest.py"):
            self.assertIn(m, approved, f"{m} not covered by the integrity manifest")


class DrainTests(SimpleTestCase):
    """B-6: the stop drain waits for in-flight mutating ops; the lock manager exposes the active count."""

    def test_lock_manager_tracks_active_mutations(self):
        locks = RuntimeLockManager()
        self.assertEqual(locks.active_mutations(), 0)
        with locks.acquire("u-1"):
            self.assertEqual(locks.active_mutations(), 1)
        self.assertEqual(locks.active_mutations(), 0)

    def test_await_drain_times_out_when_never_idle(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=FakeWin(), enforce_integrity=False)

        class _Busy:
            def active_mutations(self): return 1
        srv._locks = _Busy()
        self.assertFalse(srv._await_drain(0.1))   # never drains → False (bounded, does not hang)

    def test_await_drain_returns_true_when_idle(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=FakeWin(), enforce_integrity=False)
        self.assertTrue(srv._await_drain(0.1))


class AgentServerHttpTests(SimpleTestCase):
    """Resource-limit + routing behaviour of the live HTTP surface (loopback, real sockets)."""

    def _start(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=FakeWin(), enforce_integrity=False)
        srv.start()
        host, port = srv._httpd.server_address
        return srv, host, port

    def _post(self, host, port, body, *, content_length=None, path="/provision"):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Length": str(len(body) if content_length is None else content_length),
                   "Connection": "close"}
        conn.request("POST", path, body, headers)
        resp = conn.getresponse(); data = resp.read(); conn.close()
        return resp.status, data

    def test_valid_request_ok_oversize_413_and_bad_route_404(self):
        srv, host, port = self._start()
        try:
            body = json.dumps(_req("VERIFY")).encode()
            status, data = self._post(host, port, body)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["outcome"], "ok")

            big = b"x" * (srv.cfg["max_body_bytes"] + 100)   # refused BEFORE being read
            status, data = self._post(host, port, big)
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(data)["reason_code"], "request_too_large")

            status, _ = self._post(host, port, b"{}", path="/nope")
            self.assertEqual(status, 404)
        finally:
            self.assertTrue(srv.stop())   # idle → drains cleanly

    def test_get_has_no_route(self):
        srv, host, port = self._start()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/provision", headers={"Connection": "close"})
            resp = conn.getresponse(); resp.read(); conn.close()
            self.assertEqual(resp.status, 404)
        finally:
            srv.stop()


class ValidateAuthenticityTests(SimpleTestCase):
    """B-7 authenticity: validate.py fails when the pinned manifest.json hash does not match."""

    @staticmethod
    def _run(argv):
        import contextlib
        import io
        import validate  # noqa: PLC0415 — bundle module
        with contextlib.redirect_stdout(io.StringIO()):   # validate prints its own ok/FAIL lines
            return validate.main(argv)

    def test_wrong_manifest_hash_fails(self):
        self.assertEqual(self._run(["--expect-manifest-sha256", "deadbeef"]), 1)

    def test_correct_manifest_hash_passes(self):
        h = hashlib.sha256(open(os.path.join(_BUNDLE, "manifest.json"), "rb").read()).hexdigest()
        self.assertEqual(self._run(["--expect-manifest-sha256", h]), 0)


class KeepAliveDisabledTests(SimpleTestCase):
    """Review fix: keep-alive is disabled (Connection: close) so no connection holds a permit across requests
    and a reject path cannot desync a persistent connection."""

    def test_response_forces_connection_close(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=FakeWin(), enforce_integrity=False)
        srv.start()
        try:
            host, port = srv._httpd.server_address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("POST", "/provision", json.dumps(_req("VERIFY")).encode())
            resp = conn.getresponse(); resp.read(); conn.close()
            self.assertEqual((resp.getheader("Connection") or "").lower(), "close")
        finally:
            srv.stop()


class DrainOrderingTests(SimpleTestCase):
    """Review fix (B-6): stop() stops accepting + refuses new mutations, then WAITS for an in-flight mutation
    to finish (never kills it), and a request arriving during drain is cleanly refused, not started."""

    def test_stop_blocks_until_in_flight_mutation_releases(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        locks = RuntimeLockManager()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=FakeWin(), locks=locks, enforce_integrity=False)
        entered, release = threading.Event(), threading.Event()

        def hold():
            with locks.acquire("u-hold"):
                entered.set()
                release.wait(5)                      # keep the mutation "in flight"

        holder = threading.Thread(target=hold); holder.start()
        self.assertTrue(entered.wait(2))
        result = {}
        stopper = threading.Thread(target=lambda: result.__setitem__("drained", srv.stop(drain_timeout_s=5)))
        stopper.start()
        time.sleep(0.2)
        self.assertTrue(stopper.is_alive())          # stop is still draining, not done
        release.set()
        stopper.join(5); holder.join(5)
        self.assertTrue(result.get("drained"))       # drained cleanly once the op finished

    def test_request_arriving_during_drain_is_refused_not_run(self):
        win = FakeWin()
        a, _, _ = _build(win=win)
        a.runtime_locks.begin_drain()                # simulate: we are stopping
        r = a.handle(_req("MATERIALISE"))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "agent_stopping"))
        self.assertEqual(win.golden, [])             # the op never ran


class ConcurrencyCapTests(SimpleTestCase):
    """Review fix: a worker-thread spawn failure must release the connection permit in the SAME thread, or the
    concurrency gate leaks a permit per failure and eventually wedges the agent fully closed."""

    def test_permit_released_when_worker_thread_fails_to_start(self):
        import socketserver
        srv = agent_mod.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler,
            max_body_bytes=100, request_timeout_s=1, max_connections=3)
        try:
            with mock.patch.object(socketserver.ThreadingMixIn, "process_request",
                                   side_effect=RuntimeError("can't start new thread")):
                for _ in range(5):                   # 5 failed spawns must NOT leak permits
                    with self.assertRaises(RuntimeError):
                        srv.process_request(object(), ("127.0.0.1", 1))
            got = sum(1 for _ in range(3) if srv._conn_sem.acquire(blocking=False))
            self.assertEqual(got, 3)                 # all permits still available (none leaked)
        finally:
            srv.server_close()


class LifecycleLogTests(SimpleTestCase):
    """Review fix: the relocated log_dir is actually written (lifecycle events only), so the claim is realised."""

    def test_start_stop_writes_lifecycle_log(self):
        d = tempfile.mkdtemp()
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name, log_dir=d), win=FakeWin(), enforce_integrity=False)
        srv.start(); srv.stop()
        logpath = os.path.join(d, "agent.log")
        self.assertTrue(os.path.exists(logpath))
        content = open(logpath, encoding="utf-8").read()
        self.assertIn("agent started", content)
        self.assertNotIn("secret", content.lower())   # never logs secrets/keyring


class SlotPoolTests(SimpleTestCase):
    """B3P-2 per-slot execution model: durable UUID->slot assignment over a PRE-PROVISIONED pool.

    The store must never create an OS object; it only records occupancy of slots an administrator created
    at install. Concurrent runtimes never share a slot; a slot is reusable only after release.
    """

    @staticmethod
    def _store(pool_size=5):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=pool_size)

    def test_assign_is_idempotent_per_runtime(self):
        s = self._store()
        a = s.assign(RUUID, now=1)
        self.assertEqual(s.assign(RUUID, now=2), a)      # same slot, not a second one
        self.assertEqual(list(s.occupancy()), [a])

    def test_distinct_runtimes_get_distinct_slots(self):
        s = self._store()
        u2 = "11111111-2222-3333-4444-555555555555"
        self.assertNotEqual(s.assign(RUUID, now=1), s.assign(u2, now=1))

    def test_lowest_free_slot_is_reused_only_after_release(self):
        s = self._store(pool_size=2)
        u2 = "11111111-2222-3333-4444-555555555555"
        u3 = "22222222-3333-4444-5555-666666666666"
        s1, s2 = s.assign(RUUID, now=1), s.assign(u2, now=1)
        self.assertEqual({s1, s2}, {1, 2})
        with self.assertRaises(Exception):               # pool full -> denied, never over-allocated
            s.assign(u3, now=1)
        self.assertTrue(s.release(RUUID))
        self.assertEqual(s.assign(u3, now=1), s1)        # freed slot reused
        self.assertIsNone(s.lookup(RUUID))

    def test_pool_exhaustion_is_a_sanitised_agent_error(self):
        from lib.mgmt_agent_core import AgentError
        from stores import PoolExhausted
        s = self._store(pool_size=1)
        s.assign(RUUID, now=1)
        with self.assertRaises(PoolExhausted) as ctx:
            s.assign("11111111-2222-3333-4444-555555555555", now=1)
        self.assertIsInstance(ctx.exception, AgentError)
        self.assertEqual(ctx.exception.reason_code, "pool_exhausted")

    def test_lookup_never_allocates(self):
        s = self._store()
        self.assertIsNone(s.lookup(RUUID))
        self.assertEqual(s.occupancy(), {})               # a stray request cannot consume a slot

    def test_release_is_idempotent(self):
        s = self._store()
        s.assign(RUUID, now=1)
        self.assertTrue(s.release(RUUID))
        self.assertFalse(s.release(RUUID))

    def test_assignment_survives_restart(self):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        s = SlotStore(f.name, pool_size=5)
        slot = s.assign(RUUID, now=1)
        self.assertEqual(SlotStore(f.name, pool_size=5).lookup(RUUID), slot)

    def test_slot_dir_is_derived_only_from_the_slot_number(self):
        from stores import slot_runtime_dir
        self.assertEqual(slot_runtime_dir(r"C:\GuvFX\beta\slots", 3), r"C:\GuvFX\beta\slots\3\terminal")
        # no caller-supplied value can influence the path -> the per-slot task target stays fixed
        self.assertEqual(slot_runtime_dir(r"C:\GuvFX\beta\slots", "2"), r"C:\GuvFX\beta\slots\2\terminal")
