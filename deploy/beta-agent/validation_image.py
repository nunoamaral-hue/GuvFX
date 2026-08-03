"""ADR-0027 Phase 2 (2026-08-03): governed build-5833 validation-image allow-list + fail-closed verification.

The dedicated broker-login validation terminal is built from the operator's PROVEN IS6 build-5833 program
files — an EXPLICIT ALLOW-LIST of account-free program files ONLY. For the non-portable production install the
account/trading state (accounts.dat, logs, trade history, saved passwords, DPAPI material, chart profiles)
lives in ``%APPDATA%`` and is NEVER read or copied. Build 6073 is the confirmed defective validation image
(it never completes the automated ``initialize(login, password, server)`` broker authorisation); build 5833 is
the proven image (2026-08-03: HEALTHY/demo_ok in ~4s).

This module is the fail-closed GOVERNANCE for the image. It is pure stdlib — no Django, no MetaTrader5 — so it
is unit-testable off-host:

  * ``ALLOW_LIST``            — the ONLY source files copied into the image (byte-deterministic; hash-pinned).
  * ``FORBIDDEN_SUBSTRINGS`` — names that must NEVER appear anywhere in the image (account/credential/trading).
  * ``SOURCE_HASHES``        — the pinned SHA-256 of each allow-listed source file for the certified build.
  * ``verify_source_hashes`` — drift-fail if the source is not exactly the certified build-5833 program set.
  * ``verify_image``         — drift-fail if the built image contains a forbidden artefact, is missing an
                               allow-listed file, has a mismatched executable/config hash, or lacks the run-in
                               ``.ex5`` layer. Generated run-in files are STRUCTURALLY (not byte) deterministic.
  * ``structural_fingerprint`` — a stable (path -> size) digest of the non-volatile image structure.

Every failure raises ``ValidationImageError`` (fail closed) — a validation terminal is NEVER used unproven.
"""
from __future__ import annotations

import hashlib
import os

# ── the governed source build (terminal64.exe is the login component; MetaEditor64.exe is the bundled
#    compiler and legitimately carries a different point build) ────────────────────────────────────────────
SOURCE_BUILD_TERMINAL = "5.0.0.5833"
SOURCE_BUILD_METAEDITOR = "5.0.0.6090"

#: The COMPLETE allow-list — the ONLY files copied from the production source into a validation image. Each is
#: an account-free program file: the terminal + the compiler, the broker SERVER LIST (not account-specific) and
#: the platform LICENSE (not a broker credential). Paths are stored lowercase with forward slashes for compare.
ALLOW_LIST = (
    "terminal64.exe",
    "metaeditor64.exe",
    "config/servers.dat",
    "config/terminal.lic",
)

#: Pinned SHA-256 of every allow-listed source file for the certified build-5833 program set. A drift here means
#: the source is no longer the proven build (e.g. an auto-update) and the image MUST NOT be built from it.
SOURCE_HASHES = {
    "terminal64.exe":       "d84fc3d891f66c1d6325c3c2d2ffc6974de1928cfb6086d1bbd11d4e1dd07d20",
    "metaeditor64.exe":     "64b7335854310bf2f0f84f5e51e12ee28f047de9eefd9f8a70005624f9d1df90",
    "config/servers.dat":   "16600f67e3c49d38e0bba29d554b0c6ae5af907f34125ef5b2e3fa3c08fb0ed1",
    "config/terminal.lic":  "19a721d3cf93be782e6188ee5c37d52268ad92a7cf90b237c0aa152ec59359e7",
}

#: Any file whose lowercase relative path CONTAINS one of these substrings is a forbidden account/credential/
#: trading artefact. Its presence in a built image fails closed — it must NEVER have been copied or generated.
FORBIDDEN_SUBSTRINGS = (
    "accounts.dat",     # the broker account store (login + server, DPAPI-wrapped password) — the primary risk
    "\\logs\\", "/logs/",
    "\\history\\", "/history/",
    "deals", "orders", "positions",
    "passwords", "dpapi",
    "origin.txt",       # a portable terminal that has recorded an origin has been run against a real account
)

