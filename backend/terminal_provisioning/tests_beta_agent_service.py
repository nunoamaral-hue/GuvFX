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
        slot, gen = s.assign(RUUID, now=1)
        self.assertEqual(s.assign(RUUID, now=2), (slot, gen))   # same slot AND generation
        self.assertEqual(list(s.occupancy()), [slot])

    def test_distinct_runtimes_get_distinct_slots(self):
        s = self._store()
        u2 = "11111111-2222-3333-4444-555555555555"
        self.assertNotEqual(s.assign(RUUID, now=1)[0], s.assign(u2, now=1)[0])

    def test_lowest_free_slot_is_reused_only_after_release(self):
        s = self._store(pool_size=2)
        u2 = "11111111-2222-3333-4444-555555555555"
        u3 = "22222222-3333-4444-5555-666666666666"
        s1, s2 = s.assign(RUUID, now=1)[0], s.assign(u2, now=1)[0]
        self.assertEqual({s1, s2}, {1, 2})
        with self.assertRaises(Exception):               # pool full -> denied, never over-allocated
            s.assign(u3, now=1)
        self.assertTrue(s.release(RUUID))
        self.assertEqual(s.assign(u3, now=1)[0], s1)     # freed slot reused
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


class SlotGenerationTests(SimpleTestCase):
    """Durable per-slot GENERATION disambiguates historical vs current occupants of a reused slot."""

    @staticmethod
    def _store(pool_size=2):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=pool_size)

    def test_generation_starts_at_one_and_increments_only_on_release(self):
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        self.assertEqual(gen, 1)
        self.assertEqual(s.assign(RUUID, now=2)[1], 1)        # re-assign does NOT bump
        self.assertTrue(s.release(RUUID, now=3))
        self.assertEqual(s.generation_of(slot), 2)            # release bumps

    def test_reused_slot_gives_the_next_occupant_a_greater_generation(self):
        s = self._store()
        u2 = "11111111-2222-3333-4444-555555555555"
        slot1, gen1 = s.assign(RUUID, now=1)
        s.release(RUUID)
        slot2, gen2 = s.assign(u2, now=2)
        self.assertEqual(slot2, slot1)                        # same physical slot
        self.assertGreater(gen2, gen1)                        # but a distinct occupancy

    def test_generation_survives_restart(self):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        s = SlotStore(f.name, pool_size=2)
        slot, _ = s.assign(RUUID, now=1)
        s.release(RUUID)
        self.assertEqual(SlotStore(f.name, pool_size=2).generation_of(slot), 2)

    def test_release_of_unknown_runtime_does_not_bump_anything(self):
        s = self._store()
        slot, _ = s.assign(RUUID, now=1)
        self.assertFalse(s.release("99999999-9999-9999-9999-999999999999"))
        self.assertEqual(s.generation_of(slot), 1)


class SlotIntegrityInvariantTests(SimpleTestCase):
    """Four-way agreement (db / marker / uuid / generation) must hold before ANY mutating operation."""

    @staticmethod
    def _marker(uuid=RUUID, slot=1, generation=1):
        from stores import format_owner_marker
        return format_owner_marker(uuid, slot, generation)

    def test_all_four_agreeing_passes(self):
        from stores import assert_slot_integrity
        assert_slot_integrity(runtime_uuid=RUUID, slot=1, generation=1,
                              marker_raw=self._marker())        # no raise

    def test_stale_generation_fails_closed(self):
        """The exact ambiguity this prevents: a marker left by the PREVIOUS occupant of a reused slot."""
        from stores import SlotIntegrityError, assert_slot_integrity
        with self.assertRaises(SlotIntegrityError) as ctx:
            assert_slot_integrity(runtime_uuid=RUUID, slot=1, generation=2,
                                  marker_raw=self._marker(generation=1))
        self.assertEqual(ctx.exception.reason_code, "slot_integrity_mismatch")

    def test_wrong_uuid_fails_closed(self):
        from stores import SlotIntegrityError, assert_slot_integrity
        with self.assertRaises(SlotIntegrityError):
            assert_slot_integrity(runtime_uuid="11111111-2222-3333-4444-555555555555",
                                  slot=1, generation=1, marker_raw=self._marker())

    def test_wrong_slot_fails_closed(self):
        from stores import SlotIntegrityError, assert_slot_integrity
        with self.assertRaises(SlotIntegrityError):
            assert_slot_integrity(runtime_uuid=RUUID, slot=2, generation=1, marker_raw=self._marker())

    def test_absent_or_corrupt_marker_is_a_mismatch_not_free(self):
        from stores import SlotIntegrityError, assert_slot_integrity
        for bad in (None, "", "not-json", '{"runtime_uuid": "x"}', "[]"):
            with self.assertRaises(SlotIntegrityError, msg=repr(bad)):
                assert_slot_integrity(runtime_uuid=RUUID, slot=1, generation=1, marker_raw=bad)

    def test_marker_roundtrips_and_carries_generation(self):
        from stores import parse_owner_marker
        m = parse_owner_marker(self._marker(slot=3, generation=7))
        self.assertEqual((m["runtime_uuid"], m["slot"], m["generation"]), (RUUID, 3, 7))

    def test_integrity_error_is_a_sanitised_agent_error(self):
        from lib.mgmt_agent_core import AgentError
        from stores import SlotIntegrityError
        self.assertIsInstance(SlotIntegrityError(), AgentError)


