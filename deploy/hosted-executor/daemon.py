"""Beta Readiness Stream 7C - the hosted signed-executor daemon (private-network HTTP wrapper around dispatch).

The runnable host end of the Stream 5 signed provisioning transport. It exposes exactly ONE mutating endpoint
(``POST /hosted/provision``) that hands a signed, fixed-schema request to ``host_agent_dispatch.dispatch`` -
which verifies it (HMAC + nonce + skew/expiry), refuses Customer Zero, derives identity/paths from
``account_id`` server-side, maps the allow-listed operation to exactly ONE reviewed ``.ps1`` primitive, runs it,
and returns a SIGNED response. There is no route, header, or field through which a command, script, path,
username, or task definition can be submitted.

Run (production): ONLY under the ``GuvFXHostedExecutor`` WinSW service (RULE 1 - never ``Start-Process``/``nohup``
over SSH: an ad-hoc process is session-bound, unsupervised, and dies when its launcher ends). A bare
``python daemon.py`` is OFFLINE/dev only.

Hardening mirrors the proven beta agent (``deploy/beta-agent/agent.py``): the bind is pinned to the EXACT
expected private address; an oversize body is refused before it is read; keep-alive is disabled; concurrent
connections are capped (an unauthenticated peer cannot exhaust the shared host that also runs Customer Zero);
the single-instance guard is the EXCLUSIVE OS bind; a stop DRAINS in-flight work; and an abnormal serve-loop
exit is non-zero so the supervisor restarts it.
"""
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
for _p in (_HERE, _LIB):                # bundle root (config/nonce/runner/envelope) + lib (vendored + staged)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import daemon_config                                        # noqa: E402
from envelope_open import make_envelope_opener              # noqa: E402
from nonce_store import SqliteNonceStore                    # noqa: E402
from primitive_runner import PrimitiveRunner                # noqa: E402

# host_protocol + host_agent_dispatch are the SINGLE SOURCE OF TRUTH in backend/hosted_workspace (Django-tested);
# the installer stages them into lib/hosted_workspace on the host, and under CI they resolve to the backend app.
from hosted_workspace.host_agent_dispatch import dispatch, reserved_ids_from   # noqa: E402
from hosted_workspace.host_protocol import HostProtocolError                    # noqa: E402

SERVICE_NAME = "guvfx-hosted-executor"
PROVISION_ROUTE = "/hosted/provision"
HEALTH_ROUTE = "/hosted/health"

logger = logging.getLogger("guvfx.hosted-executor")


# ── the pure request handler (framework-agnostic; unit-tested directly) ────────────────────────────────────
def build_dispatch_handler(cfg, *, nonce_store, runner, envelope_opener, clock=time.time, reserved_ids=None):
    """Return ``handle(req) -> (http_status, body_dict)``. Never raises: a signed success is 200 + the signed
    response; a validation/auth denial is 200 + a sanitised ``{"outcome":"denied","reason_code":...}`` (the
    backend re-verifies the signature and fails closed on the unsigned denial); an internal fault is 500."""
    base = reserved_ids if reserved_ids is not None else reserved_ids_from(cfg.get("reserved_account_ids"))
    # The daemon runs on the maximal-blast-radius host: the Customer Zero floor {1} is UNCONDITIONAL here,
    # regardless of config. The dispatch-level "explicit empty string opts out" affordance (tests only) must
    # never be reachable through the daemon - an empty/whitespace HOSTED_EXECUTOR_RESERVED_ACCOUNT_IDS can only
    # ADD to the floor, never remove it.
    reserved = frozenset(base) | frozenset({1})
    keyring = cfg["keyring"]
    max_skew = int(cfg.get("max_skew_seconds", 30))

    def handle(req):
        now = int(clock())
        nonce_store.purge_expired(now)      # bounded storage; nonces expire within the short validity window
        try:
            return 200, dispatch(
                req, keyring=keyring, now=now, nonce_burn=nonce_store.burn, run_primitive=runner.run,
                reserved_ids=reserved, envelope_open=envelope_opener, max_skew_seconds=max_skew)
        except HostProtocolError as exc:
            return 200, {"outcome": "denied", "reason_code": exc.reason_code}
        except Exception:  # noqa: BLE001 - never leak a traceback/secret; a fault is a sanitised 500
            logger.warning("hosted-executor: internal fault handling a provision request")
            return 500, {"outcome": "denied", "reason_code": "internal_error"}

    return handle


