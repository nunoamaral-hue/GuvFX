"""Broker-validation abstraction — the provider-driven seam.

Broker validation (is this broker account record well-formed? later: does it actually log in?) is
resolved through a ``BrokerValidator`` provider keyed by broker family, NOT hard-coded to one broker.
The first concrete integration (``mt5``) consumes this abstraction like any other provider rather than
being a special case, so a second broker family is added by registering another provider — no change to
the callers.

This module deliberately does NOT import any Django model: a validator is handed a duck-typed ``account``
object (a ``TradingAccount``) and reads only its public attributes, keeping the abstraction broker-agnostic
and free of circular imports.

Scope note: ``validate_account_record`` is BROKER-INDEPENDENT — it checks the shape/completeness of the
stored record (format only) and performs NO broker connectivity. Live broker-login verification is a
distinct, later stage that will extend the provider surface; it is intentionally not implemented here.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class BrokerValidationResult:
    """Outcome of a broker-record validation. ``reason_code`` is user-safe/sanitised (never a secret);
    ``detail`` is admin-only context. ``normalized_login``/``normalized_server`` are the canonical values
    the provider derived from the record (empty on failure)."""
    ok: bool
    reason_code: str = "ok"
    detail: str = ""
    normalized_login: str = ""
    normalized_server: str = ""


@runtime_checkable
class BrokerValidator(Protocol):
    """A broker family's validation provider. ``key`` is the family token it registers under."""
    key: str

    def validate_account_record(self, account) -> BrokerValidationResult:
        """Validate the STORED broker account record (format/completeness only, no connectivity)."""
        ...


@dataclass(frozen=True)
class FailClosedValidator:
    """Fallback provider for an unrecognised broker family: every record is rejected. Keeps dispatch
    fail-closed so an unknown/unsupported broker can never silently pass validation."""
    key: str = "unknown"
    families: tuple = field(default=())

    def validate_account_record(self, account) -> BrokerValidationResult:
        return BrokerValidationResult(False, "unsupported_broker",
                                      detail="no validator registered for this broker family")