class GenerationMonotonicityTests(SimpleTestCase):
    """new_generation == previous_generation + 1, always. No repeat, no gap, no decrease.
    A violation is a PERMANENT integrity failure: fail closed, quarantine, operator review — never repair."""

    @staticmethod
    def _store(pool_size=2):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=pool_size)

    def test_ledger_is_contiguous_across_occupancies(self):
        s = self._store()
        u2 = "11111111-2222-3333-4444-555555555555"
        slot, _ = s.assign(RUUID, now=1)
        s.release(RUUID, now=2)
        s.assign(u2, now=3); s.release(u2, now=4)
        s.assert_generation_monotonic(slot, 3)          # 1 -> 2 -> 3, no raise

    def test_current_generation_must_match_expected(self):
        from stores import SlotIntegrityError
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        with self.assertRaises(SlotIntegrityError):
            s.assert_generation_monotonic(slot, gen + 5)

    def test_tampered_generation_is_detected_against_the_ledger(self):
        """A rolled-forward slots.generation with no matching ledger entry must fail closed."""
        from stores import SlotIntegrityError
        s = self._store()
        slot, _ = s.assign(RUUID, now=1)
        s._conn.execute("UPDATE slots SET generation=99 WHERE slot=?", (slot,)); s._conn.commit()
        with self.assertRaises(SlotIntegrityError):
            s.assert_generation_monotonic(slot, 99)

    def test_gap_in_the_ledger_is_detected(self):
        from stores import SlotIntegrityError
        s = self._store()
        slot, _ = s.assign(RUUID, now=1)
        s._conn.execute("INSERT INTO slot_generations (slot, generation, event, at) VALUES (?,?,?,?)",
                        (slot, 5, "forged", 0))
        s._conn.execute("UPDATE slots SET generation=5 WHERE slot=?", (slot,)); s._conn.commit()
        with self.assertRaises(SlotIntegrityError):
            s.assert_generation_monotonic(slot, 5)

    def test_unknown_slot_fails_closed(self):
        from stores import SlotIntegrityError
        s = self._store()
        with self.assertRaises(SlotIntegrityError):
            s.assert_generation_monotonic(99, 1)


class OccupancyIntegrityGateTests(SimpleTestCase):
    """The full pre-mutation gate: quarantine -> 4-way agreement -> monotonicity, quarantining on failure."""

    @staticmethod
    def _store():
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=2)

    def test_healthy_occupancy_passes(self):
        from stores import format_owner_marker
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        s.assert_occupancy_integrity(runtime_uuid=RUUID, slot=slot, generation=gen,
                                     marker_raw=format_owner_marker(RUUID, slot, gen), now=2)
        self.assertEqual(s.quarantined_slots(), {})     # healthy path must NOT quarantine

    def test_stale_marker_quarantines_the_slot_and_fails_closed(self):
        from stores import SlotIntegrityError, format_owner_marker
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        stale = format_owner_marker(RUUID, slot, gen - 1 if gen > 1 else 99)
        with self.assertRaises(SlotIntegrityError):
            s.assert_occupancy_integrity(runtime_uuid=RUUID, slot=slot, generation=gen,
                                         marker_raw=stale, now=2)
        self.assertIn(slot, s.quarantined_slots())

    def test_quarantined_slot_is_refused_even_when_everything_else_agrees(self):
        """Quarantine is not silently repaired — a subsequent healthy request is still refused."""
        from stores import SlotIntegrityError, format_owner_marker
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        s.quarantine_slot(slot, "manual", 1)
        with self.assertRaises(SlotIntegrityError):
            s.assert_occupancy_integrity(runtime_uuid=RUUID, slot=slot, generation=gen,
                                         marker_raw=format_owner_marker(RUUID, slot, gen), now=2)


