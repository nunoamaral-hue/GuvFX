"""Broker-validator registry + family resolver — the provider-driven dispatch.

A validator registers itself under one or more broker-family tokens. ``get_broker_validator(account)``
resolves the account's family and returns the matching provider, or a fail-closed fallback when the
family is unregistered. This is the seam that makes broker validation provider-driven: adding a broker
family is registering a provider, not editing the callers.
"""
from .base import BrokerValidator, FailClosedValidator

# family token → provider. Populated by each provider module (e.g. ``brokers.mt5``) at import time.
_REGISTRY: dict[str, BrokerValidator] = {}

_FAIL_CLOSED = FailClosedValidator()


def register_broker_validator(validator: BrokerValidator, *families: str) -> None:
    """Register ``validator`` under its own ``key`` plus any extra family aliases."""
    for fam in (validator.key, *families):
        _REGISTRY[fam.strip().lower()] = validator


def broker_family(account) -> str:
    """Resolve the broker family token for ``account``.

    The GuvFX platform trades exclusively through MetaTrader 5 today, so every broker account maps to
    the ``mt5`` family. This resolver is the single place a future multi-broker platform would branch on
    (e.g. an explicit ``account.broker_family`` field) — callers never encode broker knowledge."""
    fam = (getattr(account, "broker_family", "") or "").strip().lower()
    return fam or "mt5"


def get_validator_for_family(family: str) -> BrokerValidator:
    """Return the provider registered for ``family``, or the fail-closed fallback if none is."""
    return _REGISTRY.get((family or "").strip().lower(), _FAIL_CLOSED)


def get_broker_validator(account) -> BrokerValidator:
    """Return the validation provider for ``account``'s broker family (fail-closed if unregistered)."""
    return get_validator_for_family(broker_family(account))
