"""Beta Readiness Stream 7C - the hosted executor's durable single-use nonce store.

Mirrors the proven ``deploy/beta-agent/stores.py`` construction: ONE SQLite file, an atomic
``INSERT OR IGNORE`` that reports whether THIS call was the first to burn a given nonce, and a prune by
expiry. Durability across a service restart is the point - a captured request may not be replayed even if the
daemon was bounced within the (short) validity window.

``verify_hosted_request`` calls ``burn(nonce, expiry) -> bool`` ONLY after the HMAC signature verifies, so an
unauthenticated peer can never fill this table. Concurrency is serialised by a ``threading.Lock`` because the
HTTP server is threaded.
"""
from __future__ import annotations

import sqlite3
import threading


class SqliteNonceStore:
    """Durable, thread-safe, single-use nonce store. One row per burned nonce; pruned by expiry."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        # check_same_thread=False: the threaded HTTP server calls burn() from worker threads; every access is
        # serialised by self._lock, so the single connection is used safely.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, expiry INTEGER NOT NULL)")
        self._conn.commit()

    def burn(self, nonce: str, expiry: int) -> bool:
        """ATOMIC single-use: insert the nonce and report whether THIS call was the first to do so. First use
        -> True; a replay of the same nonce -> False. Never raises into the verifier (a store failure denies)."""
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO nonces (nonce, expiry) VALUES (?, ?)", (str(nonce), int(expiry)))
                self._conn.commit()
                return cur.rowcount == 1
        except sqlite3.Error:
            return False   # fail closed: if we cannot prove single-use, treat as a replay (deny)

    def purge_expired(self, now: int) -> None:
        """Drop nonces whose expiry has passed - bounded storage. Best-effort (never raises)."""
        try:
            with self._lock:
                self._conn.execute("DELETE FROM nonces WHERE expiry < ?", (int(now),))
                self._conn.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass
