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

Run (production): ONLY under the ``GuvFXBetaAgent`` WinSW service (ADR-0013 — venv python -> agent.py, with
SCM start/stop, drain, recovery and rolling logs). A bare ``python agent.py`` is for OFFLINE/dev only and must
NEVER be used to run the production listener: an ad-hoc/interactive process is session-bound, has no
supervision/restart/logging, and dies when its launcher ends (security RULE 1) — this is exactly the
2026-08-05 dark-:8791 incident. The production lifecycle (supervised restart, health probe, lifecycle logging)
is defined in ``docs/VALIDATION_AGENT_PRODUCTION_HARDENING.md``. (``service.py`` is a pywin32 wrapper kept for
reference; ADR-0013 selected WinSW as the service host, not pywin32.)
"""
import itertools
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
sys.path.insert(0, _HERE)   # so the bundled ``lib`` package + agent modules import cleanly

from lib.mgmt_agent_core import BetaProvisioningAgent   # noqa: E402  bundled Django-free agent core
import config as agent_config                            # noqa: E402
import manifest as agent_manifest                        # noqa: E402
from lib.mgmt_agent_core import (EXECUTION_MODEL_SLOT_POOL,  # noqa: E402
                                 EXECUTION_MODEL_UUID_DIR)
from op_impls import OpImplementations                   # noqa: E402
from pool_op_impls import PoolOpImplementations, SlotResolver   # noqa: E402
from stores import RuntimeLockManager, SlotStore, SqliteStore   # noqa: E402
from win_ops import RealWindowsOps                        # noqa: E402
from win_slot_ops import RealSlotWindowsOps               # noqa: E402
import agent_lifecycle                                    # noqa: E402  WS-C/WS-D min-hardening primitives

AGENT_VERSION = "beta-agent-1.0.0"


def _pid_alive(pid: int) -> bool:
    """Read-only liveness probe for the single-instance guard, STDLIB-ONLY (no ctypes/win32 — this module is
    NOT a Windows adapter; the primitive-layer boundary invariant forbids Windows API here). Positively alive
    => True; positively dead OR INDETERMINATE => False, so an indeterminate lock is RECLAIMED. That is safe
    because the OS socket bind is the HARD single-instance enforcement (two live listeners on the exact
    host:port cannot coexist; the guard runs BEFORE bind, so a real conflict still fails at bind); this lock
    only adds durable identity + clean detection. On Windows ``os.kill`` cannot probe liveness with signal 0
    (it maps to a console-event / TerminateProcess), so we do NOT call it there — Windows liveness defers to
    the bind backstop by design (documented in the deployment package + ADR-0013 addendum)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # exists but owned by another user
        except OSError:
            return False
    # non-posix (Windows): indeterminate => reclaim; the OS bind is the authoritative single-instance guard.
    return False


