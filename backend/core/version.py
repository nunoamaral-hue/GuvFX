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

import logging
import os

logger = logging.getLogger(__name__)


def _safe(accessor) -> bool | None:
    """Resolve one flag accessor to a bool; None if it cannot be resolved (fail-open, never raises)."""
    try:
        return bool(accessor())
    except Exception:  # noqa: BLE001 — provenance must never break; report unknown as null
        logger.warning("version: flag accessor failed", exc_info=True)
        return None


def resolved_flags() -> dict[str, bool | None]:
    """Live booleans of the backend arming flags (the six broker-connectivity/operational-event flags +
    the three BETA flags). The two NEXT_PUBLIC_* frontend flags are build-time inlined and are reported
    by the frontend build-info, not here."""
    from trading.broker_connectivity import broker_connectivity_enabled
    from execution.broker_gate import execution_gate_enabled
    from reliability.constants import broker_health_enabled
    from operational_events.constants import operations_events_enabled
    from billing.beta import beta_onboarding_open
    from terminal_provisioning.beta_capacity import beta_runtimes_enabled
    from strategies.views import _beta_self_serve_arm_enabled

    return {
        "BROKER_CONNECTIVITY_ENABLED": _safe(broker_connectivity_enabled),
        "BROKER_CONNECTIVITY_EXECUTION_GATE": _safe(execution_gate_enabled),
        "BROKER_CONNECTIVITY_HEALTH_ENABLED": _safe(broker_health_enabled),
        "OPERATIONS_EVENTS_ENABLED": _safe(operations_events_enabled),
        "BETA_ONBOARDING_ENABLED": _safe(beta_onboarding_open),
        "BETA_RUNTIMES_ENABLED": _safe(beta_runtimes_enabled),
        "BETA_SELF_SERVE_ARM_ENABLED": _safe(_beta_self_serve_arm_enabled),
    }


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