class VerificationEvidenceTests(SimpleTestCase):
    """Generation is first-class report evidence, not an implementation detail."""

    def test_report_carries_every_required_field(self):
        from stores import build_verification_evidence, format_owner_marker
        marker = format_owner_marker(RUUID, 2, 4)
        ev = build_verification_evidence(
            runtime_uuid=RUUID, slot=2, generation=4,
            canonical_dir=r"C:\GuvFX\beta\slots\2\terminal", marker_raw=marker,
            pid=13020, session_id=1, manifest_version="2026-07-22.3", protocol_version=1,
            verified_at=1700000000, started_at=1699999999)
        for field in ("runtime_uuid", "slot", "generation", "owner_marker_digest", "canonical_path",
                      "pid", "session_id", "manifest_version", "protocol_version", "verified_at",
                      "started_at"):
            self.assertIn(field, ev, field)
        self.assertEqual((ev["slot"], ev["generation"]), (2, 4))

    def test_marker_digest_is_a_digest_not_the_contents(self):
        from stores import build_verification_evidence, format_owner_marker
        marker = format_owner_marker(RUUID, 2, 4)
        ev = build_verification_evidence(runtime_uuid=RUUID, slot=2, generation=4,
                                         canonical_dir="x", marker_raw=marker)
        self.assertEqual(len(ev["owner_marker_digest"]), 12)
        self.assertNotIn(RUUID, ev["owner_marker_digest"])

    def test_response_allowlist_now_carries_generation_identity(self):
        """Superseded contract: canonical_path was REMOVED by the hardening requirement — the channel
        carries occupancy identity plus containment attestations, not the filesystem layout."""
        from lib.mgmt_agent_core import _RESPONSE_ALLOWLIST
        for f in ("slot", "generation", "owner_marker_digest", "canonical_path_digest"):
            self.assertIn(f, _RESPONSE_ALLOWLIST, f)
        self.assertNotIn("canonical_path", _RESPONSE_ALLOWLIST)


def _proofs(**over):
    from stores import SlotStore
    p = {k: True for k in SlotStore.RELEASE_PROOFS}
    p.update(over)
    return p


class RemoteEvidenceBoundaryTests(SimpleTestCase):
    """The complete local filesystem path must not cross the management channel."""

    def _local(self):
        from stores import build_verification_evidence, format_owner_marker
        return build_verification_evidence(
            runtime_uuid=RUUID, slot=2, generation=4,
            canonical_dir=r"C:\GuvFX\beta\slots\2\terminal",
            marker_raw=format_owner_marker(RUUID, 2, 4), pid=13020, session_id=1,
            manifest_version="m", protocol_version=1, verified_at=1,
            path_containment_verified=True, executable_containment_verified=True)

    def test_local_report_retains_the_full_path(self):
        self.assertEqual(self._local()["canonical_path"], r"C:\GuvFX\beta\slots\2\terminal")

    def test_remote_projection_strips_the_full_path_but_keeps_the_attestation(self):
        from stores import remote_evidence
        r = remote_evidence(self._local())
        self.assertNotIn("canonical_path", r)
        self.assertNotIn("GuvFX", json.dumps(r))          # no filesystem layout leaks at all
        for f in ("slot", "generation", "runtime_uuid", "canonical_path_digest",
                  "path_containment_verified", "executable_containment_verified"):
            self.assertIn(f, r, f)
        self.assertTrue(r["path_containment_verified"] and r["executable_containment_verified"])

    def test_response_allowlist_matches_the_remote_contract(self):
        from lib.mgmt_agent_core import _RESPONSE_ALLOWLIST
        self.assertNotIn("canonical_path", _RESPONSE_ALLOWLIST)
        for f in ("canonical_path_digest", "path_containment_verified",
                  "executable_containment_verified"):
            self.assertIn(f, _RESPONSE_ALLOWLIST, f)


