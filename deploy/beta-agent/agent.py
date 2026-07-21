"""CVM-Inc-3 B2/B3P-1 — beta provisioning agent service (private-network HTTP wrapper around the agent core).

Exposes exactly ONE endpoint (POST /provision) that hands a fixed-schema signed request to the boundary-
enforcing agent core. Binds only to the EXACT configured private/Tailscale address (startup fails otherwise).
No other route exists — there is no way to submit a command, path, script or argument.

B3P-1 hardening (verification):
 - B-9: the live bind is pinned to the single expected management address (``config.load_config``).
 - resource exhaustion: an oversize ``Content-Length`` is refused BEFORE the body is read, keep-alive is
   disabled (one request per connection), each connection has a per-recv socket timeout, and concurrent
   connections are capped — so an UNAUTHENTICATED peer cannot exhaust the RAM / thread budget of the box that
   also runs Nuno's live terminal + bridge. (The cap bounds the HOST budget; :8791 is additionally reachable
   only from the backend via the firewall rule + Tailscale ACL, so a deliberate slow client is doubly gated.)
 - B-6 drain: ``AgentServer.stop`` stops accepting new work, then waits for in-flight mutating ops to finish
   before closing — so ``sc stop`` cannot kill a mutation mid-flight.

Run:  python agent.py     (config from the environment; see config.example.json + RUNBOOK.md)
The SCM-managed form is ``service.py`` (pywin32). This file is a DARK artefact — never started, firewalled or
scheduled until the controlled B3 install.
"""
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)   # so the bundled ``lib`` package + agent modules import cleanly

from lib.mgmt_agent_core import BetaProvisioningAgent   # noqa: E402  bundled Django-free agent core
import config as agent_config                            # noqa: E402
import manifest as agent_manifest                        # noqa: E402
from op_impls import OpImplementations                   # noqa: E402
from stores import RuntimeLockManager, SqliteStore       # noqa: E402
from win_ops import RealWindowsOps                        # noqa: E402

AGENT_VERSION = "beta-agent-1.0.0"


