"""Immutable raw-object landing with path safety, idempotency and quarantine.

Accepted raw bytes are written once via a UNIQUE staging directory and atomically
promoted; they are never edited or overwritten. Corrections are new objects.
Conflicts and malformed input are quarantined without touching accepted raw. A
prohibited credential/account-number key triggers a fail-closed security stop that
persists only a digest, never the body.

R2 hardening (GFX-PKT-006C-R2):
- a single strict manifest validator runs before every manifest write and on every
  manifest read; it verifies the exact field set, path safety, that the three paths
  live in the expected object directory, that the raw-object id matches the request
  directory, and that the stored request/response files exist (regular, in-root,
  non-symlink) with exact SHA-256 matching the manifest;
- each attempt uses a unique ``tempfile.mkdtemp`` staging dir and only ever cleans
  up its own staging; foreign staging is preserved;
- late races (a winning final/quarantine dir appearing during staging) are resolved
  by validating the winner and returning ALREADY_PRESENT only on exact byte match,
  else quarantining, never overwriting or deleting another attempt's data.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .contracts import (
    INSTANT_UTC_RE,
    MINUTE_UTC_RE,
    REQUEST_SCHEMA_ID,
    RESPONSE_SCHEMA_ID,
    ContractError,
    ProhibitedKeyError,
    canonical_json_bytes,
    find_prohibited_key,
    parse_canonical_utc_instant,
    sha256_hex,
    strict_json_loads,
    validate_request,
    validate_request_response_match,
    validate_response,
)

QUARANTINE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,12}$")
TIMEFRAME_RE = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Safe relative path: segments start with [A-Za-z0-9_-] (no dot), dots allowed
# only after the first segment char; single slashes; no backslash/empty/.././absolute.
SAFE_REL_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$")
MANIFEST_SCHEMA_ID = "https://guvfx.local/schema/raw_market_data_manifest_v1.schema.json"

MANIFEST_FIELDS = (
    "schema_version", "raw_object_id", "object_state", "source_id", "account_scope",
    "symbol", "timeframe", "representation", "range_start_utc", "range_end_utc",
    "range_semantics", "acquired_at_utc", "received_at_utc", "request_schema_id",
    "response_schema_id", "request_sha256", "response_sha256", "request_path",
    "response_path", "manifest_path", "code_commit", "timezone_status",
    "quarantine_reason", "limitations",
)


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

    # -- strict manifest validation ----------------------------------------
    def _validate_manifest(self, manifest: dict, *, files_dir: Path,
                           expected_rel_dir: str, expected_request_id: str) -> None:
        """Strictly validate a manifest + its stored files. Fail closed on any issue.

        Used before writing a manifest (files in staging) and on every read (files
        in the final/quarantine dir).
        """
        if not isinstance(manifest, dict) or set(manifest) != set(MANIFEST_FIELDS):
            raise StorageError("manifest field set invalid")
        if manifest["schema_version"] != "1.0":
            raise StorageError("manifest schema_version invalid")
        if manifest["object_state"] not in ("ACCEPTED", "QUARANTINED"):
            raise StorageError("manifest object_state invalid")
        if not isinstance(manifest["source_id"], str) or not SLUG_RE.match(manifest["source_id"]) \
                or len(manifest["source_id"]) > 64:
            raise StorageError("manifest source_id invalid")
        if not isinstance(manifest["account_scope"], str) or not SLUG_RE.match(manifest["account_scope"]) \
                or len(manifest["account_scope"]) > 64 or not any(c.isalpha() for c in manifest["account_scope"]):
            raise StorageError("manifest account_scope invalid")
        if not isinstance(manifest["symbol"], str) or not SYMBOL_RE.match(manifest["symbol"]):
            raise StorageError("manifest symbol invalid")
        if manifest["timeframe"] != "M1":
            raise StorageError("manifest timeframe must be M1")
        if manifest["representation"] != "bid_ohlc":
            raise StorageError("manifest representation invalid")
        # Semantic timestamp parsing (reject impossible calendar/time) + strict order.
        range_start = self._parse_manifest_instant(manifest["range_start_utc"], MINUTE_UTC_RE, "range_start_utc")
        range_end = self._parse_manifest_instant(manifest["range_end_utc"], MINUTE_UTC_RE, "range_end_utc")
        if not (range_end > range_start):
            raise StorageError("manifest range_end_utc must be later than range_start_utc")
        if manifest["range_semantics"] != "[start,end)":
            raise StorageError("manifest range_semantics invalid")
        for f in ("acquired_at_utc", "received_at_utc"):
            self._parse_manifest_instant(manifest[f], INSTANT_UTC_RE, f)
        if manifest["request_schema_id"] != REQUEST_SCHEMA_ID:
            raise StorageError("manifest request_schema_id is not the canonical v1 id")
        if manifest["response_schema_id"] != RESPONSE_SCHEMA_ID:
            raise StorageError("manifest response_schema_id is not the canonical v1 id")
        if not isinstance(manifest["code_commit"], str) or not manifest["code_commit"]:
            raise StorageError("manifest code_commit invalid")
        for f in ("request_sha256", "response_sha256"):
            if not isinstance(manifest[f], str) or not HEX64_RE.match(manifest[f]):
                raise StorageError(f"manifest {f} invalid")
        if manifest["timezone_status"] not in ("NOT_EVALUATED", "VERIFIED", "INCONCLUSIVE", "CONFLICT"):
            raise StorageError("manifest timezone_status invalid")
        if not isinstance(manifest["limitations"], list) or not all(
            isinstance(x, str) for x in manifest["limitations"]
        ):
            raise StorageError("manifest limitations invalid")

        # State / reason pairing.
        if manifest["object_state"] == "ACCEPTED":
            if manifest["quarantine_reason"] is not None:
                raise StorageError("ACCEPTED manifest must have null quarantine_reason")
        else:
            if not isinstance(manifest["quarantine_reason"], str) or not manifest["quarantine_reason"]:
                raise StorageError("QUARANTINED manifest must have a non-empty reason")

        # Identity must match the expected request-id directory.
        if not HEX64_RE.match(str(manifest["raw_object_id"])) \
                or manifest["raw_object_id"] != expected_request_id:
            raise StorageError("manifest raw_object_id does not match request directory")

        # Paths: safe, expected basenames, tied to the expected object directory,
        # and resolving beneath the configured root.
        expected = {
            "request_path": f"{expected_rel_dir}/request.json",
            "response_path": f"{expected_rel_dir}/response.json",
            "manifest_path": f"{expected_rel_dir}/manifest.json",
        }
        for key, exp in expected.items():
            val = manifest[key]
            if not isinstance(val, str) or not SAFE_REL_RE.match(val):
                raise StorageError(f"manifest {key} is not a safe relative path")
            if val != exp:
                raise StorageError(f"manifest {key} not in the expected object directory")
            self._resolve_under_root(*val.split("/"))  # raises if it escapes root

        # Stored files must exist (regular, in-root, non-symlink) and match digests.
        stored = {}
        for name, digest_key in (("request.json", "request_sha256"),
                                 ("response.json", "response_sha256")):
            fpath = files_dir / name
            if fpath.is_symlink() or not fpath.is_file():
                raise StorageError(f"stored {name} missing or not a regular file")
            resolved = fpath.resolve()
            if resolved != self.root and self.root not in resolved.parents:
                raise StorageError(f"stored {name} resolves outside the data root")
            data = fpath.read_bytes()
            if sha256_hex(data) != manifest[digest_key]:
                raise StorageError(f"stored {name} checksum does not match manifest")
            stored[name] = data

        # Provenance binding: the manifest identity/range must be derived from the
        # EXACT stored request/response, and the object directory must follow from
        # the parsed request. Applied to ACCEPTED objects (which hold a canonical
        # request and a valid response). QUARANTINED objects deliberately hold
        # invalid/noncanonical bytes, so they get the directory-identity check only.
        if manifest["object_state"] == "ACCEPTED":
            try:
                req_obj = strict_json_loads(stored["request.json"])
                resp_obj = strict_json_loads(stored["response.json"])
                if stored["request.json"] != canonical_json_bytes(req_obj):
                    raise StorageError("stored request bytes are not canonical")
                validate_request(req_obj)
                validate_response(resp_obj)
                validate_request_response_match(req_obj, resp_obj)
            except ContractError as exc:
                raise StorageError(f"stored request/response failed validation: {exc}") from None
            if req_obj["request_id"] != expected_request_id \
                    or resp_obj["request_id"] != expected_request_id:
                raise StorageError("stored request/response id does not match the object directory")
            for field in ("source_id", "account_scope", "symbol", "timeframe",
                          "representation", "range_start_utc", "range_end_utc",
                          "range_semantics"):
                if manifest[field] != req_obj[field]:
                    raise StorageError(f"manifest {field} differs from the stored request")
            src, scope, sym, tf, yyyy, mm = self._components(req_obj)
            derived = f"raw/mt5/{src}/{scope}/{sym}/{tf}/{yyyy}/{mm}/{expected_request_id}"
            if expected_rel_dir != derived:
                raise StorageError("object directory does not follow from the stored request")
        else:
            # Ordinary quarantine: parse and validate the EXACT stored request, then
            # bind the manifest identity, the directory and the 16-hex quarantine id
            # to those exact request bytes, the exact (possibly malformed) response
            # bytes and the reason. The response is deliberately NOT validated as a
            # success contract — malformed/invalid response evidence stays retainable.
            reason = manifest["quarantine_reason"]
            try:
                req_obj = strict_json_loads(stored["request.json"])
                if not isinstance(req_obj, dict):
                    raise StorageError("quarantined request.json is not a JSON object")
                validate_request(req_obj)
            except ContractError as exc:
                raise StorageError(
                    f"quarantined stored request failed validation: {exc}") from None
            # Canonical-byte rule: ordinary reasons require canonical request bytes;
            # the explicit noncanonical-attempt reason instead requires bytes that
            # decode to a valid request yet differ from the canonical encoding.
            canonical = canonical_json_bytes(req_obj)
            if reason == "noncanonical_request_bytes":
                if stored["request.json"] == canonical:
                    raise StorageError(
                        "noncanonical_request_bytes quarantine has canonical request bytes")
            elif stored["request.json"] != canonical:
                raise StorageError("ordinary quarantine request bytes are not canonical")
            # Identity and range derive from the stored request, never the manifest.
            if req_obj["request_id"] != expected_request_id:
                raise StorageError(
                    "quarantined stored request id does not match the object directory")
            for field in ("source_id", "account_scope", "symbol", "timeframe",
                          "representation", "range_start_utc", "range_end_utc",
                          "range_semantics"):
                if manifest[field] != req_obj[field]:
                    raise StorageError(
                        f"manifest {field} differs from the stored quarantined request")
            src, scope, sym, tf, yyyy, mm = self._components(req_obj)
            prefix = f"quarantine/mt5/{src}/{scope}/{sym}/{tf}/{yyyy}/{mm}/{expected_request_id}/"
            if not expected_rel_dir.startswith(prefix):
                raise StorageError("quarantine directory does not follow from the stored request")
            tail = expected_rel_dir[len(prefix):]
            if not QUARANTINE_ID_RE.match(tail):
                raise StorageError("quarantine directory has an invalid quarantine-id segment")
            # The 16-hex tail is the deterministic id over exact request bytes,
            # exact response bytes and the reason; recompute and require equality.
            expected_tail = sha256_hex(
                stored["request.json"] + b"\x00" + stored["response.json"]
                + b"\x00" + reason.encode("utf-8")
            )[:16]
            if tail != expected_tail:
                raise StorageError("quarantine id does not match request+response+reason")

    @staticmethod
    def _parse_manifest_instant(value, pattern: re.Pattern, field: str):
        # The pattern fixes the field-specific shape (e.g. minute-aligned range
        # bounds); the shared exact parser then validates the calendar/time and
        # returns a UtcInstant that preserves every admitted fractional digit.
        if not isinstance(value, str) or not pattern.match(value):
            raise StorageError(f"manifest {field} is not a UTC instant")
        try:
            return parse_canonical_utc_instant(value)
        except ContractError:
            raise StorageError(f"manifest {field} is not a valid calendar instant") from None

    def _load_manifest_failclosed(self, directory: Path, *, expected_rel_dir: str,
                                  expected_request_id: str) -> dict:
        path = directory / "manifest.json"
        try:
            manifest = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError("manifest missing or corrupt; failing closed") from exc
        self._validate_manifest(manifest, files_dir=directory,
                                expected_rel_dir=expected_rel_dir,
                                expected_request_id=expected_request_id)
        return manifest

    def _result_from_manifest(self, status: str, manifest: dict) -> dict:
        return {
            "status": status,
            "object_state": manifest["object_state"],
            "raw_object_id": manifest["raw_object_id"],
            "request_sha256": manifest["request_sha256"],
            "response_sha256": manifest["response_sha256"],
            "request_path": manifest["request_path"],
            "response_path": manifest["response_path"],
            "manifest_path": manifest["manifest_path"],
        }

    def _staging_parent(self) -> Path:
        parent = self._resolve_under_root(".staging")
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    # -- public API ---------------------------------------------------------
    def land(self, request: dict, response: dict, request_bytes: bytes,
             response_bytes: bytes, *, code_commit: str, acquired_at_utc: str,
             received_at_utc: str, timezone_status: str = "NOT_EVALUATED") -> dict:
        """Land an accepted raw object, or report ALREADY_PRESENT / quarantine."""
        for obj, label in ((request, "request"), (response, "response")):
            bad = find_prohibited_key(obj)
            if bad:
                self.security_stop(request, request_bytes, response_bytes, bad)
                raise ProhibitedKeyError(
                    f"prohibited key {bad!r} in {label}; body not persisted"
                )

        request_id = _safe_component(request["request_id"], "request_id", HEX64_RE)
        request_sha = sha256_hex(request_bytes)
        response_sha = sha256_hex(response_bytes)
        source, scope, symbol, timeframe, yyyy, mm = self._components(request)
        rel_parts = ("raw", "mt5", source, scope, symbol, timeframe, yyyy, mm, request_id)
        rel_dir = "/".join(rel_parts)
        final_dir = self._resolve_under_root(*rel_parts)

        # Canonical-request-byte guard.
        if request_bytes != canonical_json_bytes(request):
            q = self._quarantine(request, request_bytes, response_bytes, source, scope,
                                 symbol, timeframe, yyyy, mm, request_id, code_commit,
                                 acquired_at_utc, received_at_utc,
                                 reason="noncanonical_request_bytes")
            return {**q, "status": "QUARANTINED_CONFLICT"}

        if final_dir.exists():
            return self._resolve_existing_accepted(
                final_dir, rel_dir, request_id, request_sha, response_sha, request,
                request_bytes, response_bytes, source, scope, symbol, timeframe, yyyy,
                mm, code_commit, acquired_at_utc, received_at_utc)

        manifest = self._manifest(
            object_state="ACCEPTED", request=request, request_id=request_id,
            request_sha=request_sha, response_sha=response_sha, rel_parts=rel_parts,
            code_commit=code_commit, acquired_at_utc=acquired_at_utc,
            received_at_utc=received_at_utc, timezone_status=timezone_status,
            quarantine_reason=None,
        )
        staging = Path(tempfile.mkdtemp(dir=self._staging_parent(), prefix="accept-"))
        try:
            self._write_exclusive(staging / "request.json", request_bytes)
            self._write_exclusive(staging / "response.json", response_bytes)
            # Validate before writing the manifest (verifies staged files + checksums).
            self._validate_manifest(manifest, files_dir=staging, expected_rel_dir=rel_dir,
                                    expected_request_id=request_id)
            self._write_exclusive(staging / "manifest.json", canonical_json_bytes(manifest))
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, final_dir)  # atomic; fails if final already exists
            except OSError:
                # Late race: a winner appeared. Discard our staging, resolve the winner.
                shutil.rmtree(staging, ignore_errors=True)
                return self._resolve_existing_accepted(
                    final_dir, rel_dir, request_id, request_sha, response_sha, request,
                    request_bytes, response_bytes, source, scope, symbol, timeframe,
                    yyyy, mm, code_commit, acquired_at_utc, received_at_utc)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        return self._result_from_manifest("ACCEPTED", manifest)

    def _resolve_existing_accepted(self, final_dir, rel_dir, request_id, request_sha,
                                   response_sha, request, request_bytes, response_bytes,
                                   source, scope, symbol, timeframe, yyyy, mm,
                                   code_commit, acquired_at_utc, received_at_utc) -> dict:
        manifest = self._load_manifest_failclosed(
            final_dir, expected_rel_dir=rel_dir, expected_request_id=request_id)
        if manifest["request_sha256"] == request_sha and manifest["response_sha256"] == response_sha:
            return self._result_from_manifest("ALREADY_PRESENT", manifest)
        reason = "response_byte_conflict" if manifest["request_sha256"] == request_sha \
            else "request_byte_conflict"
        q = self._quarantine(request, request_bytes, response_bytes, source, scope,
                             symbol, timeframe, yyyy, mm, request_id, code_commit,
                             acquired_at_utc, received_at_utc, reason=reason)
        return {**q, "status": "QUARANTINED_CONFLICT"}

    def quarantine_malformed(self, request: dict, request_bytes: bytes,
                             response_bytes: bytes, *, code_commit: str,
                             acquired_at_utc: str, received_at_utc: str,
                             reason: str) -> dict:
        bad = find_prohibited_key(request)
        if bad:
            self.security_stop(request, request_bytes, response_bytes, bad)
            raise ProhibitedKeyError(f"prohibited key {bad!r}; body not persisted")
        request_id = _safe_component(request.get("request_id", ""), "request_id", HEX64_RE)
        source, scope, symbol, timeframe, yyyy, mm = self._components(request)
        return self._quarantine(
            request, request_bytes, response_bytes, source, scope, symbol, timeframe,
            yyyy, mm, request_id, code_commit, acquired_at_utc, received_at_utc,
            reason=reason,
        )

    # -- internals ----------------------------------------------------------
    def _quarantine(self, request, request_bytes, response_bytes, source, scope, symbol,
                    timeframe, yyyy, mm, request_id, code_commit, acquired_at_utc,
                    received_at_utc, *, reason) -> dict:
        quarantine_id = sha256_hex(
            request_bytes + b"\x00" + response_bytes + b"\x00" + reason.encode("utf-8")
        )[:16]
        rel_parts = ("quarantine", "mt5", source, scope, symbol, timeframe, yyyy, mm,
                     request_id, quarantine_id)
        rel_dir = "/".join(rel_parts)
        qdir = self._resolve_under_root(*rel_parts)
        request_sha = sha256_hex(request_bytes)
        response_sha = sha256_hex(response_bytes)

        if qdir.exists():
            return self._resolve_existing_quarantine(qdir, rel_dir, request_id,
                                                     request_sha, response_sha, reason)

        manifest = self._manifest(
            object_state="QUARANTINED", request=request, request_id=request_id,
            request_sha=request_sha, response_sha=response_sha, rel_parts=rel_parts,
            code_commit=code_commit, acquired_at_utc=acquired_at_utc,
            received_at_utc=received_at_utc, timezone_status="NOT_EVALUATED",
            quarantine_reason=reason,
        )
        staging = Path(tempfile.mkdtemp(dir=self._staging_parent(), prefix="quar-"))
        try:
            self._write_exclusive(staging / "request.json", request_bytes)
            self._write_exclusive(staging / "response.json", response_bytes)
            self._validate_manifest(manifest, files_dir=staging, expected_rel_dir=rel_dir,
                                    expected_request_id=request_id)
            self._write_exclusive(staging / "manifest.json", canonical_json_bytes(manifest))
            qdir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, qdir)
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)
                return self._resolve_existing_quarantine(qdir, rel_dir, request_id,
                                                         request_sha, response_sha, reason)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return self._result_from_manifest("QUARANTINED", manifest)

    def _resolve_existing_quarantine(self, qdir, rel_dir, request_id, request_sha,
                                     response_sha, reason) -> dict:
        manifest = self._load_manifest_failclosed(
            qdir, expected_rel_dir=rel_dir, expected_request_id=request_id)
        # The deterministic id binds request+response+reason; require an exact match.
        if (manifest["request_sha256"] != request_sha
                or manifest["response_sha256"] != response_sha
                or manifest.get("quarantine_reason") != reason):
            raise StorageError("existing quarantine object does not match; failing closed")
        return self._result_from_manifest("QUARANTINED", manifest)

    def security_stop(self, request, request_bytes, response_bytes, bad_key) -> None:
        """Persist ONLY digests + a redacted reason; never the offending body."""
        request_id = request.get("request_id", "unknown")
        if not re.match(r"^[0-9a-f]{64}$", str(request_id)):
            request_id = sha256_hex(request_bytes)
        sdir = self._resolve_under_root("quarantine", "security", request_id)
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

    @staticmethod
    def _write_exclusive(path: Path, data: bytes) -> None:
        with open(path, "xb") as fh:
            fh.write(data)
