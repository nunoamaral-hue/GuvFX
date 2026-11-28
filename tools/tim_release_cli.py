#!/usr/bin/env python3
"""GO #2d operational-release command-line workflow (GFX-TIM GO #2d, ADR-0127). synthetic by default.

Four fail-closed, value-blind command-line entry points that turn the merged in-process proof
(``tools/tim_release_v3_operational_proof.py``) into the real producer<->consumer release handoff WITHOUT
adding any signing primitive to the repository (the producer signs; TIM only verifies — ADR-0070):

  build     Generate ONE bounded synthetic v0.3 release under an explicitly-approved local root and emit its
            payload, manifest and ``request.fields.json`` — the EXACT fields the producer host
            ``scripts/tim_signing_provider.py sign-release --fields-file`` consumes. Validates the request
            against the pinned producer contract and keeps the candidate UNSEALED (no signature, no READY).
  assemble  Ingest the returned ``release.signature.json`` and verify it against the exact candidate manifest
            and the PINNED operational trust store BEFORE sealing. Rejects a wrong key, an expired window, a
            mismatched signature or manifest, altered files, and an already-sealed candidate.
  publish   Copy a sealed candidate to an already-authenticated producer mount, verifying the destination
            identity and scope, refusing to overwrite an existing release, and writing READY LAST only after
            every destination file and hash verifies. A failed copy leaves the release READY-less (invisible
            to consumers) and reports its exact state plus the reviewed cleanup path.
  validate  A STANDALONE, read-only consumer that checks a published copy's READY binding, operational
            signature (against the pinned trust store), all nine evidence references, and every required file
            hash. Writes nothing.

This tool performs NO signing, NO real key handling, and NO network, NAS, MT5, broker, credential or
production action of its own. The signature is produced OUT OF BAND by the producer host (or, in CI, by a
disposable pure-python test signer that mirrors the producer contract). Receipts are sanitized: reason
codes, PUBLIC digests/fingerprints, counts and release-relative paths only — never key bytes, secrets or
host-absolute paths.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Verification-only research modules (stdlib; NO duckdb). The heavy synthetic generator (duckdb) is
# imported LAZILY inside ``cmd_build`` so ``assemble`` / ``publish`` / ``validate`` stay duckdb-free and the
# consumer command is genuinely standalone.
from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_contracts_v0_4 as V4  # noqa: E402  session-aware (ADR-0130)
from research.market_data import tim_calendar_pin_store as PINS  # noqa: E402  approved calendar pins
from research.market_data import tim_session_consumer as CON4  # noqa: E402  v0.4 session-aware consumer
from research.market_data import tim_release_signing_v3 as S  # noqa: E402
from research.market_data import tim_trust_store as TS  # noqa: E402
from research.market_data.contracts import (  # noqa: E402
    SHA256_RE,
    SLUG_RE,
    canonical_json_bytes,
    parse_canonical_utc_instant,
    sha256_hex,
)

PRODUCER_IDENTITY_DEFAULT = "guvfx_tim_producer"       # host-side signing identity (manifest producer_identity)
PUBLISH_IDENTITY_DEFAULT = "guvfx-tim-producer"        # NAS/SMB publish identity (mount scope marker)
DEFAULT_SCOPE = "exchange/releases"
SCOPE_MARKER = ".guvfx_producer_scope.json"
# The EXACT key set the producer ``sign-release --fields-file`` consumes (minus the key-derived
# signing_key_id / fingerprint, which the signer derives from its own key).
REQUEST_FIELDS = ("contract_bundle_digest", "created_at_utc", "key_validity", "limitations",
                  "manifest_digest", "producer_identity", "release_id", "validator_bundle_digest")
# Defaults matched to the merged proof (CI disposable-key path). The live packet overrides these with the
# pinned operational signing_key_id, its validity window, and a created_at within that window.
DEFAULT_CREATED_AT = "2026-06-01T00:00:00Z"
DEFAULT_KEY_NOT_BEFORE = "2026-01-01T00:00:00Z"
DEFAULT_KEY_NOT_AFTER = "2027-01-01T00:00:00Z"
DEFAULT_READY_CREATED_AT = "2026-01-12T00:03:02Z"      # after the synthetic published_at (fixture-fixed)


class CliError(Exception):
    """Fixed, value-blind CLI failure (a reason code only; never key bytes, paths or secrets)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---- small, value-blind helpers -----------------------------------------------------------------
