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


class PoolExhausted(AgentError):
    """No free slot remains in the pre-provisioned pool."""

    def __init__(self):
        super().__init__("pool_exhausted")


class SlotStore:
    """Durable runtime-UUID → pool-slot assignment (B3P-2, per-slot execution model).

    ARCHITECTURAL INTENT (Rule 5 — do not collapse these into one statement):

    * **Intent.** Each concurrently-running beta runtime executes under its OWN dedicated non-admin Windows
      identity, in its OWN fixed directory, launched and terminated by its OWN fixed scheduled tasks. That
      is the isolation boundary.
    * **Implementation.** The pool (identities + `TASK_LOGON_PASSWORD` launch/terminate tasks + per-slot
      directories + ACLs) is **pre-provisioned once by a human administrator at install**. This store only
      records which runtime currently occupies which pre-existing slot. It creates **no** OS object, no
      user, and holds **no** credential — the agent is non-admin and must never be able to mint identities.
    * **Compatibility.** A slot identity is reused after a runtime is torn down and its directory
      tombstoned. Concurrent tenants therefore never share an identity; sequential ones may reuse a slot.
      This matches the estate's existing ``guvfx_u_{1,6,7}`` per-slot pattern.

    The fixed per-slot directory is what makes the launch target non-steerable: each slot's task hard-codes
    its own path, so no caller-supplied value ever reaches a task action.
    """

    GENERATION_START = 1

    def __init__(self, path: str, pool_size: int):
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self.pool_size = int(pool_size)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        # One durable row PER SLOT. ``runtime_uuid`` NULL = free (SQLite treats NULLs as distinct, so the
        # UNIQUE constraint still forbids two runtimes holding the same slot). ``generation`` is a per-slot
        # monotonic counter that survives release, so (slot, generation) uniquely identifies one OCCUPANCY
        # for the life of the pool — that is what disambiguates a historical occupant from the current one.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS slots ("
            " slot INTEGER PRIMARY KEY,"
            " runtime_uuid TEXT UNIQUE,"
            " generation INTEGER NOT NULL,"
            " assigned_at INTEGER)")
        self._conn.commit()

    def lookup(self, runtime_uuid: str):
        """``(slot, generation)`` currently assigned to this runtime, or ``None``. NEVER allocates."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT slot, generation FROM slots WHERE runtime_uuid=?", (str(runtime_uuid),))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def assign(self, runtime_uuid: str, now: int):
        """Idempotently assign the lowest free slot; returns ``(slot, generation)``.

        Raises :class:`PoolExhausted` when the pool is full. Only MATERIALISE may allocate; every other
        operation resolves via :meth:`lookup`, so a stray request cannot consume a slot.
        """
        uid = str(runtime_uuid)
        with self._lock:
            cur = self._conn.execute(
                "SELECT slot, generation FROM slots WHERE runtime_uuid=?", (uid,))
            row = cur.fetchone()
            if row:
                return (row[0], row[1])            # idempotent: already assigned, same generation
            existing = {r[0]: r[1] for r in self._conn.execute("SELECT slot, runtime_uuid FROM slots")}
            for slot in range(1, self.pool_size + 1):
                if existing.get(slot) is not None:
                    continue                       # occupied
                if slot in existing:               # free row exists -> reuse it, keep its generation
                    gen = self._conn.execute(
                        "SELECT generation FROM slots WHERE slot=?", (slot,)).fetchone()[0]
                    self._conn.execute(
                        "UPDATE slots SET runtime_uuid=?, assigned_at=? WHERE slot=?", (uid, int(now), slot))
                else:                              # first ever use of this slot
                    gen = self.GENERATION_START
                    self._conn.execute(
                        "INSERT INTO slots (slot, runtime_uuid, generation, assigned_at) VALUES (?,?,?,?)",
                        (slot, uid, gen, int(now)))
                self._conn.commit()
                return (slot, gen)
            raise PoolExhausted()

    def release(self, runtime_uuid: str) -> bool:
        """Free the runtime's slot after TOMBSTONE and **increment that slot's generation**. Idempotent.

        The increment is what makes a reused slot unambiguous: the next occupant gets a strictly greater
        generation, so any stale marker, report or audit row from the previous occupancy can never be
        mistaken for the current one — it will fail the integrity check instead.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT slot FROM slots WHERE runtime_uuid=?", (str(runtime_uuid),)).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE slots SET runtime_uuid=NULL, assigned_at=NULL, generation=generation+1 "
                "WHERE slot=?", (row[0],))
            self._conn.commit()
            return True

    def generation_of(self, slot: int):
        with self._lock:
            row = self._conn.execute("SELECT generation FROM slots WHERE slot=?", (int(slot),)).fetchone()
        return row[0] if row else None

    def occupancy(self) -> dict:
        """``{slot: (runtime_uuid, generation)}`` for OCCUPIED slots only."""
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT slot, runtime_uuid, generation FROM slots "
                "WHERE runtime_uuid IS NOT NULL ORDER BY slot"))
        return {slot: (uid, gen) for slot, uid, gen in rows}