def make_handler(handle, *, max_body_bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            try:
                self.connection.settimeout(self.server.request_timeout_s)
            except OSError:
                pass
            # HARD wall-clock deadline on the request-READ phase. The per-recv socket timeout above is reset by
            # every byte received, so a slow-loris that dribbles one byte just under the timeout can hold a
            # connection permit + handler thread indefinitely and exhaust the (deliberately small) connection
            # cap pre-auth. This watchdog force-closes the socket after request_timeout_s regardless of recv
            # activity. It is DISARMED the moment the full request body has been read (`_disarm_deadline`), so
            # it bounds only the untrusted read phase - NEVER the legitimate (and possibly multi-minute)
            # provisioning dispatch that follows.
            self._deadline = None
            try:
                self._deadline = threading.Timer(self.server.request_timeout_s, self._force_close)
                self._deadline.daemon = True
                self._deadline.start()
            except RuntimeError:
                self._deadline = None

        def _force_close(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)   # unblocks a recv() stalled by a trickle client
            except OSError:
                pass

        def _disarm_deadline(self):
            t = getattr(self, "_deadline", None)
            if t is not None:
                t.cancel()
                self._deadline = None

        def finish(self):
            self._disarm_deadline()          # backstop for GET / early-return / error paths
            super().finish()

        def log_message(self, *a):          # never log request bodies / paths / nonces / secrets
            return

        def _send(self, obj, code=200):
            self.close_connection = True     # one request per connection (no keep-alive permit hoarding)
            body = json.dumps(obj).encode("utf-8")
            try:                             # the client may have hung up / been force-closed mid-response
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_POST(self):
            if self.path.rstrip("/") != PROVISION_ROUTE:
                return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            if length < 0:
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            if length > max_body_bytes:
                return self._send({"outcome": "denied", "reason_code": "request_too_large"}, 413)
            try:
                raw = self.rfile.read(length) or b"{}"
            except OSError:
                return
            finally:
                self._disarm_deadline()      # request fully read - the long dispatch must NOT be under the deadline
            try:
                req = json.loads(raw)
            except (ValueError, TypeError):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            if not isinstance(req, dict):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            code, body = handle(req)
            self._send(body, code)

        def do_GET(self):
            self._disarm_deadline()
            if self.path.rstrip("/") == HEALTH_ROUTE:    # non-secret liveness for supervision/monitoring
                return self._send({"service": SERVICE_NAME, "status": "ok"}, 200)
            return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)

    return Handler


# ── hardened threading server (exclusive single-instance bind + bounded connections) ───────────────────────
class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False          # do NOT set SO_REUSEADDR - it enables the Windows bind-hijack

    def __init__(self, addr, handler, *, request_timeout_s, max_connections):
        super().__init__(addr, handler)
        self.request_timeout_s = float(request_timeout_s)
        self._conn_sem = threading.BoundedSemaphore(max(1, int(max_connections)))

    def server_bind(self):
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)   # Windows only; the hard single-instance guard
        if excl is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
            except OSError:
                pass
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._conn_sem.acquire(blocking=False):
            self.shutdown_request(request)      # over the cap - refuse without spawning a handler thread
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self._conn_sem.release()            # Thread.start() failed - release the permit, don't leak it
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_sem.release()


def _make_logger(log_dir):
    lg = logging.getLogger("guvfx.hosted-executor")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if log_dir and not lg.handlers:
        try:
            os.makedirs(log_dir, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "hosted-executor.log"), maxBytes=1_000_000, backupCount=5,
                encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            lg.addHandler(h)
        except OSError:
            pass
    return lg


