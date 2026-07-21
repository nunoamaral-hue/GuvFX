"""CVM-Inc-3 B2 — approved implementation manifest + integrity verification (requirement 6).

The manifest pins agent/protocol/manifest versions, the supported operations, and SHA-256 checksums of
the implementation modules. Mutating operations are refused unless the on-disk implementation matches the
approved manifest. The agent NEVER self-updates through its API.
"""
import hashlib
import json
import os

# Implementation modules whose integrity gates mutating operations.
IMPL_MODULES = ("op_impls.py", "win_ops.py", "lib/mgmt_protocol.py", "lib/mgmt_agent_core.py")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_checksums(base_dir: str) -> dict:
    return {m: sha256_file(os.path.join(base_dir, m)) for m in IMPL_MODULES}


def load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _combined(checksums: dict) -> str:
    """One digest over ALL implementation modules — op_impls.py, win_ops.py AND the bundled protocol +
    agent-core. Any single-module drift changes it, so a mutating op fails closed regardless of which
    implementation file was tampered (S1)."""
    return hashlib.sha256(
        json.dumps({m: checksums.get(m) for m in IMPL_MODULES}, sort_keys=True).encode("utf-8")).hexdigest()


def build_script_manifest(approved: dict, actual: dict, operations) -> dict:
    """Produce the per-op ``script_manifest`` the agent-core consumes: each op maps to the COMBINED
    approved checksum of every implementation module (+ its ``:actual`` counterpart). A drift in ANY
    implementation module therefore fails EVERY mutating op closed."""
    ca, cx = _combined(approved), _combined(actual)
    sm = {}
    for op in operations:
        name = f"op_{op.lower()}"
        sm[name] = ca
        sm[name + ":actual"] = cx
    return sm


def integrity_ok(approved: dict, actual: dict) -> bool:
    """True iff every approved implementation module checksum matches the on-disk value."""
    return all(approved.get(m) == actual.get(m) for m in IMPL_MODULES)