def slot_runtime_dir(slots_root: str, slot: int) -> str:
    """The FIXED per-slot runtime directory. Derived only from the slot number — never from any
    caller-supplied value — which is what keeps each slot's launch task non-steerable."""
    return rf"{slots_root}\{int(slot)}\terminal"


class SlotIntegrityError(AgentError):
    """The slot database, the on-disk ownership marker, the runtime UUID and the generation disagree."""

    def __init__(self):
        super().__init__("slot_integrity_mismatch")


def format_owner_marker(runtime_uuid: str, slot: int, generation: int) -> str:
    """Serialise the on-disk slot ownership marker. Carries the GENERATION so a marker left by a previous
    occupant of the same slot is detectably stale rather than silently accepted."""
    return json.dumps(
        {"runtime_uuid": str(runtime_uuid), "slot": int(slot), "generation": int(generation)},
        sort_keys=True)


def parse_owner_marker(raw):
    """Parse a marker written by :func:`format_owner_marker`. Returns None if absent or unparseable —
    callers treat that as 'no verified owner', never as 'free'."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {"runtime_uuid": str(data["runtime_uuid"]),
                "slot": int(data["slot"]),
                "generation": int(data["generation"])}
    except (ValueError, KeyError, TypeError):
        return None


def assert_slot_integrity(*, runtime_uuid: str, slot: int, generation: int, marker_raw) -> None:
    """FAIL-CLOSED four-way agreement, required before ANY mutating operation.

    All four must agree or the operation is refused with a sanitised ``slot_integrity_mismatch``:

    1. the slot assignment database (``slot`` / ``generation`` passed in by the caller after lookup),
    2. the on-disk slot ownership marker (``marker_raw``),
    3. the runtime UUID of the request,
    4. the slot generation.

    An absent or corrupt marker is a mismatch, never an implicit "free" — that is the distinction that
    stops a stale directory from a previous occupancy being adopted by the current one.
    """
    marker = parse_owner_marker(marker_raw)
    if marker is None:
        raise SlotIntegrityError()
    if (marker["runtime_uuid"] != str(runtime_uuid)
            or marker["slot"] != int(slot)
            or marker["generation"] != int(generation)):
        raise SlotIntegrityError()


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
        self._draining = False                 # once set, no NEW mutation may begin

    def begin_drain(self) -> None:
        """Refuse any mutation that has not yet committed (verification B-6): a request arriving during
        shutdown is denied (``agent_stopping``) rather than started and then killed. Ops already counted in
        ``_active`` are unaffected — the stop path waits them out."""
        with self._guard:
            self._draining = True

    @contextlib.contextmanager
    def acquire(self, runtime_uuid: str):
        if not self._global.acquire(blocking=False):
            raise AgentError("agent_busy")
        try:
            # Drain-check + per-runtime lock + active-count increment are ONE critical section, so once
            # ``begin_drain`` is set no op can slip past the check and then increment (closes the race where a
            # just-starting op is momentarily invisible to the drain counter).
            with self._guard:
                if self._draining:
                    raise AgentError("agent_stopping")
                lk = self._per_runtime.setdefault(str(runtime_uuid), threading.Lock())
                if not lk.acquire(blocking=False):          # non-blocking, safe under the guard
                    raise AgentError("runtime_busy")
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
