"""
Packet B — B3: Local artifact storage service.

Provides a narrow helper for writing backtest artifact files to
local filesystem storage, computing checksums, enforcing size/filename
safety, and compressing text-like artifacts with gzip.

PostgreSQL stores metadata only (via BacktestArtifact).
Artifact file contents live on the local filesystem under
``settings.BACKTEST_ARTIFACT_ROOT``.

No cloud storage, no S3, no external dependencies.
"""
import gzip
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Configuration defaults ──

DEFAULT_ARTIFACT_ROOT = "backtest_artifacts"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Text-like artifact types eligible for gzip compression
_COMPRESSIBLE_TYPES = frozenset({
    "execution_log",
    "result_stub",
    "execution_manifest",
    "worker_log",
    "metrics",
    "trade_log",
    "equity_curve",
})

# Conservative safe-character set for filenames
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")
_PATH_TRAVERSAL_RE = re.compile(r"\.\.")


# ── Public data structures ──


@dataclass(frozen=True)
class StoredArtifact:
    """Metadata returned after successfully storing an artifact file."""

    file_path: str
    file_size: int
    checksum: str
    compressed: bool


# ── Configuration helpers ──


def _get_artifact_root() -> Path:
    """Return the configured artifact storage root directory."""
    root = getattr(settings, "BACKTEST_ARTIFACT_ROOT", None)
    if root:
        return Path(root)
    return Path(settings.BASE_DIR) / DEFAULT_ARTIFACT_ROOT


def _get_max_bytes() -> int:
    """Return the configured maximum artifact file size in bytes."""
    return int(getattr(settings, "BACKTEST_ARTIFACT_MAX_BYTES", DEFAULT_MAX_BYTES))


# ── Filename / path safety ──


def sanitize_filename(raw: str) -> str:
    """
    Sanitize a raw string into a safe filesystem filename.

    - Replaces unsafe characters with underscores
    - Rejects path traversal segments
    - Strips leading/trailing whitespace and dots
    - Returns a non-empty safe string
    """
    if not raw or not raw.strip():
        return "unnamed_artifact"

    # Block path traversal
    if _PATH_TRAVERSAL_RE.search(raw):
        raise ValueError(f"Path traversal detected in filename: {raw!r}")

    # Strip path separators — only the basename matters
    name = os.path.basename(raw)

    # Replace unsafe characters
    name = _SAFE_FILENAME_RE.sub("_", name)

    # Strip leading/trailing dots and whitespace
    name = name.strip(". ")

    if not name:
        return "unnamed_artifact"

    # Truncate to reasonable length
    return name[:200]


def build_artifact_path(
    execution_id: int,
    artifact_type: str,
    extension: str = "",
    compressed: bool = False,
) -> str:
    """
    Build a deterministic, safe relative path for an artifact file.

    Returns a path relative to BACKTEST_ARTIFACT_ROOT, for example:
        backtests/execution_42/execution_log_42.json.gz

    The path is safe for reruns because each execution has a unique id.
    """
    safe_type = sanitize_filename(artifact_type)
    filename = f"{safe_type}_{execution_id}"

    if extension:
        ext = extension.lstrip(".")
        filename = f"{filename}.{ext}"

    if compressed:
        filename = f"{filename}.gz"

    return f"backtests/execution_{execution_id}/{filename}"


# ── Core storage operations ──


def store_artifact(
    execution_id: int,
    artifact_type: str,
    content: bytes | str,
    extension: str = "",
) -> StoredArtifact:
    """
    Write artifact content to local filesystem storage.

    1. Sanitizes the filename
    2. Enforces max-size limit
    3. Compresses text-like artifacts with gzip
    4. Writes to local filesystem (append-only, never overwrites)
    5. Computes SHA-256 checksum of stored bytes
    6. Returns metadata for BacktestArtifact row creation

    Raises:
        ValueError: If content exceeds max size, or path is unsafe.
        FileExistsError: If artifact file already exists (immutability).
    """
    # Normalize content to bytes
    if isinstance(content, str):
        raw_bytes = content.encode("utf-8")
    else:
        raw_bytes = content

    # Size enforcement (pre-compression)
    max_bytes = _get_max_bytes()
    if len(raw_bytes) > max_bytes:
        raise ValueError(
            f"Artifact content exceeds maximum size: "
            f"{len(raw_bytes)} bytes > {max_bytes} bytes limit"
        )

    # Determine compression eligibility
    should_compress = artifact_type in _COMPRESSIBLE_TYPES

    # Build path
    rel_path = build_artifact_path(
        execution_id=execution_id,
        artifact_type=artifact_type,
        extension=extension,
        compressed=should_compress,
    )

    root = _get_artifact_root()
    abs_path = root / rel_path

    # Immutability: never overwrite existing files
    if abs_path.exists():
        raise FileExistsError(
            f"Artifact file already exists (immutability violation): {abs_path}"
        )

    # Ensure parent directory exists
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    # Compress and write
    if should_compress:
        stored_bytes = gzip.compress(raw_bytes)
    else:
        stored_bytes = raw_bytes

    abs_path.write_bytes(stored_bytes)

    # Compute checksum of stored bytes (post-compression)
    checksum = hashlib.sha256(stored_bytes).hexdigest()

    logger.info(
        "Stored artifact: %s (%d bytes, compressed=%s, checksum=%s)",
        rel_path,
        len(stored_bytes),
        should_compress,
        checksum[:16],
    )

    return StoredArtifact(
        file_path=rel_path,
        file_size=len(stored_bytes),
        checksum=checksum,
        compressed=should_compress,
    )
