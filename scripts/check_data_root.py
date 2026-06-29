#!/usr/bin/env python3
"""Controller-side preflight validator for the GuvFX data root (`GUVFX_DATA_ROOT`).

Validates that the configured market-data storage root is the approved, dedicated
``GuvFXData`` target before any durable raw object is written, and emits **only**
logical labels and booleans — never an absolute path, NAS hostname, share path,
username or credential. This mechanises the storage-target gate used by the first
real-export packet (GFX-PKT-006D-A2-P5) and any future acquisition packet, so the
gate is a checkable control rather than an attestation.

Usage:
    GUVFX_DATA_ROOT=/path python3 scripts/check_data_root.py   # exit 0 if gate passes
    python3 scripts/check_data_root.py --root /path            # explicit root (tests)

The printed JSON contains booleans and the logical label only. It never contains
the path, so it is safe to surface in chat, Notion or evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

APPROVED_LABEL = "GuvFXData"
MARKER = ".guvfxdata"
MIN_FREE_BYTES = 1 << 30  # 1 GiB


def _outside_git(path: Path) -> bool:
    """True if no `.git` exists at the root or any ancestor."""
    p = path.resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return False
    return True


def _atomic_rename_ok(path: Path) -> bool:
    """True if the root supports create + atomic rename (needed for immutable publish)."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path), prefix=".dr_probe_")
        os.close(fd)
        dst = str(path / (".dr_probe_renamed_%d" % os.getpid()))
        os.replace(tmp, dst)
        os.remove(dst)
        return True
    except Exception:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _free_space_class(path: Path) -> str:
    try:
        st = os.statvfs(str(path))
        return "sufficient" if (st.f_bavail * st.f_frsize) > MIN_FREE_BYTES else "insufficient"
    except (OSError, AttributeError):
        return "unknown"


def validate(root):
    """Return a dict of logical/boolean gate results. Never includes the path."""
    out = {
        "data_root_set": bool(root),
        "exists_dir": False,
        "logical_label": "Unknown",
        "approved_marker_or_label": False,
        "outside_git": False,
        "writable_atomic_rename": False,
        "free_space_class": "unknown",
        "gate_pass": False,
    }
    if not root:
        return out
    p = Path(root)
    out["exists_dir"] = p.is_dir()
    if not out["exists_dir"]:
        return out
    label_ok = p.name == APPROVED_LABEL
    marker_ok = (p / MARKER).is_file()
    out["approved_marker_or_label"] = bool(label_ok or marker_ok)
    out["logical_label"] = APPROVED_LABEL if out["approved_marker_or_label"] else "Unknown"
    out["outside_git"] = _outside_git(p)
    out["writable_atomic_rename"] = _atomic_rename_ok(p)
    out["free_space_class"] = _free_space_class(p)
    out["gate_pass"] = (
        out["data_root_set"]
        and out["exists_dir"]
        and out["approved_marker_or_label"]
        and out["outside_git"]
        and out["writable_atomic_rename"]
        and out["free_space_class"] == "sufficient"
    )
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate GUVFX_DATA_ROOT (logical/boolean output only).")
    parser.add_argument("--root", default=os.environ.get("GUVFX_DATA_ROOT"))
    args = parser.parse_args(argv)
    result = validate(args.root)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["gate_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
