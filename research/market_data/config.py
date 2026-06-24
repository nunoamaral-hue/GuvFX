"""Data-root resolution and mode selection (GFX-PKT-006C).

Fail-closed: real operations require an explicit, safe ``GUVFX_DATA_ROOT`` that is
NOT inside the Git repository and has NO fallback. Synthetic operations require an
explicit, caller-supplied temporary root (never the environment, never the repo).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Repository root: research/market_data/config.py -> parents[2] == repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

ENV_DATA_ROOT = "GUVFX_DATA_ROOT"


class DataRootError(RuntimeError):
    """Raised when a data root is missing, blank, unsafe, or inside the repo."""


@dataclass(frozen=True)
class ResolvedRoot:
    path: Path
    mode: str  # "synthetic" | "real"


def _is_within_repo(path: Path) -> bool:
    """True if ``path`` equals the repository root or sits inside it."""
    resolved = path.resolve()
    if resolved == REPO_ROOT:
        return True
    return REPO_ROOT in resolved.parents


def _validate_root_path(raw: str | os.PathLike | None) -> Path:
    """Validate a candidate data-root path; raise DataRootError on any problem."""
    if raw is None:
        raise DataRootError("data root is unset")
    text = str(raw).strip()
    if not text:
        raise DataRootError("data root is blank")
    path = Path(text)
    if not path.is_absolute():
        raise DataRootError("data root must be an absolute path")
    if _is_within_repo(path):
        raise DataRootError("data root must not be inside the Git repository")
    return path


def resolve_real_data_root(environ: dict | None = None) -> ResolvedRoot:
    """Resolve the REAL data root from the environment, failing closed.

    No default and no repository fallback. Used only by future authorised real
    operations; this packet never calls it against a live root.
    """
    env = os.environ if environ is None else environ
    path = _validate_root_path(env.get(ENV_DATA_ROOT))
    return ResolvedRoot(path=path, mode="real")


def synthetic_data_root(tmp_root: str | os.PathLike) -> ResolvedRoot:
    """Resolve a SYNTHETIC data root from an explicit caller-supplied temp path.

    Never reads the environment and never uses the repository.
    """
    if tmp_root is None:
        raise DataRootError("synthetic root must be supplied explicitly")
    path = _validate_root_path(tmp_root)
    return ResolvedRoot(path=path, mode="synthetic")