def _os_open_excl(path: str) -> bool:
    """Atomic exclusive create (O_CREAT|O_EXCL). True iff THIS call created the file; False if it already
    exists. A missing parent dir raises (the caller makes the dir best-effort first)."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def build_agent(cfg: dict, *, win=None, store=None, locks=None, manifest_path: str = "",
                enforce_integrity: bool = False, slot_store_override=None,
                agent_supervised=None) -> BetaProvisioningAgent:
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

    store = store if store is not None else SqliteStore(cfg["state_db"])
    locks = locks if locks is not None else RuntimeLockManager()
    slot_pool = cfg.get("execution_model") == EXECUTION_MODEL_SLOT_POOL

    # The two execution models get DIFFERENT adapters and DIFFERENT implementations. They are never mixed:
    # the B2 uuid-directory model has no slot store, and the pool model never constructs the legacy ops.
    if slot_pool:
        win = win if win is not None else RealSlotWindowsOps(golden_dir=cfg["golden_dir"],
                                                             slots_root=cfg["slots_root"])
        slot_store = slot_store_override or SlotStore(cfg["slot_db"], pool_size=cfg["slot_pool_size"])
        # now_fn is load-bearing, not decoration: omitted, PoolOpImplementations falls back to
        # ``lambda: 0`` and every durable stage record, audit row and release timestamp is written as 0,
        # making the evidence chain unorderable in time.
        impls = PoolOpImplementations(
            win, slot_store, golden_digest=cfg["golden_digest"],
            golden_manifest_version=cfg["golden_manifest_version"],
            approved_tasks=cfg.get("approved_tasks") or {},
            now_fn=lambda: int(time.time()),
            manifest_version=approved.get("manifest_version", "")).as_dict()
        resolver = SlotResolver(slot_store, slots_root=cfg["slots_root"], now_fn=lambda: int(time.time()))
        base = cfg["slots_root"]
    else:
        win = win if win is not None else RealWindowsOps()
        impls = OpImplementations(win, tombstone_base=cfg["tombstone_base"]).as_dict()
        resolver, base = None, cfg["beta_root"]

    return BetaProvisioningAgent(
        keyring=cfg["keyring"], nonce_store=store, idempotency_store=store, op_impls=impls,
        agent_version=AGENT_VERSION,
        script_manifest=script_manifest,
        script_versions={f"op_{op.lower()}": approved.get("manifest_version", "")
                         for op in approved.get("supported_operations", [])},
        resolve_real_path=win.real_path, runtime_locks=locks,
        base=base, manifest_version=approved.get("manifest_version", ""),
        execution_model=cfg.get("execution_model") or EXECUTION_MODEL_UUID_DIR,
        slot_resolver=resolver, login_validator=_build_login_validator(cfg, win),
        agent_supervised=agent_supervised)


def _build_login_validator(cfg: dict, win=None):
    """ADR-0027: return the login validator the agent CORE injects.

    Task-launch remediation (root cause 2026-08-02: MT5 GUI/MDI creation fails when the terminal is launched
    IN-PROCESS by the WinSW service, and succeeds via a scheduled task). When a validation TASK NAME is
    configured, return a ``TaskLaunchLoginValidator`` that DELEGATES the probe to the GUI-capable
    task-launched runner via the secure single-use handoff (no secret ever reaches the task command/args/env).
    The task is triggered ONLY through the Windows adapter (``win.run_task`` — agent.py itself touches no
    Windows API / subprocess). Without a task name, fall back to the legacy IN-PROCESS handler (retained for
    tests / non-Windows).

    Either way returns ``None`` when the validation terminal / envelope private key is unconfigured, so
    VALIDATE_LOGIN fails closed (``validation_unconfigured``)."""
    if not cfg.get("validation_terminal_dir"):
        return None
    from validate_login import TaskLaunchLoginValidator, build_inprocess_handler
    task_name = cfg.get("validation_task_name")
    if task_name:
        handoff_dir = cfg.get("validation_handoff_dir")
        if not (handoff_dir and win is not None):        # need the adapter to trigger the task → fail closed
            return None
        return TaskLaunchLoginValidator(
            handoff_dir=handoff_dir, task_name=task_name, trigger_task=win.run_task,
            timeout_ms=int(cfg.get("login_timeout_ms", 120000)),
            # ADR-0027 Phase 2: the result wait = login_timeout_ms/1000 + result_grace_s. Feed the canonical
            # cleanup grace (config-owned, contract-validated) so the Agent NEVER pre-empts a completing runner.
            result_grace_s=int(cfg.get("cleanup_grace_s", 45)))
    return build_inprocess_handler(cfg)              # legacy in-process (GUI-incapable under the service)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server that BOUNDS concurrent connections (refuses, never queues, past the cap) and
    carries the per-request body/timeout limits. Prevents an unauthenticated flood from spawning unbounded
    threads on the shared live host (verification: pre-auth resource exhaustion).

    Single-instance: ``allow_reuse_address`` is FORCED False (the base ``HTTPServer`` defaults it True → the
    socket would carry ``SO_REUSEADDR``, which on WINDOWS lets a second process bind the EXACT same
    host:port and hijack delivery — an ad-hoc ``python agent.py`` could silently steal :8791 from the
    supervised service). With it off, and ``SO_EXCLUSIVEADDRUSE`` set on Windows, a second bind to :8791
    FAILS at the OS — making the exclusive bind the genuine hard single-instance guard the lock only
    advises (adversarial review, 2026-08-06). :8788 is never touched here."""
    daemon_threads = True
    allow_reuse_address = False        # do NOT set SO_REUSEADDR — it enables the Windows bind-hijack

    def server_bind(self):
        # On Windows, additionally request EXCLUSIVE ownership so even a SO_REUSEADDR peer cannot steal the
        # port. getattr-guarded: SO_EXCLUSIVEADDRUSE exists only on Windows Python; a no-op elsewhere. This
        # is stdlib ``socket`` only (no ctypes/win32 — the primitive-layer boundary is preserved).
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
            except OSError:
                pass
        super().server_bind()

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
        except RuntimeError:
            # Thread.start() failed ("can't start new thread" under load) — the ONLY exception source here, and
            # it means the worker never started, so it will NOT run process_request_thread's release. Release
            # HERE to avoid leaking the permit (which would otherwise wedge the gate closed after
            # ``max_connections`` such failures). Narrow to RuntimeError so an interrupt arriving AFTER the
            # worker started cannot double-release the BoundedSemaphore.
            self._conn_sem.release()
            raise

    def process_request_thread(self, request, client_address):
        # Each request runs on a FRESH worker thread. COM must be initialised PER THREAD or the COM callers in
        # the request path raise CO_E_NOTINITIALIZED — the WMI ``Win32_Process`` session query in observe
        # (ADR-0015) AND the ``Schedule.Service`` task primitives (STOP/TOMBSTONE triggers). Initialise the
        # MTA once at the thread boundary, balanced on exit. Off-host (pywin32 absent) this is a no-op.
        com_inited = False
        try:
            import pythoncom
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            com_inited = True
        except Exception:                    # noqa: BLE001 — off-host, or apartment already established
            pass
        try:
            super().process_request_thread(request, client_address)
        finally:
            if com_inited:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:            # noqa: BLE001
                    pass
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


