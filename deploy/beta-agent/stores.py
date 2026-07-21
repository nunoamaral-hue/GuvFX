"""CVM-Inc-3 B2 — durable replay + idempotency stores and conservative concurrency for the agent.

Replay protection and completed-operation evidence must survive an agent RESTART (requirement 7): both
live in a SQLite file, not memory. Concurrency (requirement 8): at most one mutating op per runtime and a
conservative global mutation limit; a conflicting op returns a sanitised BUSY reason rather than queueing.
"""
import contextlib
import json
import sqlite3
import threading

from lib.mgmt_agent_core import AgentError   # bundled lib


class SqliteStore:
    """One SQLite file holding the nonce (replay) table + the (job_id, op) idempotency table."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, expiry INTEGER)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS idem (job_id INTEGER, op TEXT, digest TEXT, response TEXT, "
            "PRIMARY KEY (job_id, op))")
        self._conn.commit()

    # ── nonce store (replay) ──
    def burn(self, nonce: str, expiry: int) -> bool:
        """ATOMIC single-use: insert the nonce and report whether THIS call was the first to do so.
        The INSERT-OR-IGNORE + rowcount check under the connection lock makes seen+remember one step, so
        two concurrent identical requests cannot both be accepted (S3)."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO nonces (nonce, expiry) VALUES (?, ?)",
                                     (nonce, int(expiry)))
            self._conn.commit()
            return cur.rowcount == 1

    def seen(self, nonce: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM nonces WHERE nonce=?", (nonce,))
            return cur.fetchone() is not None

    def purge_expired_nonces(self, now: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nonces WHERE expiry < ?", (int(now),))
            self._conn.commit()

    # ── idempotency store ──
    def get(self, job_id: int, op: str):
        with self._lock:
            cur = self._conn.execute("SELECT digest, response FROM idem WHERE job_id=? AND op=?",
                                     (int(job_id), op))
            row = cur.fetchone()
        if row is None:
            return None
        return {"digest": row[0], "response": json.loads(row[1])}

    def put(self, job_id: int, op: str, record: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO idem (job_id, op, digest, response) VALUES (?, ?, ?, ?)",
                (int(job_id), op, record["digest"], json.dumps(record["response"])))
            self._conn.commit()


class RuntimeLockManager:
    """In-process, NON-blocking mutation gate: one mutating op per runtime + a global cap. A conflict
    raises ``AgentError`` with a sanitised BUSY reason (the agent-core turns it into an ``outcome=denied``)
    — conflicting operations never queue unpredictably.

    B3P-1 drain support (verification B-6): the manager tracks the count of mutating ops currently holding a
    lock, so the service stop handler can WAIT for in-flight mutations to finish before shutting down rather
    than killing one mid-flight. (This is the in-process safety half; the durable crash-recovery marker is a
    B3P-2 deliverable alongside the box-side MATERIALISE ops.)"""

    def __init__(self, max_global_mutations: int = 2):
        self._global = threading.Semaphore(max_global_mutations)
        self._guard = threading.Lock()
        self._per_runtime: dict[str, threading.Lock] = {}
        self._active = 0                       # mutating ops currently holding a lock

    @contextlib.contextmanager
    def acquire(self, runtime_uuid: str):
        if not self._global.acquire(blocking=False):
            raise AgentError("agent_busy")
        try:
            with self._guard:
                lk = self._per_runtime.setdefault(str(runtime_uuid), threading.Lock())
            if not lk.acquire(blocking=False):
                raise AgentError("runtime_busy")
            with self._guard:
                self._active += 1
            try:
                yield
            finally:
                with self._guard:
                    self._active -= 1
                lk.release()
        finally:
            self._global.release()

    def active_mutations(self) -> int:
        """Number of mutating ops currently executing under a runtime lock."""
        with self._guard:
            return self._active