class AuditPropagationTests(SimpleTestCase):
    """Every material lifecycle event carries occupancy identity."""

    @staticmethod
    def _store():
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=2)

    def test_audit_requires_slot_and_generation(self):
        s = self._store()
        for bad in ({"slot": None, "generation": 1}, {"slot": 1, "generation": None}):
            with self.assertRaises(ValueError):
                s.record_audit(event="slot_assigned", runtime_uuid=RUUID, **bad)

    def test_unknown_event_rejected(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.record_audit(event="not_a_real_event", runtime_uuid=RUUID, slot=1, generation=1)

    def test_all_required_lifecycle_events_are_supported(self):
        from stores import SlotStore
        for e in ("slot_assigned", "materialise_started", "materialise_completed", "runtime_started",
                  "verification_completed", "stop_requested", "stop_completed", "tombstone_completed",
                  "slot_released", "integrity_mismatch", "slot_quarantined", "quarantine_cleared"):
            self.assertIn(e, SlotStore.AUDIT_EVENTS, e)

    def test_history_is_never_attributed_across_generations(self):
        """The core rule: a previous occupant's events must not be readable as the current one's."""
        s = self._store()
        u2 = "11111111-2222-3333-4444-555555555555"
        slot, g1 = s.assign(RUUID, now=1)
        s.record_audit(event="runtime_started", runtime_uuid=RUUID, slot=slot, generation=g1, now=1)
        s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=g1, proofs=_proofs(), now=2)
        _, g2 = s.assign(u2, now=3)
        s.record_audit(event="runtime_started", runtime_uuid=u2, slot=slot, generation=g2, now=3)
        cur = s.audit_for_occupancy(slot, g2)
        self.assertEqual([e["runtime_uuid"] for e in cur], [u2])   # previous occupant absent
        self.assertEqual([e["runtime_uuid"] for e in s.audit_for_occupancy(slot, g1)], [RUUID])

    def test_audit_carries_the_full_field_set(self):
        s = self._store()
        s.record_audit(event="verification_completed", runtime_uuid=RUUID, slot=1, generation=1,
                       operation="VERIFY", provisioning_job_id=42, correlation_id="c-1",
                       prior_state="STARTED", resulting_state="VERIFIED", integrity_outcome="ok",
                       quarantined=False, agent_version="a", manifest_version="m", protocol_version=1,
                       now=5)
        ev = s.audit_for_occupancy(1, 1)[0]
        self.assertEqual((ev["operation"], ev["prior_state"], ev["resulting_state"],
                          ev["integrity_outcome"]), ("VERIFY", "STARTED", "VERIFIED", "ok"))


class ReleaseOrderTests(SimpleTestCase):
    """A generation may advance only after all seven proofs; the slot is never exposed mid-release."""

    @staticmethod
    def _store():
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=1)

    def test_all_seven_proofs_required(self):
        from stores import ReleaseProofMissing, SlotStore
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        for proof in SlotStore.RELEASE_PROOFS:
            with self.assertRaises(ReleaseProofMissing, msg=proof) as ctx:
                s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=gen,
                                          proofs=_proofs(**{proof: False}), now=2)
            self.assertIn(proof, ctx.exception.missing)
            self.assertEqual(ctx.exception.reason_code, "release_proof_missing")

    def test_slot_is_not_exposed_to_another_runtime_before_advancement(self):
        from stores import PoolExhausted
        s = self._store()                                   # pool_size=1
        slot, gen = s.assign(RUUID, now=1)
        try:
            s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=gen,
                                      proofs=_proofs(no_mutation_lock_held=False), now=2)
        except Exception:
            pass
        with self.assertRaises(PoolExhausted):              # still occupied — never handed out
            s.assign("11111111-2222-3333-4444-555555555555", now=3)
        self.assertEqual(s.generation_of(slot), gen)        # generation unadvanced

    def test_successful_release_advances_by_exactly_one_and_frees_the_slot(self):
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        self.assertEqual(s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=gen,
                                                   proofs=_proofs(), now=2), gen + 1)
        self.assertIsNone(s.lookup(RUUID))
        s.assert_generation_monotonic(slot, gen + 1)
        self.assertEqual(s.assign("11111111-2222-3333-4444-555555555555", now=3)[1], gen + 1)

    def test_stale_caller_view_fails_closed(self):
        from stores import SlotIntegrityError
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        with self.assertRaises(SlotIntegrityError):
            s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=gen + 1,
                                      proofs=_proofs(), now=2)