_LOGGER_SEQ = itertools.count()


def _make_logger(log_dir: str | None) -> logging.Logger:
    """Per-instance lifecycle logger (start/stop/drain-timeout only — NEVER request bodies/paths/nonces/
    secrets) writing a rotating file under ``log_dir`` so the state/log relocation is realised, not just
    declared. A UNIQUE name per instance + ``propagate=False`` keeps the agent's log out of the root logger
    and stops handlers accumulating / bleeding across instances on a shared singleton; ``AgentServer.stop``
    closes+removes the handler."""
    logger = logging.getLogger(f"beta-agent.{next(_LOGGER_SEQ)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "agent.log"), maxBytes=1_000_000,
                                                     backupCount=3, encoding="utf-8")
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

    def __init__(self, cfg: dict, *, win=None, store=None, locks=None, enforce_integrity: bool = True,
                 env=None, now_fn=None):
        self.cfg = cfg
        self._locks = locks if locks is not None else RuntimeLockManager()
        # WS-D launch classification — decided ONCE, from the environment the process was started in. A bare
        # ``python agent.py`` (the Aug-5 vector) is NOT supervised, so it advertises ``agent_supervised=false``
        # in NEGOTIATE and the backend health probe treats it as not-HEALTHY (never mistaken for the service).
        self._env = os.environ if env is None else env
        self._now = now_fn or time.time
        self._launch = agent_lifecycle.classify_launch(self._env)
        self.supervised = bool(self._launch["supervised"])
        # Hard refuse-to-bind switch (default OFF). cfg wins (tests); else read the env directly (no config.py
        # change needed) so the WinSW supervised profile can flip it on ONLY after the service is in place.
        cfg_refuse = self.cfg.get("refuse_unsupervised_launch")
        self._refuse_unsupervised = bool(cfg_refuse) if cfg_refuse is not None else \
            str(self._env.get("BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH", "")).strip().lower() in \
            ("1", "true", "yes", "on")
        self._agent = build_agent(cfg, win=win, store=store, locks=self._locks,
                                  enforce_integrity=enforce_integrity, agent_supervised=self.supervised)
        self._log = _make_logger(cfg.get("log_dir"))
        self._httpd = None
        self._thread = None
        self._started_at = None
        self._stopping = False          # set True by stop(): a serve-thread exit while stopping is EXPECTED
        self._crashed = False           # set True by the serve guard on an ABNORMAL serve-thread exit
        self._lock_path = self._resolve_lifecycle_path("instance_lock_path", "agent_instance.lock", port=True)
        self._lifecycle_log = self._resolve_lifecycle_path("lifecycle_log_path", "agent_lifecycle.jsonl")

    def _resolve_lifecycle_path(self, cfg_key: str, default_name: str, *, port: bool = False) -> str | None:
        """Resolve a durable lifecycle path from config, else derive it next to the state db / log dir. The
        lock name carries the bind PORT so two DIFFERENT sanctioned ports never share one lock."""
        explicit = self.cfg.get(cfg_key)
        if explicit:
            return explicit
        base = self.cfg.get("log_dir") or os.path.dirname(self.cfg.get("state_db") or "") or None
        if not base:
            return None
        if port:
            default_name = f"agent_instance_{self.cfg.get('bind_port', 'x')}.lock"
        return os.path.join(base, default_name)

    def _emit(self, event: str, **fields) -> None:
        """Emit ONE durable, secret-safe lifecycle event (independent of the WinSW wrapper log — RR-3). Never
        raises; always stamps the common identity fields so a start/exit is never invisible again."""
        base = {
            "agent_version": AGENT_VERSION, "manifest_version": self._agent.manifest_version,
            "pid": os.getpid(), "parent_pid": (os.getppid() if hasattr(os, "getppid") else None),
            "supervised": self.supervised, "startup_reason": self._launch.get("startup_reason"),
            "service_identity": self._launch.get("service_identity"),
            "bind_host": self.cfg.get("bind_host"), "bind_port": self.cfg.get("bind_port"),
        }
        base.update({k: v for k, v in fields.items() if v is not None})
        ev = agent_lifecycle.build_event(event, now=self._now(), fields=base)
        if self._lifecycle_log:
            agent_lifecycle.append_event(self._lifecycle_log, ev)
        self._log.info("lifecycle %s", event)

    def _acquire_instance(self) -> None:
        """Acquire the single-instance lock (defence-in-depth). The OS bind is the HARD single-instance
        enforcement (two active listeners on the exact host:port cannot coexist); this lock adds a durable,
        inspectable owner record and a clean, logged detection. Raises InstanceGuardError only on a positively
        LIVE conflicting holder; indeterminate/dead locks reclaim (the bind is the backstop)."""
        if not self._lock_path:
            return
        try:
            os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
        except OSError:
            return              # cannot store the lock — rely on the OS bind; do not block startup
        agent_lifecycle.acquire_single_instance(
            self._lock_path, pid=os.getpid(), now=self._now(), pid_alive=_pid_alive, open_excl=_os_open_excl)

    def _release_instance(self) -> None:
        if not self._lock_path:
            return
        try:
            os.remove(self._lock_path)
        except OSError:
            pass

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
        self._emit("AGENT_STARTING")
        # WS-D launch enforcement. ALWAYS-ON: an unsupervised launch already advertises supervised=false
        # (backend => not HEALTHY). The HARD refuse-to-bind is a documented, config-gated switch
        # (``refuse_unsupervised_launch``, default OFF) so it can be enabled ONLY after the supervised WinSW
        # service is in place — flipping it on before then would brick the manual recovery path (RULE-1 care).
        if self._refuse_unsupervised and not agent_lifecycle.launch_permitted(self._launch):
            self._emit("AGENT_LAUNCH_REJECTED", result="refused_unsupervised",
                       shutdown_reason="unsanctioned_launch")
            raise RuntimeError("refusing to bind: unsupervised launch and no operator override "
                               "(start via the GuvFXBetaAgent service)")
        # Single-instance: the lock is ADVISORY (durable identity + clean detection), NOT the arbiter. A
        # detected conflict is LOGGED and we PROCEED — the EXCLUSIVE OS bind below is the hard guard: a real
        # duplicate fails at bind (loud), while a stale/reused-pid lock must never veto a start on a free port
        # (adversarial review 2026-08-06: raising here turned one crash into a persistent dark-:8791 outage).
        try:
            self._acquire_instance()
        except agent_lifecycle.InstanceGuardError as exc:
            self._emit("AGENT_DEGRADED", result="instance_lock_conflict",
                       detail=f"advisory single-instance lock conflict; letting the exclusive bind arbitrate: {exc}")
        self._stopping = False
        self._crashed = False
        try:
            self._httpd = self.make_server()      # exclusive bind — a second listener on :8791 FAILS here
        except OSError as exc:
            # A real duplicate (or a taken port) fails the exclusive bind LOUDLY. Record it durably before it
            # propagates (main() has no handler → non-zero exit → WinSW restarts), so the crash is not invisible.
            self._emit("AGENT_LAUNCH_REJECTED", result="bind_failed",
                       shutdown_reason="address_in_use", detail=type(exc).__name__)
            raise
        self._started_at = self._now()
        self._thread = threading.Thread(target=self._serve_guarded, name="beta-agent-http", daemon=True)
        self._thread.start()
        self._log.info("agent started bind=%s:%s", self.cfg["bind_host"], self.cfg["bind_port"])
        self._emit("AGENT_LISTENING")
        # NEGOTIATE-ready the instant the socket is up: the agent has no separate warm-up (VALIDATE_LOGIN
        # readiness is a downstream/keyring concern the backend probe distinguishes, not an agent state here).
        self._emit("AGENT_READY", result="ok" if self.supervised else "ok_unsupervised")

    def _serve_guarded(self) -> None:
        """Run serve_forever with a crash guard. serve_forever returns cleanly ONLY when ``stop()`` calls
        ``shutdown()`` (``_stopping`` is then True). Any other exit — an exception, or serve_forever returning
        while NOT stopping — is an ABNORMAL death: record it as AGENT_CRASHED so a crash is never invisible,
        and ``main()`` can exit non-zero so the supervisor restarts (WinSW ``onfailure=restart`` fires only on
        a non-zero exit; a silent exit 0 would leave :8791 dark, the Aug-5 failure class)."""
        try:
            self._httpd.serve_forever()
        except BaseException as exc:                 # noqa: BLE001 — ANY serve-loop failure is a crash signal
            self._note_crash(f"serve_forever raised: {type(exc).__name__}")
            return
        if not self._stopping:
            self._note_crash("serve_forever returned without a stop")

    def _note_crash(self, detail: str) -> None:
        if self._stopping or self._crashed:
            return
        self._crashed = True
        uptime = None if self._started_at is None else round(self._now() - self._started_at, 3)
        self._emit("AGENT_CRASHED", result="abnormal_exit", exit_classification="crash",
                   uptime_s=uptime, detail=detail)

    @property
    def crashed(self) -> bool:
        return self._crashed

    def stop(self, drain_timeout_s: float | None = None) -> bool:
        """Stop accepting new work, drain in-flight mutating ops (bounded), then shut down. Returns True if
        fully drained, False if the drain timed out (shutdown still proceeds; the SCM logs the forced stop).

        Order matters (verification B-6): begin_drain() refuses any mutation that has not yet committed, and
        shutdown() exits the accept loop, BEFORE we wait — so no new mutation can start during the drain
        window and then be killed at teardown."""
        self._stopping = True                         # a serve-thread exit from here on is EXPECTED, not a crash
        self._emit("AGENT_STOPPING")
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
        self._release_instance()
        uptime = None if self._started_at is None else round(self._now() - self._started_at, 3)
        self._emit("AGENT_STOPPED", result="drained" if drained else "drain_timeout",
                   exit_classification="operator_stop", uptime_s=uptime)
        self._started_at = None
        (self._log.warning if not drained else self._log.info)(
            "agent stopped%s", "" if drained else " (drain timed out — forced)")
        for h in list(self._log.handlers):          # release the rotating-file handle; do not leak across restarts
            try:
                h.close()
            finally:
                self._log.removeHandler(h)
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
        return
    # The serve thread ended without a KeyboardInterrupt. If it CRASHED (abnormal exit), exit NON-ZERO so the
    # supervisor (WinSW onfailure=restart) restarts it — a silent exit 0 here is the Aug-5 dark-:8791 failure.
    if server.crashed:
        sys.exit(1)


if __name__ == "__main__":
    main()