def build_agent(cfg: dict, *, win=None, store=None, locks=None, manifest_path: str = "",
                enforce_integrity: bool = False) -> BetaProvisioningAgent:
    """Assemble the boundary-enforcing agent from config + the approved manifest. Injectable (win/store/
    locks) for tests; defaults to the real Windows ops + the SQLite state store. ``enforce_integrity``
    (used by the live service) hashes every implementation module on disk NOW and refuses to build if ANY
    drifts from the manifest — this is the fresh-disk check. The request-time per-op gate then re-affirms
    this START-TIME snapshot on every mutation (so a drift caught at start also blocks each op); fresh-disk
    re-verification happens at the next (re)start, so on-disk tampering after start is caught on restart, not
    per-request. The agent dir is ACL-scoped to the service account + admins, so tampering already requires
    high privilege."""
    manifest_path = manifest_path or cfg.get("manifest_path") or os.path.join(_HERE, "manifest.json")
    approved = agent_manifest.load_manifest(manifest_path)
    actual = agent_manifest.compute_checksums(_HERE)
    if enforce_integrity and not agent_manifest.integrity_ok(approved.get("checksums", {}), actual):
        raise RuntimeError("agent implementation integrity check failed — refusing to start")
    script_manifest = agent_manifest.build_script_manifest(
        approved.get("checksums", {}), actual, approved.get("supported_operations", []))

    win = win if win is not None else RealWindowsOps()
    store = store if store is not None else SqliteStore(cfg["state_db"])
    locks = locks if locks is not None else RuntimeLockManager()
    impls = OpImplementations(win, tombstone_base=cfg["tombstone_base"]).as_dict()

    return BetaProvisioningAgent(
        keyring=cfg["keyring"], nonce_store=store, idempotency_store=store, op_impls=impls,
        agent_version=AGENT_VERSION,
        script_manifest=script_manifest,
        script_versions={f"op_{op.lower()}": approved.get("manifest_version", "")
                         for op in approved.get("supported_operations", [])},
        resolve_real_path=win.real_path, runtime_locks=locks,
        base=cfg["beta_root"], manifest_version=approved.get("manifest_version", ""))


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server that BOUNDS concurrent connections (refuses, never queues, past the cap) and
    carries the per-request body/timeout limits. Prevents an unauthenticated flood from spawning unbounded
    threads on the shared live host (verification: pre-auth resource exhaustion)."""
    daemon_threads = True

    def __init__(self, addr, handler, *, max_body_bytes: int, request_timeout_s: float,
                 max_connections: int):
        super().__init__(addr, handler)
        self.max_body_bytes = int(max_body_bytes)
        self.request_timeout_s = float(request_timeout_s)
        self._conn_sem = threading.BoundedSemaphore(max(1, int(max_connections)))

    def process_request(self, request, client_address):
        if not self._conn_sem.acquire(blocking=False):
            # over the concurrency cap — refuse without spawning a handler thread
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)   # spawns the worker thread
        except BaseException:
            # the worker thread never started (e.g. "can't start new thread" under load) → it will NOT run
            # process_request_thread, so release the permit HERE to avoid permanently leaking it (which would
            # otherwise wedge the concurrency gate fully closed after ``max_connections`` such failures).
            self._conn_sem.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_sem.release()


def make_handler(agent: BetaProvisioningAgent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # bound how long a single (possibly slow-loris) connection may hold a thread
            try:
                self.connection.settimeout(self.server.request_timeout_s)
            except OSError:
                pass

        def log_message(self, *a):  # no request logging (never log request bodies / paths)
            return

        def _send(self, obj, code=200):
            # One request per connection: forcing close means a slow/keep-alive client cannot hold a
            # concurrency permit across multiple requests, and an early-return (413/400/404) path that did not
            # consume the request body cannot desync a persistent connection.
            self.close_connection = True
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/provision":
                return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)
            # Bound the body BEFORE reading it: the signed provision request is a few KB, so a large
            # Content-Length is refused up front (413) rather than read into memory (verification).
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            if length < 0:
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            if length > self.server.max_body_bytes:
                return self._send({"outcome": "denied", "reason_code": "request_too_large"}, 413)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            resp = agent.handle(req)     # the agent core NEVER raises; always a sanitised dict
            self._send(resp, 200)

        def do_GET(self):                # no read routes — negotiation is a signed POST
            return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)

    return Handler


def _make_logger(log_dir: str | None) -> logging.Logger:
    """Lifecycle logger (start/stop/drain-timeout only — NEVER request bodies/paths/nonces/secrets) writing a
    rotating file under ``log_dir`` so the state/log relocation is realised, not just declared."""
    logger = logging.getLogger("beta-agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False           # the agent's operational log stays in its own file, not the root logger
    if log_dir:
        path = os.path.join(log_dir, "agent.log")
        if not any(getattr(h, "_beta_path", None) == path for h in logger.handlers):
            try:
                os.makedirs(log_dir, exist_ok=True)
                h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                                         encoding="utf-8")
                h._beta_path = path                                   # de-dupe handlers across instances
                h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                logger.addHandler(h)
            except OSError:
                pass
    return logger


class AgentServer:
    """Testable lifecycle controller for the agent HTTP server. Builds the agent (integrity-enforced),
    serves in a background thread, and stops with a DRAIN: it first stops accepting new work, then waits for
    in-flight mutating ops to finish (up to ``drain_timeout_s``) before shutting the socket, so ``sc stop``
    cannot kill a MATERIALISE/TOMBSTONE mid-flight (verification B-6). The pywin32 SCM wrapper (``service.py``)
    is a thin delegate to this."""

    def __init__(self, cfg: dict, *, win=None, store=None, locks=None, enforce_integrity: bool = True):
        self.cfg = cfg
        self._locks = locks if locks is not None else RuntimeLockManager()
        self._agent = build_agent(cfg, win=win, store=store, locks=self._locks,
                                  enforce_integrity=enforce_integrity)
        self._log = _make_logger(cfg.get("log_dir"))
        self._httpd = None
        self._thread = None

    def make_server(self) -> BoundedThreadingHTTPServer:
        agent_config.assert_exact_bind(
            self.cfg["bind_host"],
            self.cfg.get("expected_bind_host", agent_config.DEFAULT_EXPECTED_BIND_HOST))
        return BoundedThreadingHTTPServer(
            (self.cfg["bind_host"], self.cfg["bind_port"]), make_handler(self._agent),
            max_body_bytes=self.cfg["max_body_bytes"], request_timeout_s=self.cfg["request_timeout_s"],
            max_connections=self.cfg["max_connections"])

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = self.make_server()
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="beta-agent-http",
                                        daemon=True)
        self._thread.start()
        self._log.info("agent started bind=%s:%s", self.cfg["bind_host"], self.cfg["bind_port"])

    def stop(self, drain_timeout_s: float | None = None) -> bool:
        """Stop accepting new work, drain in-flight mutating ops (bounded), then shut down. Returns True if
        fully drained, False if the drain timed out (shutdown still proceeds; the SCM logs the forced stop).

        Order matters (verification B-6): begin_drain() refuses any mutation that has not yet committed, and
        shutdown() exits the accept loop, BEFORE we wait — so no new mutation can start during the drain
        window and then be killed at teardown."""
        self._locks.begin_drain()                     # refuse new mutations (denied, not killed)
        if self._httpd is not None:
            self._httpd.shutdown()                    # stop accepting; serve_forever thread returns
        drained = self._await_drain(
            self.cfg["drain_timeout_s"] if drain_timeout_s is None else drain_timeout_s)
        if self._httpd is not None:
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        (self._log.warning if not drained else self._log.info)(
            "agent stopped%s", "" if drained else " (drain timed out — forced)")
        return drained

    def _await_drain(self, timeout_s: float) -> bool:
        deadline = time.time() + max(0.0, timeout_s)
        while self._locks.active_mutations() > 0:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)
        return True


def main():
    cfg = agent_config.load_config()             # raises unless the bind host is the EXACT expected private address
    server = AgentServer(cfg, enforce_integrity=True)
    server.start()
    try:
        while server._thread is not None and server._thread.is_alive():
            server._thread.join(timeout=1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