def _is_within(path: str, root: str) -> bool:
    p, r = os.path.abspath(path), os.path.abspath(root)
    return p == r or p.startswith(r + os.sep)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _hash_file(path: str) -> str:
    return sha256_hex(_read_bytes(path))


def _exclusive_write(path: str, data: bytes) -> None:
    """Create ``path`` EXCLUSIVELY (never overwrite) and write ``data``. Mirrors the producer signer's
    one-approved-output discipline so a prior sealed artefact is never clobbered."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _emit(obj: dict, receipt_path: str | None = None) -> None:
    line = json.dumps(obj, sort_keys=True)
    sys.stdout.write(line + "\n")
    if receipt_path:
        with open(receipt_path, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _load_trust_store(path: str | None):
    """Load the PINNED operational trust store (the committed default) unless an explicit store is given
    (CI supplies an isolated test store pinning the disposable key)."""
    return TS.load(path) if path else TS.load_pinned_operational_trust_store()


def _instant_or_raise(value, reason: str):
    try:
        return parse_canonical_utc_instant(value)
    except Exception:
        raise CliError(reason)


def validate_request_fields(fields: dict) -> None:
    """Validate ``request.fields.json`` against the pinned producer contract: EXACTLY the eight fields the
    producer ``sign-release --fields-file`` consumes, each well-formed. Fail-closed, value-blind."""
    if not isinstance(fields, dict) or set(fields) != set(REQUEST_FIELDS):
        raise CliError("request_fields_shape")
    for name in ("release_id", "producer_identity"):
        v = fields[name]
        if not (isinstance(v, str) and SLUG_RE.match(v) and len(v) <= 64):
            raise CliError("request_%s_invalid" % name)
    for name in ("manifest_digest", "contract_bundle_digest", "validator_bundle_digest"):
        v = fields[name]
        if not (isinstance(v, str) and SHA256_RE.match(v) and len(v) == 64):
            raise CliError("request_%s_invalid" % name)
    _instant_or_raise(fields["created_at_utc"], "request_created_at_invalid")
    kv = fields["key_validity"]
    if not (isinstance(kv, dict) and set(kv) == {"not_before_utc", "not_after_utc"}):
        raise CliError("request_key_validity_shape")
    nb = _instant_or_raise(kv["not_before_utc"], "request_key_validity_invalid")
    na = _instant_or_raise(kv["not_after_utc"], "request_key_validity_invalid")
    if na < nb:
        raise CliError("request_key_validity_window")
    lim = fields["limitations"]
    if not (isinstance(lim, list) and all(isinstance(x, str) for x in lim)):
        raise CliError("request_limitations_invalid")


# ---- build ---------------------------------------------------------------------------------------
def cmd_build(args) -> int:
    root = os.path.abspath(args.root)
    if _is_within(root, REPO_ROOT):
        raise CliError("root_within_repo")  # data rule: releases live outside Git, under a configured root
    # The synthetic generator (duckdb + the v0.3 fixtures) is only needed to BUILD.
    import tim_release_v3_operational_proof as P  # noqa: E402  lazy (duckdb) import

    staging = os.path.join(root, "staging")
    os.makedirs(staging, exist_ok=True)
    rel_dir = os.path.join(staging, P.RELEASE_ID)
    if os.path.exists(rel_dir):
        raise CliError("candidate_exists")

    built = P.build_release(staging, signing_key_id=args.signing_key_id,
                            producer_identity=args.producer_identity)
    # Persist the EXACT canonical manifest bytes as manifest.json (the artefact the producer signs over and
    # the consumer re-hashes); its sha256 is the manifest_digest bound into the request + envelope.
    with open(os.path.join(rel_dir, "manifest.json"), "wb") as fh:
        fh.write(built["manifest_bytes"])

    key_validity = {"not_before_utc": args.key_not_before, "not_after_utc": args.key_not_after}
    fields = P.sign_release_fields(built, producer_identity=args.producer_identity,
                                   created_at_utc=args.created_at, key_validity=key_validity,
                                   limitations=list(args.limitations))
    validate_request_fields(fields)
    if fields["manifest_digest"] != built["manifest_digest"]:
        raise CliError("request_manifest_mismatch")
    req_bytes = canonical_json_bytes(fields)
    # Write the request for external signing (never overwrite an existing one).
    _exclusive_write(os.path.join(rel_dir, "request.fields.json"), req_bytes)

    receipt = {
        "receipt_type": "producer", "stage": "build", "state": "UNSEALED", "signed": False,
        "release_id": built["manifest"]["release_id"], "manifest_digest": built["manifest_digest"],
        "contract_bundle_digest": built["contract_bundle_digest"],
        "validator_bundle_digest": built["validator_bundle_digest"],
        "signing_key_id": args.signing_key_id, "producer_identity": args.producer_identity,
        "request_fields_sha256": sha256_hex(req_bytes), "request_fields_keys": sorted(fields),
        "created_at_utc": args.created_at, "key_validity": key_validity,
        "file_count": len(built["manifest"]["files"]),
        "evidence_references": len(built["manifest"]["evidence"]),
        "synthetic_data": True, "nas": "none", "network": "none", "real_signing_key": "none",
    }
    _emit(receipt, args.receipt)
    return 0


# ---- assemble ------------------------------------------------------------------------------------
def _seal(candidate: str, manifest_bytes: bytes, sig: dict, ready_created_at: str, *,
          synthetic: bool, limitations: list, contract_version: str = V.CONTRACT_VERSION) -> dict:
    """Seal the candidate IN PLACE: write ``manifest.sig.json`` (canonical), then the READY marker LAST,
    binding the manifest + signature-envelope digests. The READY marker's contract_version mirrors the
    manifest's (0.3 or the session-aware 0.4); the matching marker validator is dispatched. Returns the
    READY marker dict."""
    sig_bytes = canonical_json_bytes(sig)
    _exclusive_write(os.path.join(candidate, "manifest.sig.json"), sig_bytes)
    ready = {
        "schema_version": "1.0", "release_id": sig["release_id"], "state": "READY",
        "manifest_digest": sha256_hex(manifest_bytes), "signature_envelope_digest": sha256_hex(sig_bytes),
        "contract_version": contract_version, "synthetic": bool(synthetic), "created_at_utc": ready_created_at,
        "limitations": list(limitations),
    }
    V4.validate_ready_marker(ready)  # dispatches the 0.3 (frozen) or 0.4 marker validator
    _exclusive_write(os.path.join(candidate, "READY"), canonical_json_bytes(ready))
    return ready


def cmd_assemble(args) -> int:
    candidate = os.path.abspath(args.candidate)
    if not os.path.isdir(candidate):
        raise CliError("candidate_absent")
    staging, release_id = os.path.dirname(candidate), os.path.basename(candidate)
    mpath = os.path.join(candidate, "manifest.json")
    if not os.path.isfile(mpath):
        raise CliError("manifest_absent")
    # Reject an already-sealed candidate (existing release id / no clobber).
    if os.path.isfile(os.path.join(candidate, "manifest.sig.json")) or \
            os.path.isfile(os.path.join(candidate, "READY")):
        raise CliError("already_sealed")

    manifest_bytes = _read_bytes(mpath)
    manifest = V.strict_json_loads_v0_3(manifest_bytes)
    if manifest.get("release_id") != release_id:
        raise CliError("release_id_mismatch")

    # Candidate integrity: the request (if present) must bind to THIS manifest.
    rfpath = os.path.join(candidate, "request.fields.json")
    if os.path.isfile(rfpath):
        req = json.loads(_read_bytes(rfpath))
        if req.get("manifest_digest") != sha256_hex(manifest_bytes):
            raise CliError("request_manifest_mismatch")

    # Altered files: re-hash every signed file against the candidate before trusting the signature.
    for entry in manifest["files"]:
        fp = os.path.join(staging, entry["relative_path"])
        if not os.path.isfile(fp) or _hash_file(fp) != entry["digest"]["value"]:
            raise CliError("altered_files")

    # Ingest the returned signature (producer ``--out`` format: json.dumps(sort_keys)).
    spath = os.path.abspath(args.signature)
    if not os.path.isfile(spath):
        raise CliError("signature_absent")
    try:
        sig = json.loads(_read_bytes(spath))
    except Exception:
        raise CliError("signature_unreadable")
    if not isinstance(sig, dict):
        raise CliError("signature_shape")

    store = _load_trust_store(args.trust_store)
    result = S.verify_operational_release_signature_v3(sig, manifest_bytes, trust_store=store,
                                                       manifest=manifest)
    if not result.ok:
        # wrong key -> unknown_signing_key / key_fingerprint_mismatch / key_revoked / key_algorithm_mismatch;
        # expired window -> key_not_valid_at_created_at; bad signature -> signature_invalid;
        # bad manifest -> manifest_digest_mismatch / identity_mismatch; synthetic -> operational_required.
        raise CliError(result.reason)
    # Belt-and-braces identity: the envelope must name this release + its manifest.
    if sig.get("release_id") != manifest["release_id"] or \
            sig.get("signed_manifest_path") != manifest["release_id"] + "/manifest.json":
        raise CliError("identity_mismatch")

    ready = _seal(candidate, manifest_bytes, sig, args.ready_created_at,
                  synthetic=bool(manifest.get("synthetic", True)),
                  limitations=list(manifest.get("limitations", [])),
                  contract_version=manifest.get("contract_version", V.CONTRACT_VERSION))

    receipt = {
        "receipt_type": "producer", "stage": "assemble", "state": "SEALED", "sealed": True,
        "release_id": manifest["release_id"], "manifest_digest": ready["manifest_digest"],
        "signature_envelope_digest": ready["signature_envelope_digest"],
        "signing_key_id": sig["signing_key_id"], "public_key_fingerprint": sig["public_key_fingerprint"],
        "algorithm": sig["algorithm"], "signature_synthetic": sig["synthetic"],
        "verify_reason": result.reason, "altered_files": False, "ready_written_last": True,
        "evidence_references": len(manifest.get("evidence", {})), "file_count": len(manifest["files"]),
        "trust_store": "explicit" if args.trust_store else "pinned",
        "synthetic_data": True, "real_signing_key": "none",
    }
    _emit(receipt, args.receipt)
    return 0


# ---- publish -------------------------------------------------------------------------------------
def _verify_mount_identity(mount_root: str, scope: str, expect_identity: str | None,
                           no_check: bool) -> bool:
    marker = os.path.join(mount_root, SCOPE_MARKER)
    if not os.path.isfile(marker):
        if no_check:
            return False
        raise CliError("mount_identity_unverifiable")
    try:
        data = json.loads(_read_bytes(marker))
    except Exception:
        raise CliError("mount_identity_unreadable")
    expected_id = expect_identity or PUBLISH_IDENTITY_DEFAULT
    if data.get("producer_identity") != expected_id:
        raise CliError("mount_identity_mismatch")
    if data.get("scope") != scope:
        raise CliError("mount_scope_mismatch")
    return True


def _verify_published(dest_base: str, dest_rel: str, candidate: str, manifest: dict) -> None:
    # The two control files must be byte-identical to the sealed candidate.
    for name in ("manifest.json", "manifest.sig.json"):
        if _hash_file(os.path.join(dest_rel, name)) != _hash_file(os.path.join(candidate, name)):
            raise CliError("published_control_file_mismatch")
    # Every signed file must hash-match at the destination.
    for entry in manifest["files"]:
        fp = os.path.join(dest_base, entry["relative_path"])
        if not os.path.isfile(fp) or _hash_file(fp) != entry["digest"]["value"]:
            raise CliError("published_file_hash_mismatch")


def cmd_publish(args) -> int:
    candidate = os.path.abspath(args.candidate)
    if not os.path.isdir(candidate):
        raise CliError("candidate_absent")
    release_id = os.path.basename(candidate)
    for req in ("manifest.json", "manifest.sig.json", "READY"):
        if not os.path.isfile(os.path.join(candidate, req)):
            raise CliError("candidate_not_sealed")
    manifest_bytes = _read_bytes(os.path.join(candidate, "manifest.json"))
    manifest = V.strict_json_loads_v0_3(manifest_bytes)
    if manifest.get("release_id") != release_id:
        raise CliError("release_id_mismatch")

    mount_root = os.path.abspath(args.mount_root)
    if not os.path.isdir(mount_root):
        raise CliError("mount_absent")
    scope = args.scope.strip("/")
    identity_verified = _verify_mount_identity(mount_root, scope, args.expect_identity,
                                               args.no_identity_check)
    dest_base = os.path.join(mount_root, *scope.split("/"))
    dest_rel = os.path.join(dest_base, release_id)
    if os.path.exists(dest_rel):
        raise CliError("release_exists")  # overwrite refusal / existing release id
    os.makedirs(dest_rel)

    copied: list = []
    try:
        for name in sorted(os.listdir(candidate)):
            if name == "READY":
                continue  # READY is written LAST, only after the destination verifies
            shutil.copyfile(os.path.join(candidate, name), os.path.join(dest_rel, name))
            copied.append(name)
        _verify_published(dest_base, dest_rel, candidate, manifest)
    except CliError as exc:
        state = {
            "receipt_type": "producer", "stage": "publish", "result": "failed", "reason": exc.reason,
            "release_id": release_id, "scope": scope, "ready_written": False, "consumer_visible": False,
            "files_copied": sorted(copied), "identity_verified": bool(identity_verified),
            "mount_is_local_substitute": bool(args.local_substitute),
            "cleanup": {"action": "remove_incomplete_release_dir",
                        "release_dir_relative": scope + "/" + release_id,
                        "safe_because": "READY_absent_never_consumer_visible", "performed": False},
        }
        if args.cleanup_on_failure and not os.path.isfile(os.path.join(dest_rel, "READY")):
            shutil.rmtree(dest_rel)
            state["cleanup"]["performed"] = True
        _emit(state, args.receipt)
        return 3

    # Success: write READY to the destination LAST.
    shutil.copyfile(os.path.join(candidate, "READY"), os.path.join(dest_rel, "READY"))
    receipt = {
        "receipt_type": "producer", "stage": "publish", "result": "published", "release_id": release_id,
        "scope": scope, "ready_written_last": True, "consumer_visible": True,
        "identity_verified": bool(identity_verified), "expected_identity": args.expect_identity or PUBLISH_IDENTITY_DEFAULT,
        "published_file_count": len(copied) + 1, "all_destination_hashes_ok": True,
        "mount_is_local_substitute": bool(args.local_substitute),
        "manifest_digest": sha256_hex(manifest_bytes), "synthetic_data": True, "real_nas": "none",
    }
    _emit(receipt, args.receipt)
    return 0


# ---- validate (standalone, read-only consumer) --------------------------------------------------
def consumer_validate(release_dir: str, store) -> dict:
    """Read-only consumer acceptance of a published release. Fail-closed, in order: READY present and
    binding the manifest + signature digests; the operational signature verified against the PINNED store
    (K-7 cross-bind); all nine evidence references present + hash-matched; every required file SHA-256;
    evidence identity. Writes NOTHING. Returns a sanitized consumer receipt."""
    base = {"receipt_type": "consumer", "stage": "validate", "release_dir_name": os.path.basename(release_dir)}
    if not os.path.isfile(os.path.join(release_dir, "READY")):
        return {**base, "accepted": False, "reason": "missing_ready"}
    try:
        manifest_bytes = _read_bytes(os.path.join(release_dir, "manifest.json"))
        sig_bytes = _read_bytes(os.path.join(release_dir, "manifest.sig.json"))
        ready = V.strict_json_loads_v0_3(_read_bytes(os.path.join(release_dir, "READY")))
        manifest = V.strict_json_loads_v0_3(manifest_bytes)
        sig = V.strict_json_loads_v0_3(sig_bytes)
    except Exception:
        return {**base, "accepted": False, "reason": "unreadable_release"}

    if not (ready.get("state") == "READY"
            and ready.get("manifest_digest") == sha256_hex(manifest_bytes)
            and ready.get("signature_envelope_digest") == sha256_hex(sig_bytes)):
        return {**base, "accepted": False, "reason": "ready_binding_mismatch"}

    result = S.verify_operational_release_signature_v3(sig, manifest_bytes, trust_store=store, manifest=manifest)
    if not result.ok:
        return {**base, "accepted": False, "reason": result.reason}

    releases_parent = os.path.dirname(release_dir)
    evidence = manifest.get("evidence", {})
    if set(evidence) != set(V._EVIDENCE_ROLES):
        return {**base, "accepted": False, "reason": "evidence_roles_incomplete"}
    for role in V._EVIDENCE_ROLES:
        ref = evidence[role]
        fp = os.path.join(releases_parent, ref["relative_path"])
        if not os.path.isfile(fp) or _hash_file(fp) != ref["sha256"]:
            return {**base, "accepted": False, "reason": "evidence_reference_mismatch"}

    for entry in manifest["files"]:
        fp = os.path.join(releases_parent, entry["relative_path"])
        if not os.path.isfile(fp) or _hash_file(fp) != entry["digest"]["value"]:
            return {**base, "accepted": False, "reason": "file_hash_mismatch"}

    c_evidence = {}
    for role, fname in (("timezone_evidence", "timezone.json"), ("broker_server_identity", "broker_server.json"),
                        ("licensing", "licensing.json"), ("source_eligibility", "eligibility.json")):
        try:
            c_evidence[role] = V.strict_json_loads_v0_3(_read_bytes(os.path.join(release_dir, fname)))
        except Exception:
            return {**base, "accepted": False, "reason": "evidence_unreadable"}
    try:
        V.validate_release_evidence_identity(manifest, c_evidence)
    except (V.ContractError, V.QuarantineRequired):
        return {**base, "accepted": False, "reason": "evidence_identity_mismatch"}

    return {
        **base, "accepted": True, "reason": None, "release_id": manifest["release_id"],
        "signing_key_id": sig["signing_key_id"], "public_key_fingerprint": sig["public_key_fingerprint"],
        "algorithm": sig["algorithm"], "signature_synthetic": sig["synthetic"], "ready_bound": True,
        "operational_signature_verified": True, "evidence_references_verified": len(V._EVIDENCE_ROLES),
        "files_verified": len(manifest["files"]), "evidence_identity_verified": True,
        "synthetic_data": bool(manifest.get("synthetic", True)), "trust_store": "pinned",
    }


def cmd_validate(args) -> int:
    if args.release_dir:
        release_dir = os.path.abspath(args.release_dir)
    else:
        if not (args.mount_root and args.scope and args.release_id):
            raise CliError("validate_target_missing")
        release_dir = os.path.join(os.path.abspath(args.mount_root), *args.scope.strip("/").split("/"),
                                   args.release_id)
    store = _load_trust_store(args.trust_store)
    # Dispatch by the published manifest's contract_version: v0.4 uses the session-aware consumer (which
    # independently recomputes bar counts + gap classes against the pinned calendar and the actual data);
    # everything else stays on the frozen v0.3 consumer. Both are duckdb-free and read-only.
    contract_version = None
    mpath = os.path.join(release_dir, "manifest.json")
    if os.path.isfile(mpath):
        try:
            contract_version = json.loads(_read_bytes(mpath)).get("contract_version")
        except Exception:
            contract_version = None
    if contract_version == V4.CONTRACT_VERSION_V0_4:
        cal_store = PINS.load(args.calendar_store) if getattr(args, "calendar_store", None) else None
        receipt = CON4.consumer_validate_v0_4(release_dir, store, cal_store)
    else:
        receipt = consumer_validate(release_dir, store)
    _emit(receipt, args.receipt)
    return 0 if receipt["accepted"] else 4


# ---- CLI -----------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GuvFX-TIM operational-release workflow (build/assemble/publish/validate).")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build one synthetic release + request.fields.json (UNSEALED)")
    b.add_argument("--root", required=True, help="explicitly-approved local root (OUTSIDE the repo)")
    b.add_argument("--signing-key-id", required=True, help="the operational signing_key_id the manifest binds")
    b.add_argument("--producer-identity", default=PRODUCER_IDENTITY_DEFAULT)
    b.add_argument("--created-at", default=DEFAULT_CREATED_AT, help="signing time (must be within the key window)")
    b.add_argument("--key-not-before", default=DEFAULT_KEY_NOT_BEFORE)
    b.add_argument("--key-not-after", default=DEFAULT_KEY_NOT_AFTER)
    b.add_argument("--limitations", nargs="*", default=["synthetic_test_only", "FIRST_OPERATIONAL_SIGNATURE_SYNTHETIC_DATA"])
    b.add_argument("--receipt", default=None, help="also write the producer receipt JSON here")
    b.set_defaults(func=cmd_build)

    a = sub.add_parser("assemble", help="verify the returned signature and seal the candidate")
    a.add_argument("--candidate", required=True, help="the candidate release dir (…/staging/<release_id>)")
    a.add_argument("--signature", required=True, help="the returned release.signature.json")
    a.add_argument("--trust-store", default=None, help="explicit trust store (default: the pinned committed store)")
    a.add_argument("--ready-created-at", default=DEFAULT_READY_CREATED_AT)
    a.add_argument("--receipt", default=None)
    a.set_defaults(func=cmd_assemble)

    p = sub.add_parser("publish", help="copy a sealed candidate to an authenticated mount (READY last)")
    p.add_argument("--candidate", required=True, help="the sealed candidate release dir")
    p.add_argument("--mount-root", required=True, help="the already-authenticated producer mount root")
    p.add_argument("--scope", default=DEFAULT_SCOPE, help="destination scope under the mount (default exchange/releases)")
    p.add_argument("--expect-identity", default=None, help="required mount producer_identity (default guvfx-tim-producer)")
    p.add_argument("--no-identity-check", action="store_true",
                   help="proceed without a mount scope marker (records identity_verified=false; reduced assurance)")
    p.add_argument("--cleanup-on-failure", action="store_true",
                   help="on a failed copy, remove the READY-less (consumer-invisible) partial release dir")
    p.add_argument("--local-substitute", action="store_true", help="label the receipt: this mount is a local CI substitute")
    p.add_argument("--receipt", default=None)
    p.set_defaults(func=cmd_publish)

    v = sub.add_parser("validate", help="standalone read-only consumer acceptance of a published release")
    v.add_argument("--release-dir", default=None, help="the published <release_id> dir")
    v.add_argument("--mount-root", default=None)
    v.add_argument("--scope", default=DEFAULT_SCOPE)
    v.add_argument("--release-id", default=None)
    v.add_argument("--trust-store", default=None, help="explicit trust store (default: the pinned committed store)")
    v.add_argument("--calendar-store", default=None,
                   help="approved session-calendar pin store (required to validate a v0.4 release; without it "
                        "a v0.4 release fails closed as unapproved_calendar)")
    v.add_argument("--receipt", default=None)
    v.set_defaults(func=cmd_validate)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        _emit({"receipt_type": "error", "stage": getattr(args, "command", None), "reason": exc.reason},
              getattr(args, "receipt", None))
        return 2
    except (V.ContractError, V.QuarantineRequired, TS.TrustStoreIntegrityError, S.ContractError,
            PINS.CalendarPinError) as exc:
        # A contract/trust-store integrity failure is reported value-blind (never the exception text).
        _emit({"receipt_type": "error", "stage": getattr(args, "command", None),
               "reason": exc.__class__.__name__}, getattr(args, "receipt", None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