class QuarantineClearanceTests(SimpleTestCase):
    """Clearance is an evidenced operator action, never a boolean reset."""

    @staticmethod
    def _store():
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=2)

    def _ok(self, **over):
        args = dict(diagnosed_reason="stale marker after manual copy", operator_identity="nuno",
                    evidence_reference="EV-123", reconciliation_confirmed=True,
                    no_runtime_process_confirmed=True, slot_directory_safe_confirmed=True)
        args.update(over)
        return args

    def test_missing_evidence_is_refused(self):
        from stores import QuarantineClearanceRefused
        s = self._store(); slot, _ = s.assign(RUUID, now=1); s.quarantine_slot(slot, "x", 1)
        for field in ("diagnosed_reason", "operator_identity", "evidence_reference"):
            with self.assertRaises(QuarantineClearanceRefused, msg=field):
                s.clear_quarantine(slot=slot, **self._ok(**{field: ""}))
        for field in ("reconciliation_confirmed", "no_runtime_process_confirmed",
                      "slot_directory_safe_confirmed"):
            with self.assertRaises(QuarantineClearanceRefused, msg=field):
                s.clear_quarantine(slot=slot, **self._ok(**{field: False}))
        self.assertTrue(s.is_quarantined(slot))          # still quarantined after every refusal

    def test_full_evidence_clears_and_emits_an_auditable_event(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1); s.quarantine_slot(slot, "x", 1)
        s.clear_quarantine(slot=slot, now=9, **self._ok())
        self.assertFalse(s.is_quarantined(slot))
        events = [e["event"] for e in s.audit_for_occupancy(slot, gen)]
        self.assertIn("quarantine_cleared", events)

    def test_clearance_does_not_rewrite_history(self):
        s = self._store()
        slot, gen = s.assign(RUUID, now=1)
        s.release_after_tombstone(runtime_uuid=RUUID, slot=slot, generation=gen, proofs=_proofs(), now=2)
        before = [r[0] for r in s._conn.execute(
            "SELECT generation FROM slot_generations WHERE slot=? ORDER BY generation", (slot,))]
        s.quarantine_slot(slot, "x", 3)
        s.clear_quarantine(slot=slot, now=4, **self._ok())
        after = [r[0] for r in s._conn.execute(
            "SELECT generation FROM slot_generations WHERE slot=? ORDER BY generation", (slot,))]
        self.assertEqual(before, after)                  # ledger untouched

    def test_clearing_a_healthy_slot_is_refused(self):
        from stores import QuarantineClearanceRefused
        s = self._store(); slot, _ = s.assign(RUUID, now=1)
        with self.assertRaises(QuarantineClearanceRefused):
            s.clear_quarantine(slot=slot, **self._ok())