def build_components(cfg):
    """Assemble the daemon's injected pieces from config. ParseFile-gates every primitive (RULE 9) - a parse
    failure raises here so the service refuses to start."""
    nonce_store = SqliteNonceStore(cfg["state_db"])
    runner = PrimitiveRunner(scripts_dir=cfg["scripts_dir"], powershell=cfg["powershell"],
                             timeout_s=cfg["primitive_timeout_s"], max_output_bytes=cfg["max_output_bytes"])
    runner.verify_scripts()
    opener = make_envelope_opener(cfg["enc_privkeys_raw"])
    return nonce_store, runner, opener


class DaemonServer:
    """Testable lifecycle controller. Serves in a background thread; a stop drains in-flight work then closes;
    an abnormal serve-loop exit sets ``crashed`` so ``main`` exits non-zero and the supervisor restarts."""

    def __init__(self, cfg, *, nonce_store=None, runner=None, envelope_opener=None, clock=time.time):
        self.cfg = cfg
        if nonce_store is None or runner is None or envelope_opener is None:
            nonce_store, runner, envelope_opener = build_components(cfg)
        self._nonce = nonce_store
        self._log = _make_logger(cfg.get("log_dir"))
        self._drain_timeout_s = float(cfg.get("drain_timeout_s", 630))
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()      # initially drained (no in-flight work)
        self._handle = self._track_inflight(build_dispatch_handler(
            cfg, nonce_store=nonce_store, runner=runner, envelope_opener=envelope_opener, clock=clock))
        self._httpd = None
        self._thread = None
        self._stopping = False
        self._crashed = False

    def _track_inflight(self, inner):
        """Wrap the handler so ``stop`` can DRAIN in-flight provisioning ops: a ``sc stop`` must not kill a
        MATERIALISE/ACL mid-flight. ``shutdown()`` stops accepting new connections; this counter lets stop wait
        for the already-running handlers to finish (bounded by ``drain_timeout_s``)."""
        def tracked(req):
            with self._inflight_lock:
                self._inflight += 1
                self._drained.clear()
            try:
                return inner(req)
            finally:
                with self._inflight_lock:
                    self._inflight -= 1
                    if self._inflight == 0:
                        self._drained.set()
        return tracked

    @property
    def crashed(self):
        return self._crashed

    def make_server(self):
        daemon_config.assert_exact_bind(self.cfg["bind_host"], self.cfg["expected_bind_host"])
        httpd = BoundedThreadingHTTPServer(
            (self.cfg["bind_host"], self.cfg["bind_port"]),
            make_handler(self._handle, max_body_bytes=self.cfg["max_body_bytes"]),
            request_timeout_s=self.cfg["request_timeout_s"], max_connections=self.cfg["max_connections"])
        return httpd

    def start(self):
        if self._httpd is not None:
            return
        self._stopping = False
        self._crashed = False
        self._httpd = self.make_server()     # exclusive bind - a second listener on this port FAILS here
        self._thread = threading.Thread(target=self._serve_guarded, name="hosted-executor-http", daemon=True)
        self._thread.start()
        self._log.info("hosted-executor listening bind=%s:%s", self.cfg["bind_host"], self.cfg["bind_port"])

    def _serve_guarded(self):
        try:
            self._httpd.serve_forever()
        except BaseException:  # noqa: BLE001 - ANY serve-loop failure is a crash signal
            if not self._stopping:
                self._crashed = True
            return
        if not self._stopping:
            self._crashed = True     # serve_forever returned without a stop - abnormal

    def stop(self):
        self._stopping = True
        if self._httpd is not None:
            self._httpd.shutdown()                       # stop accepting new connections
            self._drained.wait(timeout=self._drain_timeout_s)   # DRAIN in-flight ops before closing the socket
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        try:
            self._nonce.close()
        except Exception:  # noqa: BLE001
            pass
        for h in list(self._log.handlers):
            try:
                h.close()
            finally:
                self._log.removeHandler(h)


def main():
    cfg = daemon_config.load_config()        # raises unless bind host is the EXACT expected private address
    server = DaemonServer(cfg)
    server.start()
    try:
        while server._thread is not None and server._thread.is_alive():
            server._thread.join(timeout=1)
    except KeyboardInterrupt:
        server.stop()
        return
    if server.crashed:                       # abnormal exit -> non-zero so WinSW onfailure=restart fires
        sys.exit(1)


if __name__ == "__main__":
    main()
