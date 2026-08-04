"""IPR Area G — build provenance + live arming-flag snapshot for the backend image.

`provenance()` returns the non-secret build fingerprint (commit / timestamp / release id, injected at
`docker build` time via build-args — see backend/Dockerfile) plus the RESOLVED live booleans of the
backend arming flags. The shared backend image runs `guvfx-backend` + `trade-ingest` + `shadow`, so one
fingerprint describes all three; the isolated `wayond-listener` image is NOT covered here.

Including the resolved flag booleans closes the WP5.4 gap where arming state was
"HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL": the staff-only /api/version/ surface IS that
host-verification oracle, and every value here is read-live + non-secret (names + booleans only — never
a flag VALUE or a secret). Flag resolution is fail-open: a flag whose accessor cannot be imported is
reported as ``null`` rather than raising.
"""
from __future__ import annotations

import importlib
import logging
import os

logger = logging.getLogger(__name__)

# (module, accessor) for each backend arming flag. Resolved lazily + fail-open (below) so a later
# accessor rename — exactly the drift this oracle exists to catch — reports `null` instead of 500ing.
_FLAG_ACCESSORS = {
    "BROKER_CONNECTIVITY_ENABLED": ("trading.broker_connectivity", "broker_connectivity_enabled"),
    "BROKER_CONNECTIVITY_EXECUTION_GATE": ("execution.broker_gate", "execution_gate_enabled"),
    "BROKER_CONNECTIVITY_HEALTH_ENABLED": ("reliability.constants", "broker_health_enabled"),
    "OPERATIONS_EVENTS_ENABLED": ("operational_events.constants", "operations_events_enabled"),
    "BETA_ONBOARDING_ENABLED": ("billing.beta", "beta_onboarding_open"),
    "BETA_RUNTIMES_ENABLED": ("terminal_provisioning.beta_capacity", "beta_runtimes_enabled"),
    "BETA_SELF_SERVE_ARM_ENABLED": ("strategies.views", "_beta_self_serve_arm_enabled"),
}


def _resolve(module_name: str, attr: str) -> bool | None:
    """Import + call one flag accessor → bool; None if EITHER the import or the call fails (fail-open,
    never raises). Guarding the import too is deliberate: a renamed/removed accessor must not 500 the
    provenance endpoint."""
    try:
        return bool(getattr(importlib.import_module(module_name), attr)())
    except Exception:  # noqa: BLE001 — provenance must never break; report unknown as null
        logger.warning("version: flag accessor %s.%s failed", module_name, attr, exc_info=True)
        return None


def resolved_flags() -> dict[str, bool | None]:
    """Live booleans of the backend arming flags (the six broker-connectivity/operational-event flags +
    the three BETA flags). The two NEXT_PUBLIC_* frontend flags are build-time inlined and are reported
    by the frontend build-info, not here."""
    return {name: _resolve(mod, attr) for name, (mod, attr) in _FLAG_ACCESSORS.items()}


def provenance() -> dict:
    """Non-secret build fingerprint + resolved live flag snapshot. Safe to serialise to a staff caller."""
    return {
        "service": "guvfx-backend",
        "git_commit": os.getenv("GUVFX_GIT_COMMIT", "unknown"),
        "build_timestamp": os.getenv("GUVFX_BUILD_TIMESTAMP", "unknown"),
        "release_id": os.getenv("GUVFX_RELEASE_ID", "unknown"),
        "flags": resolved_flags(),
        "note": (
            "Covers the shared backend image (guvfx-backend / trade-ingest / shadow). "
            "The wayond-listener image and the frontend build are fingerprinted separately."
        ),
    }
