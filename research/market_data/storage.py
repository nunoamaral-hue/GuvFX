"""Immutable raw-object landing with path safety, idempotency and quarantine.

Accepted raw bytes are written once via a staging directory and atomically
promoted; they are never edited or overwritten. Corrections are new objects.
Conflicts and malformed input are quarantined without touching accepted raw. A
prohibited credential/account-number key triggers a fail-closed security stop that
persists only a digest, never the body.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .contracts import (
    PROHIBITED_KEYS,
    REQUEST_SCHEMA_ID,
    RESPONSE_SCHEMA_ID,
    ProhibitedKeyError,
    canonical_json_bytes,
    find_prohibited_key,
    sha256_hex,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,12}$")
TIMEFRAME_RE = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
MANIFEST_SCHEMA_ID = "https://guvfx.local/schema/raw_market_data_manifest_v1.schema.json"


class StorageError(RuntimeError):
    pass


class PathSafetyError(StorageError):
    pass


def _safe_component(value: str, kind: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not value:
        raise PathSafetyError(f"{kind} component is blank")
    if value in (".", ".."):
        raise PathSafetyError(f"{kind} component is a traversal token")
    if any(c in value for c in ("/", "\\", "\x00")) or any(c.isspace() for c in value):
        raise PathSafetyError(f"{kind} component contains an unsafe character")
    if not pattern.match(value):
        raise PathSafetyError(f"{kind} component is not a safe component: {value!r}")
    return value


class RawStore:
    """Landing store rooted at a validated, repo-external data root."""

    def __init__(self, root: Path):
        root = Path(root).resolve()
        if config._is_within_repo(root):
            raise PathSafetyError("data root must not be inside the Git repository")
        self.root = root

    # -- path helpers -------------------------------------------------------
    def _resolve_under_root(self, *parts: str) -> Path:
        dest = self.root.joinpath(*parts).resolve()
        if dest != self.root and self.root not in dest.parents:
            raise PathSafetyError("resolved path escapes the data root")
        return dest

    def _rel(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root)).replace(os.sep, "/")

    def _components(self, request: dict) -> tuple[str, str, str, str, str, str]:
        source = _safe_component(request["source_id"], "source_id", SLUG_RE)
        scope = _safe_component(request["account_scope"], "account_scope", SLUG_RE)
        if not any(c.isalpha() for c in scope):
            raise PathSafetyError("account_scope must not be all digits")
        symbol = _safe_component(request["symbol"], "symbol", SYMBOL_RE)
        timeframe = _safe_component(request["timeframe"], "timeframe", TIMEFRAME_RE)
        dt = datetime.fromisoformat(request["range_start_utc"].replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return source, scope, symbol, timeframe, f"{dt.year:04d}", f"{dt.month:02d}"

    # -- public API ---------------------------------------------------------
    def land(self, request: dict, response: dict, request_bytes: bytes,
             response_bytes: bytes, *, code_commit: str, acquired_at_utc: str,
             received_at_utc: str, timezone_status: str = "NOT_EVALUATED") -> dict:
        """Land an accepted raw object, or report ALREADY_PRESENT / quarantine."""
        # Fail-closed security stop: never persist a body containing a prohibited key.
        for obj, label in ((request, "request"), (response, "response")):
            bad = find_prohibited_key(obj)
            if bad:
                self.security_stop(request, request_bytes, response_bytes, bad)
                raise ProhibitedKeyError(
                    f"prohibited key {bad!r} in {label}; body not persisted"
                )

        request_id = request["request_id"]
        request_id = _safe_component(request_id, "request_id", re.compile(r"^[0-9a-f]{64}$"))
        request_sha = sha256_hex(request_bytes)
        response_sha = sha256_hex(response_bytes)

        source, scope, symbol, timeframe, yyyy, mm = self._components(request)
        rel_parts = ("raw", "mt5", source, scope, symbol, timeframe, yyyy, mm, request_id)
        final_dir = self._resolve_under_root(*rel_parts)

        if final_dir.exists():
            existing = json.loads((final_dir / "manifest.json").read_text("utf-8"))
            if existing.get("response_sha256") == response_sha:
                return self._result("ALREADY_PRESENT", "ACCEPTED", request_id,
                                    request_sha, response_sha, final_dir)
            # Same request id, different bytes -> conflict quarantine, accepted untouched.
            q = self._quarantine(request, request_bytes, response_bytes, source, scope,
                                 symbol, timeframe, yyyy, mm, request_id, request_sha,
                                 response_sha, code_commit, acquired_at_utc, received_at_utc,
                                 reason="response_byte_conflict")
            return {**q, "status": "QUARANTINED_CONFLICT"}

        manifest = self._manifest(
            object_state="ACCEPTED", request=request, request_id=request_id,
            request_sha=request_sha, response_sha=response_sha,
            rel_parts=rel_parts, code_commit=code_commit,
            acquired_at_utc=acquired_at_utc, received_at_utc=received_at_utc,
            timezone_status=timezone_status, quarantine_reason=None,
        )

        staging = self._resolve_under_root(".staging", f"accept-{request_id}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            self._write_exclusive(staging / "request.json", request_bytes)
            self._write_exclusive(staging / "response.json", response_bytes)
            self._write_exclusive(staging / "manifest.json", canonical_json_bytes(manifest))
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():  # race / late conflict
                raise StorageError("accepted object appeared during staging")
            os.replace(staging, final_dir)  # atomic promotion of the complete object
        finally:
            if staging.exists():
                shutil.rmtree(staging)

        return self._result("ACCEPTED", "ACCEPTED", request_id, request_sha,
                            response_sha, final_dir)

    def quarantine_malformed(self, request: dict, request_bytes: bytes,
                             response_bytes: bytes, *, code_commit: str,
                             acquired_at_utc: str, received_at_utc: str,
                             reason: str) -> dict:
        """Quarantine ordinary malformed/invalid data with exact bytes + reason."""
        bad = find_prohibited_key(request)
        if bad:
            self.security_stop(request, request_bytes, response_bytes, bad)
            raise ProhibitedKeyError(f"prohibited key {bad!r}; body not persisted")
        request_id = _safe_component(
            request.get("request_id", ""), "request_id", re.compile(r"^[0-9a-f]{64}$")
        )
        source, scope, symbol, timeframe, yyyy, mm = self._components(request)
        return self._quarantine(
            request, request_bytes, response_bytes, source, scope, symbol, timeframe,
            yyyy, mm, request_id, sha256_hex(request_bytes), sha256_hex(response_bytes),
            code_commit, acquired_at_utc, received_at_utc, reason=reason,
        )

    # -- internals ----------------------------------------------------------
    def _quarantine(self, request, request_bytes, response_bytes, source, scope, symbol,
                    timeframe, yyyy, mm, request_id, request_sha, response_sha,
                    code_commit, acquired_at_utc, received_at_utc, *, reason) -> dict:
        quarantine_id = sha256_hex(response_bytes + reason.encode("utf-8"))[:16]
        rel_parts = ("quarantine", "mt5", source, scope, symbol, timeframe, yyyy, mm,
                     request_id, quarantine_id)
        qdir = self._resolve_under_root(*rel_parts)
        manifest = self._manifest(
            object_state="QUARANTINED", request=request, request_id=request_id,
            request_sha=request_sha, response_sha=response_sha, rel_parts=rel_parts,
            code_commit=code_commit, acquired_at_utc=acquired_at_utc,
            received_at_utc=received_at_utc, timezone_status="NOT_EVALUATED",
            quarantine_reason=reason,
        )
        staging = self._resolve_under_root(".staging", f"quar-{request_id}-{quarantine_id}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            self._write_exclusive(staging / "request.json", request_bytes)
            self._write_exclusive(staging / "response.json", response_bytes)
            self._write_exclusive(staging / "manifest.json", canonical_json_bytes(manifest))
            qdir.parent.mkdir(parents=True, exist_ok=True)
            if not qdir.exists():
                os.replace(staging, qdir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self._result("QUARANTINED", "QUARANTINED", request_id, request_sha,
                            response_sha, qdir)

    def security_stop(self, request, request_bytes, response_bytes, bad_key) -> None:
        """Persist ONLY digests + a redacted reason; never the offending body."""
        request_id = request.get("request_id", "unknown")
        if not re.match(r"^[0-9a-f]{64}$", str(request_id)):
            request_id = sha256_hex(request_bytes)
        rel_parts = ("quarantine", "security", request_id)
        sdir = self._resolve_under_root(*rel_parts)
        record = {
            "schema_version": "1.0",
            "object_state": "QUARANTINED",
            "security_reason": "prohibited_key_present",
            "redacted_key_category": "credential_or_account_identifier",
            "request_sha256": sha256_hex(request_bytes),
            "response_sha256": sha256_hex(response_bytes),
            "note": "body intentionally not persisted",
        }
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / "security.json"
        if not path.exists():
            self._write_exclusive(path, canonical_json_bytes(record))

    def _manifest(self, *, object_state, request, request_id, request_sha, response_sha,
                  rel_parts, code_commit, acquired_at_utc, received_at_utc,
                  timezone_status, quarantine_reason) -> dict:
        base = "/".join(rel_parts)
        return {
            "schema_version": "1.0",
            "raw_object_id": request_id,
            "object_state": object_state,
            "source_id": request["source_id"],
            "account_scope": request["account_scope"],
            "symbol": request["symbol"],
            "timeframe": request["timeframe"],
            "representation": request["representation"],
            "range_start_utc": request["range_start_utc"],
            "range_end_utc": request["range_end_utc"],
            "range_semantics": request["range_semantics"],
            "acquired_at_utc": acquired_at_utc,
            "received_at_utc": received_at_utc,
            "request_schema_id": REQUEST_SCHEMA_ID,
            "response_schema_id": RESPONSE_SCHEMA_ID,
            "request_sha256": request_sha,
            "response_sha256": response_sha,
            "request_path": f"{base}/request.json",
            "response_path": f"{base}/response.json",
            "manifest_path": f"{base}/manifest.json",
            "code_commit": code_commit,
            "timezone_status": timezone_status,
            "quarantine_reason": quarantine_reason,
            "limitations": ["synthetic_test_only"],
        }

    def _result(self, status, object_state, request_id, request_sha, response_sha, d: Path) -> dict:
        base = self._rel(d)
        return {
            "status": status,
            "object_state": object_state,
            "raw_object_id": request_id,
            "request_sha256": request_sha,
            "response_sha256": response_sha,
            "request_path": f"{base}/request.json",
            "response_path": f"{base}/response.json",
            "manifest_path": f"{base}/manifest.json",
        }

    @staticmethod
    def _write_exclusive(path: Path, data: bytes) -> None:
        # Exclusive creation: accepted/quarantined files are never overwritten.
        with open(path, "xb") as fh:
            fh.write(data)
