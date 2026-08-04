"""WP5.1 — Operational Event Model (ADR-0032): shared vocabulary + the DARK feature flag.

The operational-event subsystem is a query-optimised, NON-SECRET, owner-scoped read model. It is
ADDITIVE and ships DARK behind ``OPERATIONS_EVENTS_ENABLED`` (default OFF). It is DISTINCT from — and
must never duplicate — the immutable ``core.audit`` security ledger. See docs/ADRs/0032-operational-event-model.md.
"""
from __future__ import annotations

import os


def operations_events_enabled() -> bool:
    """Master DARK flag for the operational-event subsystem. Default OFF.

    Read LIVE (a function, not an import-time constant) so tests and a future arming step toggle it
    without a process restart. Tolerant parser (1/true/yes/on) — the same idiom as
    ``reliability.constants.broker_health_enabled`` / ``execution.broker_gate.execution_gate_enabled``.
    """
    return os.getenv("OPERATIONS_EVENTS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


# ── Category vocabulary (future-safe; 7 fixed operational domains) ──
CATEGORY_VALIDATION = "VALIDATION"
CATEGORY_HEALTH = "HEALTH"
CATEGORY_EXECUTION = "EXECUTION"
CATEGORY_RUNTIME = "RUNTIME"
CATEGORY_CREDENTIAL = "CREDENTIAL"
CATEGORY_CONNECTIVITY = "CONNECTIVITY"
CATEGORY_SYSTEM = "SYSTEM"

CATEGORIES = (
    CATEGORY_VALIDATION, CATEGORY_HEALTH, CATEGORY_EXECUTION, CATEGORY_RUNTIME,
    CATEGORY_CREDENTIAL, CATEGORY_CONNECTIVITY, CATEGORY_SYSTEM,
)

# ── Severity vocabulary (WP5.1 spec: INFO/WARNING/ERROR/CRITICAL) ──
SEV_INFO = "INFO"
SEV_WARNING = "WARNING"
SEV_ERROR = "ERROR"
SEV_CRITICAL = "CRITICAL"

SEVERITIES = (SEV_INFO, SEV_WARNING, SEV_ERROR, SEV_CRITICAL)

# Severities that make an unresolved event "open" (needs-attention). INFO is informational only.
OPEN_SEVERITIES = (SEV_WARNING, SEV_ERROR, SEV_CRITICAL)

# Deterministic mapping from the various upstream severity vocabularies into this model's 4-value set.
# core.audit.AuditEvent.Severity = DEBUG/INFO/WARN/ERROR/CRITICAL ; reliability.AlertEvent.Severity =
# INFO/WARN/CRITICAL. Both use WARN (not WARNING) and audit adds DEBUG. This packet's model uses WARNING;
# the mapping is the documented reconciliation (ADR-0032 §Severity mapping). Unknown → INFO.
_SEVERITY_ALIASES = {
    "DEBUG": SEV_INFO,
    "INFO": SEV_INFO,
    "WARN": SEV_WARNING,
    "WARNING": SEV_WARNING,
    "ERROR": SEV_ERROR,
    "CRITICAL": SEV_CRITICAL,
    "FATAL": SEV_CRITICAL,
}


def normalize_severity(raw) -> str:
    """Map any upstream severity string into this model's 4-value vocabulary; unknown → INFO."""
    return _SEVERITY_ALIASES.get(str(raw or "").strip().upper(), SEV_INFO)


# ── Source vocabulary (free-form CharField; these are the recognised emitters a FUTURE wiring
# increment will pass. Kept as constants so the wiring layer and tests share one spelling.) ──
SOURCE_BROKER_VALIDATION = "broker_validation"
SOURCE_HEALTH_ENGINE = "health_engine"
SOURCE_EXECUTION_GATE = "execution_gate"
SOURCE_RUNTIME_PAUSE = "runtime_pause"
SOURCE_RUNTIME_RESUME = "runtime_resume"
SOURCE_CREDENTIAL_LIFECYCLE = "credential_lifecycle"
SOURCE_DISCONNECT = "disconnect"
SOURCE_MONITORING = "monitoring"
SOURCE_SYSTEM = "system"

# Default customer-visibility per category. VALIDATION/HEALTH/CREDENTIAL/CONNECTIVITY/RUNTIME describe
# the customer's own account posture; EXECUTION/SYSTEM default operator-only (internal diagnostics).
# A caller MAY override per event via record_event(customer_visible=...).
CUSTOMER_VISIBLE_DEFAULT = {
    CATEGORY_VALIDATION: True,
    CATEGORY_HEALTH: True,
    CATEGORY_CREDENTIAL: True,
    CATEGORY_CONNECTIVITY: True,
    CATEGORY_RUNTIME: True,
    CATEGORY_EXECUTION: False,
    CATEGORY_SYSTEM: False,
}


def default_customer_visible(category) -> bool:
    return bool(CUSTOMER_VISIBLE_DEFAULT.get(str(category or ""), False))


# ── Secret-safety: metadata key denylist (mirrors core.audit._sanitize_metadata). The operational
# event is NON-SECRET by contract; this is a defensive backstop so a mislabelled key can never persist
# a secret. Structural safety comes first from allow-listed callers passing secret-free projections. ──
SECRET_KEY_MARKERS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "auth",
    "credential", "private", "ssn", "credit_card", "cvv", "cipher", "ciphertext",
)
REDACTED = "[REDACTED]"

# ── Pagination bounds (there is no project-wide DRF pagination default; this endpoint sets its own). ──
DEFAULT_TIMELINE_LIMIT = 50
MAX_TIMELINE_LIMIT = 200
DEFAULT_RECENT_LIMIT = 20