class OccupancyIdTests(SimpleTestCase):
    """Immutable, deterministic single reference for one occupancy."""

    def test_deterministic_and_recomputable(self):
        from stores import occupancy_id
        self.assertEqual(occupancy_id(2, 5), occupancy_id(2, 5))
        self.assertEqual(occupancy_id("2", "5"), occupancy_id(2, 5))   # normalised

    def test_distinct_per_slot_and_per_generation(self):
        from stores import occupancy_id
        ids = {occupancy_id(1, 1), occupancy_id(1, 2), occupancy_id(2, 1), occupancy_id(2, 2)}
        self.assertEqual(len(ids), 4)

    def test_present_in_report_remote_evidence_audit_and_quarantine(self):
        from stores import (SlotStore, build_verification_evidence, format_owner_marker, occupancy_id,
                            remote_evidence)
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        s = SlotStore(f.name, pool_size=2)
        slot, gen = s.assign(RUUID, now=1)
        expected = occupancy_id(slot, gen)
        local = build_verification_evidence(runtime_uuid=RUUID, slot=slot, generation=gen,
                                            canonical_dir="d", marker_raw=format_owner_marker(RUUID, slot, gen))
        self.assertEqual(local["occupancy_id"], expected)                       # Verification Report
        self.assertEqual(remote_evidence(local)["occupancy_id"], expected)      # remote evidence
        s.record_audit(event="runtime_started", runtime_uuid=RUUID, slot=slot, generation=gen, now=1)
        self.assertEqual(s.audit_for_occupancy(slot, gen)[0]["occupancy_id"], expected)   # audit
        s.quarantine_slot(slot, "x", 2)
        self.assertEqual(s.quarantined_slots()[slot]["occupancy_id"], expected)           # quarantine

    def test_in_response_allowlist(self):
        from lib.mgmt_agent_core import _RESPONSE_ALLOWLIST
        self.assertIn("occupancy_id", _RESPONSE_ALLOWLIST)


class AuditChainTests(SimpleTestCase):
    """Forward-linked chain detects accidental deletion, insertion and reordering. No auto-repair."""

    @staticmethod
    def _store_with(n=4):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        s = SlotStore(f.name, pool_size=2)
        slot, gen = s.assign(RUUID, now=1)
        for i, ev in enumerate(("slot_assigned", "materialise_started", "materialise_completed",
                                "runtime_started")[:n]):
            s.record_audit(event=ev, runtime_uuid=RUUID, slot=slot, generation=gen, now=i + 1)
        return s, slot, gen

    def test_healthy_chain_is_valid(self):
        s, _, _ = self._store_with()
        self.assertEqual(s.verify_audit_chain()["status"], "VALID")
        self.assertEqual(s.verify_audit_chain()["records"], 4)

    def test_empty_chain_is_valid(self):
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        self.assertEqual(SlotStore(f.name, pool_size=1).verify_audit_chain()["status"], "VALID")

    def test_deletion_is_detected(self):
        from stores import AuditChainCorrupt
        s, _, _ = self._store_with()
        s._conn.execute("DELETE FROM slot_audit WHERE id=2"); s._conn.commit()
        with self.assertRaises(AuditChainCorrupt) as ctx:
            s.verify_audit_chain()
        self.assertEqual(ctx.exception.reason_code, "audit_chain_corrupt")

    def test_content_modification_is_detected(self):
        from stores import AuditChainCorrupt
        s, _, _ = self._store_with()
        s._conn.execute("UPDATE slot_audit SET resulting_state='TAMPERED' WHERE id=2"); s._conn.commit()
        with self.assertRaises(AuditChainCorrupt):
            s.verify_audit_chain()

    def test_insertion_is_detected(self):
        from stores import AuditChainCorrupt
        s, slot, gen = self._store_with()
        s._conn.execute(
            "INSERT INTO slot_audit (event, runtime_uuid, slot, generation, occupancy_id, quarantined,"
            " at, previous_audit_hash, audit_hash) VALUES ('runtime_started',?,?,?,'x',0,9,'bogus','bogus')",
            (RUUID, slot, gen))
        s._conn.commit()
        with self.assertRaises(AuditChainCorrupt):
            s.verify_audit_chain()

    def test_verification_never_repairs(self):
        from stores import AuditChainCorrupt
        s, _, _ = self._store_with()
        s._conn.execute("UPDATE slot_audit SET resulting_state='TAMPERED' WHERE id=2"); s._conn.commit()
        for _ in range(2):
            with self.assertRaises(AuditChainCorrupt):
                s.verify_audit_chain()
        still = s._conn.execute("SELECT resulting_state FROM slot_audit WHERE id=2").fetchone()[0]
        self.assertEqual(still, "TAMPERED")     # untouched — operator investigation only


