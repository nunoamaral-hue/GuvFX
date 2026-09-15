"""broker_catalogue.flags — DARK-by-default gate for Broker Catalogue V1 provisioning consumption.

Same tolerant settings-first-then-env idiom as hosted_workspace.flags. Default OFF: while off, provisioning is
byte-identical to before this app (no catalogue lookup, no preseed) and a fresh runtime uses native MT5 discovery.
Turning it on only lets provisioning CONSUME an already-APPROVED, hash-verified catalogue artefact — it never
promotes a candidate (that stays human-gated in the ``approvals`` app) and never touches the golden image.
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def hosted_broker_catalogue_enabled() -> bool:
    """Master gate for Broker Catalogue V1 provisioning consumption. DEFAULT OFF."""
    return _flag("HOSTED_BROKER_CATALOGUE_ENABLED")
