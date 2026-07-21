"""Broker-validation abstraction (provider-driven).

Public surface: resolve a broker account's validation provider via ``get_broker_validator(account)`` and
call ``validate_account_record``. Providers self-register on import; importing this package registers the
built-in MetaTrader 5 provider.
"""
from .base import BrokerValidationResult, BrokerValidator, FailClosedValidator
from .registry import (
    broker_family,
    get_broker_validator,
    get_validator_for_family,
    register_broker_validator,
)

# Import concrete providers for their registration side-effect (the first broker integration).
from . import mt5 as _mt5  # noqa: F401

__all__ = [
    "BrokerValidationResult",
    "BrokerValidator",
    "FailClosedValidator",
    "broker_family",
    "get_broker_validator",
    "get_validator_for_family",
    "register_broker_validator",
]