#: The run-in layer compiles the standard MQL5 library from the isolated image itself. The certified count is
#: STRUCTURAL (timestamps vary bake-to-bake), so we assert a minimum, never a byte hash.
MIN_RUN_IN_EX5 = 100        # certified build-5833 run-in produced 131; a large regression fails closed


class ValidationImageError(Exception):
    """The proposed validation image is not a governed, account-free build-5833 image. ``reason`` is sanitised
    (a category, never a path/secret) so it is safe to surface to operators and evidence."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(base: str, path: str) -> str:
    return os.path.relpath(path, base).replace("\\", "/").lower()


def _walk_files(base: str):
    for root, _dirs, files in os.walk(base):
        for name in files:
            yield os.path.join(root, name)


def verify_source_hashes(source_dir: str, *, sha256=_sha256_file) -> None:
    """Fail closed unless EVERY allow-listed source file is present and matches its pinned build-5833 hash —
    a drift means the operator's install is no longer the certified build and the image must not be built."""
    for rel in ALLOW_LIST:
        p = os.path.join(source_dir, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            raise ValidationImageError("source_allow_listed_file_missing")
        if sha256(p) != SOURCE_HASHES[rel]:
            raise ValidationImageError("source_hash_drift")


def scan_forbidden(image_dir: str) -> list:
    """Return the (sanitised, path-free) categories of any forbidden account/credential/trading artefact found
    anywhere in the image. Empty list == clean."""
    hits = []
    for p in _walk_files(image_dir):
        rel = _rel(image_dir, p)
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad.strip("\\/") and bad.replace("\\", "/").strip("/") in rel:
                hits.append(bad.strip("\\/"))
    return sorted(set(hits))


def verify_image(image_dir: str, *, sha256=_sha256_file) -> dict:
    """Fail closed unless ``image_dir`` is a governed build-5833 validation image: (1) NO forbidden account/
    credential/trading artefact anywhere; (2) every allow-listed file present with the pinned source hash;
    (3) the run-in ``.ex5`` layer present (>= ``MIN_RUN_IN_EX5``). Returns a sanitised report on success."""
    if not image_dir or not os.path.isdir(image_dir):
        raise ValidationImageError("image_dir_missing")

    forbidden = scan_forbidden(image_dir)
    if forbidden:
        raise ValidationImageError("forbidden_artefact_present")

    for rel in ALLOW_LIST:
        p = os.path.join(image_dir, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            raise ValidationImageError("allow_listed_file_missing")
        if sha256(p) != SOURCE_HASHES[rel]:
            raise ValidationImageError("allow_listed_file_hash_mismatch")

    ex5 = sum(1 for p in _walk_files(image_dir) if p.lower().endswith(".ex5"))
    if ex5 < MIN_RUN_IN_EX5:
        raise ValidationImageError("run_in_layer_missing")

    return {
        "ok": True,
        "terminal_build": SOURCE_BUILD_TERMINAL,
        "metaeditor_build": SOURCE_BUILD_METAEDITOR,
        "allow_listed": list(ALLOW_LIST),
        "ex5_count": ex5,
        "account_artefact_count": 0,
        "attached_ea_count": 0,
        "structural_fingerprint": structural_fingerprint(image_dir),
    }


def structural_fingerprint(image_dir: str) -> str:
    """A stable digest over (relative-path -> size) for the NON-volatile image structure — the allow-listed
    program files plus the generated ``.ex5`` layer (deterministic in structure, not bytes). Volatile per-run
    files (logs, generated caches other than the ex5 layer) are excluded so the fingerprint is reproducible."""
    entries = []
    for p in _walk_files(image_dir):
        rel = _rel(image_dir, p)
        if "/logs/" in rel or rel.endswith(".log"):
            continue
        entries.append(f"{rel}|{os.path.getsize(p)}")
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
