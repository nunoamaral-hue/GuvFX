"""GFX-BETA-HEADLESS Increment 1 — server-side canonical runtime-directory generation.

Compensating controls enforced here:
  1  Canonical runtime directories are generated SERVER-SIDE, from the runtime's immutable UUID.
  4  One separate portable-MT5 directory per runtime (unique per UUID).
 11  No user-controlled filesystem paths — the frontend can NEVER supply a path or a path fragment.
 12  No path traversal — the only input is a UUID, which cannot contain separators or ``..``.

The Windows base is configuration, not a hard-coded constant (data rule: configurable paths).
Nothing here touches the filesystem; it only computes and validates the canonical string that the
Phase-3 Windows provisioner will materialise. Nuno's production runtimes never use this path.
"""
import os
import uuid as _uuid

from django.conf import settings

#: Canonical Windows base for the beta pool. Distinct from Nuno's ``C:\GuvFX\accounts`` and terminals.
_DEFAULT_BASE = r"C:\GuvFX\beta\accounts"


def beta_runtime_base() -> str:
    val = getattr(settings, "BETA_RUNTIME_BASE", None) or os.getenv("BETA_RUNTIME_BASE") or _DEFAULT_BASE
    return val.rstrip("\\/")


def canonical_beta_runtime_root(runtime_uuid) -> str:
    """Return the canonical ``<base>\\<uuid>\\terminal`` path for a beta runtime.

    ``runtime_uuid`` MUST be a real UUID (a ``uuid.UUID`` or its canonical string). Parsing it as a
    UUID is the traversal/injection guard: a valid UUID is ``[0-9a-f-]`` only, so it cannot contain
    ``\\``, ``/``, ``..`` or drive letters. Any non-UUID input raises ``ValueError`` — fail closed.
    """
    u = _uuid.UUID(str(runtime_uuid))  # raises ValueError on anything that is not a canonical UUID
    return f"{beta_runtime_base()}\\{u}\\terminal"


def assert_no_client_path(value) -> None:
    """Guard for any code path tempted to accept a runtime path from the client. Beta runtime paths
    are NEVER client-supplied; call this to reject one loudly if it ever appears."""
    if value:
        raise ValueError("runtime paths are server-generated only; a client-supplied path is refused")