class OccupancyBindingTests(SimpleTestCase):
    """The upper layer holds the full binding; a primitive sees only slot-scoped physical facts."""

    @staticmethod
    def _binding(slot=2, gen=3, path=r"C:\GuvFX\beta\slots\2\terminal"):
        from occupancy import build_occupancy_binding
        return build_occupancy_binding(
            runtime_uuid=RUUID, slot=slot, generation=gen, slot_path=path,
            task_identity={"task_name": "GuvFXBetaRuntime-2"}, integrity_outcome="ok", quarantined=False)

    def test_binding_carries_all_required_fields(self):
        from occupancy import BINDING_FIELDS
        b = self._binding()
        for f in BINDING_FIELDS:
            self.assertIn(f, b, f)

    def test_primitive_view_cannot_carry_policy_or_identity(self):
        """The enforcement point for the boundary rule: a primitive literally cannot see these."""
        from occupancy import slot_scoped_view
        v = slot_scoped_view(self._binding(), slot_path=r"C:\GuvFX\beta\slots\2\terminal",
                             launch_task="GuvFXBetaRuntime-2", terminate_task="GuvFXBetaRuntimeStop-2")
        for forbidden in ("runtime_uuid", "generation", "occupancy_id", "provisioning_job_id",
                          "integrity_outcome", "quarantined"):
            self.assertNotIn(forbidden, v, forbidden)
        self.assertEqual(set(v), {"slot", "slot_path", "launch_task", "terminate_task"})

    def test_result_from_a_different_slot_is_rejected(self):
        from occupancy import OccupancyBindingMismatch, reconcile_primitive_result
        path = r"C:\GuvFX\beta\slots\2\terminal"
        with self.assertRaises(OccupancyBindingMismatch):
            reconcile_primitive_result(self._binding(), {"slot": 3}, slot_path=path)

    def test_result_from_a_different_path_is_rejected(self):
        from occupancy import OccupancyBindingMismatch, reconcile_primitive_result
        path = r"C:\GuvFX\beta\slots\2\terminal"
        with self.assertRaises(OccupancyBindingMismatch):
            reconcile_primitive_result(self._binding(), {"slot": 2, "slot_path": r"C:\elsewhere"},
                                       slot_path=path)

    def test_matching_result_reconciles(self):
        from occupancy import reconcile_primitive_result
        path = r"C:\GuvFX\beta\slots\2\terminal"
        reconcile_primitive_result(self._binding(), {"slot": 2, "slot_path": path}, slot_path=path)


class ProcessBirthIdentityTests(SimpleTestCase):
    """PID alone is not identity — PID reuse must not attribute a later process to an earlier occupancy."""

    @staticmethod
    def _birth(**over):
        from occupancy import build_process_birth
        args = dict(pid=13020, created_at="2026-07-22T08:00:00Z", image_digest="abc123",
                    executable_containment_verified=True, user_sid="S-1-5-21-x-1001",
                    session_id=1, slot=2)
        args.update(over)
        return build_process_birth(**args)

    def test_identical_process_matches(self):
        from occupancy import assert_same_process
        b = self._birth()
        assert_same_process(b, dict(b))

    def test_pid_reuse_with_a_different_creation_time_fails_closed(self):
        """THE case this exists for: same PID, later unrelated process."""
        from occupancy import ProcessIdentityMismatch, assert_same_process
        b = self._birth()
        later = dict(b, created_at="2026-07-22T09:30:00Z")
        with self.assertRaises(ProcessIdentityMismatch) as ctx:
            assert_same_process(b, later)
        self.assertEqual(ctx.exception.reason_code, "process_identity_mismatch")

    def test_different_owner_image_session_or_slot_fails_closed(self):
        from occupancy import ProcessIdentityMismatch, assert_same_process
        b = self._birth()
        for field, value in (("user_sid", "S-1-5-21-x-9999"), ("image_digest", "deadbeef"),
                             ("session_id", 2), ("slot", 3)):
            with self.assertRaises(ProcessIdentityMismatch, msg=field):
                assert_same_process(b, dict(b, **{field: value}))

    def test_unverified_executable_containment_fails_closed(self):
        from occupancy import ProcessIdentityMismatch, assert_same_process
        b = self._birth()
        with self.assertRaises(ProcessIdentityMismatch):
            assert_same_process(b, dict(b, executable_containment_verified=False))


