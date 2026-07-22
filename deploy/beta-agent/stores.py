"""CVM-Inc-3 B2 — durable replay + idempotency stores and conservative concurrency for the agent.

Replay protection and completed-operation evidence must survive an agent RESTART (requirement 7): both
live in a SQLite file, not memory. Concurrency (requirement 8): at most one mutating op per runtime and a
conservative global mutation limit; a conflicting op returns a sanitised BUSY reason rather than queueing.
"""
import contextlib
import hashlib
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


def _chain_hash(previous_hash: str, material: dict) -> str:
    """Forward link: SHA-256 over the previous link plus a canonical serialisation of this record."""
    body = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((str(previous_hash) + "|" + body).encode("utf-8")).hexdigest()


def occupancy_id(slot, generation) -> str:
    """Immutable identifier for ONE occupancy of one execution slot.

    ``(slot, generation)`` is already unique; this gives every downstream component a single stable
    reference instead of re-concatenating the pair. Deterministic and reproducible from the pair alone, so
    an investigator can always recompute it. Carried by the Verification Report, the slot audit,
    ProvisioningJob evidence, operator evidence and quarantine records.
    """
    return hashlib.sha256(f"slot={int(slot)}|generation={int(generation)}".encode("utf-8")).hexdigest()[:16]


class AuditChainCorrupt(AgentError):
    """The forward-linked audit chain does not verify: a record was deleted, inserted or reordered."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("audit_chain_corrupt")


class AllocationBlocked(AgentError):
    """No slot may be handed out: audit corruption could not be attributed to a single slot."""

    def __init__(self, reason=""):
        self.reason = reason
        super().__init__("allocation_blocked")


class ReleaseProofMissing(AgentError):
    """A slot release was attempted before every required proof was durably true."""

    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__("release_proof_missing")


class QuarantineClearanceRefused(AgentError):
    """Operator quarantine clearance did not meet the required evidence bar."""

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__("quarantine_clearance_refused")


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
    #: First link of the forward-linked audit chain.
    AUDIT_CHAIN_GENESIS = "genesis"

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
        # Append-only ledger of every generation value a slot has ever held. Monotonicity is asserted
        # against this, so a tampered/rolled-back ``slots.generation`` is detectable rather than trusted.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS slot_generations ("
            " slot INTEGER NOT NULL, generation INTEGER NOT NULL, event TEXT NOT NULL,"
            " at INTEGER NOT NULL, PRIMARY KEY (slot, generation))")
        # A slot that failed an integrity assertion is quarantined and NEVER silently repaired or reused.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS slot_quarantine ("
            " slot INTEGER PRIMARY KEY, reason TEXT NOT NULL, at INTEGER NOT NULL,"
            " occupancy_id TEXT, generation INTEGER)")
        # Append-only audit of material lifecycle events. Every row carries occupancy identity.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS slot_audit ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, runtime_uuid TEXT NOT NULL,"
            " slot INTEGER NOT NULL, generation INTEGER NOT NULL, occupancy_id TEXT NOT NULL,"
            " operation TEXT,"
            " provisioning_job_id INTEGER, correlation_id TEXT, prior_state TEXT, resulting_state TEXT,"
            " integrity_outcome TEXT, quarantined INTEGER NOT NULL DEFAULT 0, agent_version TEXT,"
            " manifest_version TEXT, protocol_version INTEGER, at INTEGER NOT NULL,"
            " previous_audit_hash TEXT NOT NULL, audit_hash TEXT NOT NULL)")
        # Per-occupancy operation sequence. PRIMARY KEY (slot, generation, sequence_number) makes a
        # DUPLICATE sequence number structurally impossible; contiguity from 1 is asserted separately, so a
        # GAP (a missing operation record) is an integrity failure rather than a silent hole.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS occupancy_sequence ("
            " slot INTEGER NOT NULL, generation INTEGER NOT NULL, sequence_number INTEGER NOT NULL,"
            " operation TEXT NOT NULL, at INTEGER NOT NULL,"
            " PRIMARY KEY (slot, generation, sequence_number))")
        # Global allocation block: set when audit corruption cannot be attributed to a single slot.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS allocation_block ("
            " id INTEGER PRIMARY KEY CHECK (id = 1), reason TEXT NOT NULL, at INTEGER NOT NULL)")
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
        blocked = self.allocation_blocked()
        if blocked:
            raise AllocationBlocked(blocked)
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
                    self._conn.execute(
                        "INSERT OR IGNORE INTO slot_generations (slot, generation, event, at) "
                        "VALUES (?,?,?,?)", (slot, gen, "init", int(now)))
                self._conn.commit()
                return (slot, gen)
            raise PoolExhausted()

    def release(self, runtime_uuid: str, now: int = 0) -> bool:
        """Free the runtime's slot after TOMBSTONE and **increment that slot's generation by exactly 1**.

        Idempotent. The increment is what makes a reused slot unambiguous: the next occupant gets a
        strictly greater generation, so any stale marker, report or audit row from the previous occupancy
        can never be mistaken for the current one — it fails the integrity check instead. Every transition
        is appended to ``slot_generations`` so monotonicity is provable, not assumed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT slot, generation FROM slots WHERE runtime_uuid=?",
                (str(runtime_uuid),)).fetchone()
            if not row:
                return False
            slot, prev = row[0], row[1]
            self._conn.execute(
                "UPDATE slots SET runtime_uuid=NULL, assigned_at=NULL, generation=? WHERE slot=?",
                (prev + 1, slot))
            self._conn.execute(
                "INSERT OR IGNORE INTO slot_generations (slot, generation, event, at) VALUES (?,?,?,?)",
                (slot, prev + 1, "release", int(now)))
            self._conn.commit()
            return True

    # ── audit evidence ──
    #: Material lifecycle events. EVERY one carries occupancy identity (slot AND generation), so a
    #: historical event can never be attributed to the current occupant merely because the slot matches.
    AUDIT_EVENTS = (
        "slot_assigned", "materialise_started", "materialise_completed", "runtime_started",
        "verification_completed", "stop_requested", "stop_completed", "tombstone_completed",
        "slot_released", "integrity_mismatch", "slot_quarantined", "quarantine_cleared",
    )

    def record_audit(self, *, event: str, runtime_uuid, slot, generation, operation="",
                     provisioning_job_id=None, correlation_id="", prior_state="", resulting_state="",
                     integrity_outcome="", quarantined=False, agent_version="", manifest_version="",
                     protocol_version=None, now: int = 0) -> None:
        """Append one material lifecycle event. Occupancy identity (``slot`` + ``generation``) is
        MANDATORY — an event without a generation must never be attributed to the current occupant."""
        if event not in self.AUDIT_EVENTS:
            raise ValueError(f"unknown audit event: {event}")
        if slot is None or generation is None:
            raise ValueError("audit events require both slot and generation (occupancy identity)")
        occ = occupancy_id(slot, generation)
        with self._lock:
            prev = self._conn.execute(
                "SELECT audit_hash FROM slot_audit ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else self.AUDIT_CHAIN_GENESIS
            material = {
                "event": event, "runtime_uuid": str(runtime_uuid), "slot": int(slot),
                "generation": int(generation), "occupancy_id": occ, "operation": operation,
                "provisioning_job_id": provisioning_job_id, "correlation_id": correlation_id,
                "prior_state": prior_state, "resulting_state": resulting_state,
                "integrity_outcome": integrity_outcome, "quarantined": bool(quarantined),
                "agent_version": agent_version, "manifest_version": manifest_version,
                "protocol_version": protocol_version, "at": int(now),
            }
            this_hash = _chain_hash(prev_hash, material)
            self._conn.execute(
                "INSERT INTO slot_audit (event, runtime_uuid, slot, generation, occupancy_id, operation,"
                " provisioning_job_id, correlation_id, prior_state, resulting_state, integrity_outcome,"
                " quarantined, agent_version, manifest_version, protocol_version, at,"
                " previous_audit_hash, audit_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event, str(runtime_uuid), int(slot), int(generation), occ, operation,
                 provisioning_job_id, correlation_id, prior_state, resulting_state, integrity_outcome,
                 1 if quarantined else 0, agent_version, manifest_version, protocol_version, int(now),
                 prev_hash, this_hash))
            self._conn.commit()

    def verify_audit_chain(self) -> dict:
        """Walk the forward-linked audit chain and report ``VALID`` or raise :class:`AuditChainCorrupt`.

        This is **not** cryptographic tamper-proofing — an actor who can rewrite the database can rewrite
        the chain. It exists to detect **accidental** deletion, insertion and ordering corruption, which is
        exactly what silently misleads an investigation.

        **No automatic repair.** A corrupt chain is an operator investigation, never a self-heal.
        """
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT id, event, runtime_uuid, slot, generation, occupancy_id, operation,"
                " provisioning_job_id, correlation_id, prior_state, resulting_state, integrity_outcome,"
                " quarantined, agent_version, manifest_version, protocol_version, at,"
                " previous_audit_hash, audit_hash FROM slot_audit ORDER BY id"))
        expected_prev = self.AUDIT_CHAIN_GENESIS
        for r in rows:
            material = {
                "event": r[1], "runtime_uuid": r[2], "slot": r[3], "generation": r[4],
                "occupancy_id": r[5], "operation": r[6], "provisioning_job_id": r[7],
                "correlation_id": r[8], "prior_state": r[9], "resulting_state": r[10],
                "integrity_outcome": r[11], "quarantined": bool(r[12]), "agent_version": r[13],
                "manifest_version": r[14], "protocol_version": r[15], "at": r[16],
            }
            if r[17] != expected_prev:
                raise AuditChainCorrupt(f"broken link at audit id {r[0]}")
            if _chain_hash(r[17], material) != r[18]:
                raise AuditChainCorrupt(f"content mismatch at audit id {r[0]}")
            expected_prev = r[18]
        return {"status": "VALID", "records": len(rows)}

    def audit_for_occupancy(self, slot: int, generation: int) -> list:
        """Events for ONE occupancy. Filtering on (slot, generation) — never slot alone — is what stops a
        previous occupant's history being read as the current one's."""
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT event, runtime_uuid, operation, prior_state, resulting_state, integrity_outcome,"
                " quarantined, at, occupancy_id FROM slot_audit WHERE slot=? AND generation=? ORDER BY id",
                (int(slot), int(generation))))
        return [{"event": r[0], "runtime_uuid": r[1], "operation": r[2], "prior_state": r[3],
                 "resulting_state": r[4], "integrity_outcome": r[5], "quarantined": bool(r[6]),
                 "at": r[7], "occupancy_id": r[8]} for r in rows]

    # ── operation sequencing (per occupancy) ──
    def record_sequence(self, *, slot, generation, operation, now: int = 0) -> int:
        """Allocate the next sequence number for THIS occupancy, starting at 1. Duplicates are impossible
        (primary key); the returned number is the operation's position in the occupancy lifecycle."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(sequence_number) FROM occupancy_sequence WHERE slot=? AND generation=?",
                (int(slot), int(generation))).fetchone()
            nxt = (row[0] or 0) + 1
            self._conn.execute(
                "INSERT INTO occupancy_sequence (slot, generation, sequence_number, operation, at)"
                " VALUES (?,?,?,?,?)", (int(slot), int(generation), nxt, operation, int(now)))
            self._conn.commit()
            return nxt

    def sequence_for_occupancy(self, slot, generation) -> list:
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT sequence_number, operation FROM occupancy_sequence"
                " WHERE slot=? AND generation=? ORDER BY sequence_number",
                (int(slot), int(generation))))
        return [{"sequence_number": n, "operation": op} for n, op in rows]

    def assert_sequence_valid(self, slot, generation) -> None:
        """A missing or duplicated sequence number is an INTEGRITY FAILURE, not a warning."""
        seq = [e["sequence_number"] for e in self.sequence_for_occupancy(slot, generation)]
        if seq != list(range(1, len(seq) + 1)):
            raise SlotIntegrityError()

    # ── quarantine ──
    def quarantine_slot(self, slot: int, reason: str, now: int = 0) -> None:
        """Mark a slot as failed-integrity. It is NEVER silently repaired or reused — operator only."""
        with self._lock:
            gen_row = self._conn.execute(
                "SELECT generation FROM slots WHERE slot=?", (int(slot),)).fetchone()
            gen = gen_row[0] if gen_row else None
            self._conn.execute(
                "INSERT OR REPLACE INTO slot_quarantine (slot, reason, at, occupancy_id, generation)"
                " VALUES (?,?,?,?,?)",
                (int(slot), str(reason)[:64], int(now),
                 occupancy_id(slot, gen) if gen is not None else None, gen))
            self._conn.commit()

    def is_quarantined(self, slot: int) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM slot_quarantine WHERE slot=?", (int(slot),)).fetchone() is not None

    def quarantined_slots(self) -> dict:
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT slot, reason, occupancy_id FROM slot_quarantine ORDER BY slot"))
        return {slot: {"reason": reason, "occupancy_id": occ} for slot, reason, occ in rows}

    # ── invariants ──
    def assert_generation_monotonic(self, slot: int, expected_generation: int) -> None:
        """GENERATION MONOTONICITY: for every slot the ledger must be exactly
        ``GENERATION_START, +1, +1, …`` with no gap, no repeat and no decrease, its last value must equal
        the slot's current generation, and that must equal ``expected_generation``.

        A violation is a PERMANENT integrity failure requiring operator intervention — it is never
        silently repaired.
        """
        with self._lock:
            gens = [r[0] for r in self._conn.execute(
                "SELECT generation FROM slot_generations WHERE slot=? ORDER BY generation", (int(slot),))]
            cur = self._conn.execute(
                "SELECT generation FROM slots WHERE slot=?", (int(slot),)).fetchone()
        if not gens or cur is None:
            raise SlotIntegrityError()
        expected_seq = list(range(self.GENERATION_START, self.GENERATION_START + len(gens)))
        if gens != expected_seq:                       # gap, repeat, decrease or wrong start
            raise SlotIntegrityError()
        if gens[-1] != cur[0] or cur[0] != int(expected_generation):
            raise SlotIntegrityError()

    #: Boundaries at which the audit chain MUST be verified — not merely in reporting tools.
    AUDIT_CHECKPOINTS = ("before_assign", "before_mutation", "before_release",
                         "before_quarantine_clearance", "before_acceptance_evidence")

    def audit_checkpoint(self, boundary: str, *, slot=None, now: int = 0) -> dict:
        """Verify the audit chain at a lifecycle boundary and act on corruption.

        On corruption: **fail closed**; quarantine the affected slot when attribution is possible; when
        attribution is uncertain (no slot in hand) **block all new allocation** instead of guessing. Never
        repaired — operator investigation only.
        """
        if boundary not in self.AUDIT_CHECKPOINTS:
            raise ValueError(f"unknown audit checkpoint: {boundary}")
        try:
            return self.verify_audit_chain()
        except AuditChainCorrupt:
            if slot is not None:
                self.quarantine_slot(int(slot), f"audit_chain_corrupt:{boundary}", now)
            else:
                self.block_allocation(f"audit_chain_corrupt:{boundary}", now)
            raise

    # ── allocation block (attribution uncertain) ──
    def block_allocation(self, reason: str, now: int = 0) -> None:
        """Stop handing out ANY slot. Used when corruption cannot be attributed to one slot."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO allocation_block (id, reason, at) VALUES (1,?,?)",
                (str(reason)[:96], int(now)))
            self._conn.commit()

    def allocation_blocked(self):
        with self._lock:
            row = self._conn.execute("SELECT reason FROM allocation_block WHERE id=1").fetchone()
        return row[0] if row else None

    def clear_allocation_block(self, *, operator_identity, evidence_reference, now: int = 0) -> None:
        """Operator-only. Requires attribution of who cleared it and why — never an implicit reset."""
        if not str(operator_identity or "").strip() or not str(evidence_reference or "").strip():
            raise QuarantineClearanceRefused("operator_identity and evidence_reference are required")
        with self._lock:
            self._conn.execute("DELETE FROM allocation_block WHERE id=1")
            self._conn.commit()

    #: The seven proofs that must ALL be durably true before a generation may advance.
    RELEASE_PROOFS = (
        "runtime_process_stopped",
        "process_identity_verified",
        "canonical_directory_tombstoned",
        "tombstone_evidence_persisted",
        "no_ambiguous_provisioning_job",
        "no_mutation_lock_held",
        "slot_release_audit_persisted",
    )

    def release_after_tombstone(self, *, runtime_uuid, slot, generation, proofs: dict, now: int = 0):
        """Advance a slot's generation ONLY after all seven release proofs are durably true.

        Ordering guarantee: the slot is **never exposed to another runtime between TOMBSTONE and successful
        generation advancement**. It stays occupied (``runtime_uuid`` set, so ``assign`` skips it) until the
        single transaction below commits; if that transaction fails or the process is interrupted, the slot
        remains occupied and the generation unadvanced — fail-closed, recoverable, never half-released.

        The whole advancement is ONE SQLite transaction: clear the runtime UUID, increment the generation by
        exactly one, append the ledger entry, and persist the free state. SQLite gives atomicity and
        durability here, so no separate recovery protocol is required; an interruption rolls back entirely.
        """
        missing = [p for p in self.RELEASE_PROOFS if not proofs.get(p)]
        if missing:
            raise ReleaseProofMissing(missing)
        with self._lock:
            row = self._conn.execute(
                "SELECT slot, generation FROM slots WHERE runtime_uuid=?",
                (str(runtime_uuid),)).fetchone()
            if not row or row[0] != int(slot) or row[1] != int(generation):
                raise SlotIntegrityError()          # the caller's view disagrees with the database
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE slots SET runtime_uuid=NULL, assigned_at=NULL, generation=? WHERE slot=?",
                    (int(generation) + 1, int(slot)))
                self._conn.execute(
                    "INSERT INTO slot_generations (slot, generation, event, at) VALUES (?,?,?,?)",
                    (int(slot), int(generation) + 1, "release", int(now)))
                self._conn.commit()
            except Exception:
                self._conn.rollback()               # slot stays OCCUPIED; generation unadvanced
                raise
        return int(generation) + 1

    # ── operator quarantine clearance ──
    def clear_quarantine(self, *, slot, diagnosed_reason, operator_identity, evidence_reference,
                         reconciliation_confirmed, no_runtime_process_confirmed,
                         slot_directory_safe_confirmed, runtime_uuid="operator", generation=None,
                         agent_version="", manifest_version="", protocol_version=None, now: int = 0):
        """Clear a quarantine. **Not a boolean reset.**

        Requires a diagnosed reason, the operator's identity, an evidence reference, and explicit
        confirmation of: database/ledger/marker reconciliation, that no runtime process remains, and that
        the slot directory is safe. Emits an auditable ``quarantine_cleared`` event.

        Historical ledger entries are **never rewritten or deleted** — clearance removes only the
        quarantine flag, leaving the generation history intact and auditable.
        """
        for name, value in (("diagnosed_reason", diagnosed_reason),
                            ("operator_identity", operator_identity),
                            ("evidence_reference", evidence_reference)):
            if not str(value or "").strip():
                raise QuarantineClearanceRefused(f"{name} is required")
        for name, value in (("reconciliation_confirmed", reconciliation_confirmed),
                            ("no_runtime_process_confirmed", no_runtime_process_confirmed),
                            ("slot_directory_safe_confirmed", slot_directory_safe_confirmed)):
            if not value:
                raise QuarantineClearanceRefused(f"{name} must be explicitly confirmed")
        if not self.is_quarantined(int(slot)):
            raise QuarantineClearanceRefused("slot is not quarantined")
        gen = self.generation_of(int(slot)) if generation is None else int(generation)
        if gen is None:
            raise QuarantineClearanceRefused("slot has no generation history")
        # Reconciliation must actually hold, not merely be asserted by the operator.
        self.assert_generation_monotonic(int(slot), gen)
        with self._lock:
            self._conn.execute("DELETE FROM slot_quarantine WHERE slot=?", (int(slot),))
            self._conn.commit()
        self.record_audit(event="quarantine_cleared", runtime_uuid=runtime_uuid, slot=int(slot),
                          generation=gen, operation="CLEAR_QUARANTINE",
                          prior_state="quarantined", resulting_state="cleared",
                          integrity_outcome="operator_cleared",
                          correlation_id=str(evidence_reference), quarantined=False,
                          agent_version=agent_version, manifest_version=manifest_version,
                          protocol_version=protocol_version, now=now)

    def assert_occupancy_integrity(self, *, runtime_uuid, slot, generation, marker_raw, now: int = 0):
        """THE pre-mutation gate. Asserts, in order: not quarantined → database / ownership marker /
        runtime UUID / slot / generation agree → generation monotonicity.

        On ANY failure the slot is **quarantined** and a sanitised ``slot_integrity_mismatch`` is raised.
        The caller must not continue; recovery is an operator action.
        """
        if self.is_quarantined(int(slot)):
            raise SlotIntegrityError()
        try:
            assert_slot_integrity(runtime_uuid=runtime_uuid, slot=slot, generation=generation,
                                  marker_raw=marker_raw)
            self.assert_generation_monotonic(slot, generation)
        except SlotIntegrityError:
            self.quarantine_slot(slot, "integrity_assertion_failed", now)
            raise

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


def owner_marker_digest(marker_raw) -> str:
    """SHA-256 (12-hex prefix) of the on-disk ownership marker — evidence that the marker was the expected
    one, without reproducing its contents."""
    return hashlib.sha256((marker_raw or "").encode("utf-8")).hexdigest()[:12]


def path_digest(canonical_dir) -> str:
    """SHA-256 (12-hex prefix) of the canonical path — lets the backend correlate/compare without ever
    receiving the local filesystem layout."""
    return hashlib.sha256((canonical_dir or "").encode("utf-8")).hexdigest()[:12]


def build_verification_evidence(*, runtime_uuid, slot, generation, canonical_dir, marker_raw,
                                pid=None, session_id=None, manifest_version="", protocol_version=None,
                                verified_at=None, started_at=None,
                                path_containment_verified=False,
                                executable_containment_verified=False) -> dict:
    """Assemble the LOCAL, immutable Provisioning Verification Report evidence.

    Generation is part of RUNTIME IDENTITY, not an implementation detail: ``(slot, generation)`` names one
    immutable occupancy of one execution slot, while ``runtime_uuid`` remains the logical identity.

    This is the **local** record and DOES retain the complete ``canonical_path``. Use
    :func:`remote_evidence` for anything crossing the management channel — the backend receives an
    attestation that the agent verified containment, not the filesystem layout itself.
    """
    return {
        "runtime_uuid": str(runtime_uuid),
        "slot": int(slot),
        "generation": int(generation),
        "occupancy_id": occupancy_id(slot, generation),
        "owner_marker_digest": owner_marker_digest(marker_raw),
        "canonical_path": canonical_dir,                       # LOCAL ONLY
        "canonical_path_digest": path_digest(canonical_dir),
        "path_containment_verified": bool(path_containment_verified),
        "executable_containment_verified": bool(executable_containment_verified),
        "pid": pid,
        "session_id": session_id,
        "manifest_version": manifest_version,
        "protocol_version": protocol_version,
        "verified_at": verified_at,
        "started_at": started_at,
    }


# Fields the management channel may carry. The complete path is deliberately absent: the agent derives and
# verifies containment on the box and the backend consumes only the attestation (verified — no backend
# lifecycle decision reads a path from an agent response).
REMOTE_EVIDENCE_FIELDS = (
    "runtime_uuid", "slot", "generation", "occupancy_id", "owner_marker_digest", "canonical_path_digest",
    "path_containment_verified", "executable_containment_verified",
    "pid", "session_id", "manifest_version", "protocol_version", "verified_at",
)


def remote_evidence(local_evidence: dict) -> dict:
    """Project local report evidence down to what may cross the management channel.

    Strips ``canonical_path`` (and anything else not explicitly allowed) so the local filesystem layout
    never leaves the host, while preserving the occupancy identity and containment attestations the
    backend needs for lifecycle decisions.
    """
    return {k: local_evidence[k] for k in REMOTE_EVIDENCE_FIELDS if k in local_evidence}


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