class TaskIdentityTests(SimpleTestCase):
    """Task-definition drift blocks launch; the agent never repairs a task at runtime."""

    @staticmethod
    def _defn(**over):
        d = dict(task_name="GuvFXBetaRuntime-2", run_as_identity="guvfx_u_beta_2",
                 executable=r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe",
                 working_directory=r"C:\GuvFX\beta\slots\2\terminal",
                 logon_type="TASK_LOGON_PASSWORD", run_level="LEAST", enabled=True)
        d.update(over)
        return d

    def test_matching_definition_passes(self):
        from occupancy import assert_task_matches_approved
        assert_task_matches_approved(self._defn(), self._defn())

    def test_any_drift_blocks_launch(self):
        from occupancy import TaskDefinitionDrift, assert_task_matches_approved
        for field, value in (("run_as_identity", "Administrator"),
                             ("executable", r"C:\Windows\System32\cmd.exe"),
                             ("working_directory", r"C:\elsewhere"),
                             ("logon_type", "TASK_LOGON_INTERACTIVE_TOKEN"),
                             ("run_level", "HIGHEST"),
                             ("task_name", "SomethingElse")):
            with self.assertRaises(TaskDefinitionDrift, msg=field) as ctx:
                assert_task_matches_approved(self._defn(), self._defn(**{field: value}))
            self.assertEqual(ctx.exception.reason_code, "task_definition_drift")

    def test_disabled_task_blocks_launch(self):
        from occupancy import TaskDefinitionDrift, assert_task_matches_approved
        with self.assertRaises(TaskDefinitionDrift):
            assert_task_matches_approved(self._defn(), self._defn(enabled=False))

    def test_digest_is_order_independent(self):
        from occupancy import task_definition_digest
        d = self._defn()
        self.assertEqual(task_definition_digest(d),
                         task_definition_digest(dict(reversed(list(d.items())))))


class AuditCheckpointTests(SimpleTestCase):
    """Chain verification is a lifecycle gate, not a reporting nicety."""

    @staticmethod
    def _store():
        from stores import SlotStore
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=2)

    def test_all_five_boundaries_are_defined(self):
        from stores import SlotStore
        for b in ("before_assign", "before_mutation", "before_release",
                  "before_quarantine_clearance", "before_acceptance_evidence"):
            self.assertIn(b, SlotStore.AUDIT_CHECKPOINTS, b)

    def test_healthy_chain_passes_every_checkpoint(self):
        from stores import SlotStore
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        s.record_audit(event="slot_assigned", runtime_uuid=RUUID, slot=slot, generation=gen, now=1)
        for b in SlotStore.AUDIT_CHECKPOINTS:
            self.assertEqual(s.audit_checkpoint(b, slot=slot)["status"], "VALID")

    def test_corruption_with_attribution_quarantines_the_slot(self):
        from stores import AuditChainCorrupt
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        s.record_audit(event="slot_assigned", runtime_uuid=RUUID, slot=slot, generation=gen, now=1)
        s._conn.execute("UPDATE slot_audit SET resulting_state='X' WHERE id=1"); s._conn.commit()
        with self.assertRaises(AuditChainCorrupt):
            s.audit_checkpoint("before_mutation", slot=slot, now=2)
        self.assertIn(slot, s.quarantined_slots())

    def test_corruption_without_attribution_blocks_all_allocation(self):
        from stores import AllocationBlocked, AuditChainCorrupt
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        s.record_audit(event="slot_assigned", runtime_uuid=RUUID, slot=slot, generation=gen, now=1)
        s._conn.execute("UPDATE slot_audit SET resulting_state='X' WHERE id=1"); s._conn.commit()
        with self.assertRaises(AuditChainCorrupt):
            s.audit_checkpoint("before_assign", now=2)       # attribution uncertain
        self.assertTrue(s.allocation_blocked())
        with self.assertRaises(AllocationBlocked):
            s.assign("11111111-2222-3333-4444-555555555555", now=3)

    def test_allocation_block_clearance_requires_operator_attribution(self):
        from stores import QuarantineClearanceRefused
        s = self._store()
        s.block_allocation("x", 1)
        with self.assertRaises(QuarantineClearanceRefused):
            s.clear_allocation_block(operator_identity="", evidence_reference="E-1")
        s.clear_allocation_block(operator_identity="nuno", evidence_reference="E-1")
        self.assertIsNone(s.allocation_blocked())

    def test_unknown_checkpoint_rejected(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.audit_checkpoint("whenever")
