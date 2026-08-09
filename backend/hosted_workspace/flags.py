"""hosted_workspace.flags — DARK-by-default feature gates for the Hosted Persistent MT5 Workspace.

Idiom B (settings-first-then-env, tolerant parser, read LIVE) — the same shape as the BETA_* runtime
gates (billing/beta.py:_flag, terminal_provisioning/beta_capacity.beta_runtimes_enabled). Read live so
they toggle without a restart; every accessor defaults OFF (empty-string default = falsey).

All three ship DARK. In THIS increment nothing in the execution, onboarding, or delivery path reads
these — the model and the pure matcher are inert foundation (ADR-0033). The flags exist so the later
increments that DO wire behaviour ship gated from their first line.
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    """Settings override wins, else env; empty/falsey default = OFF. Tolerant token set."""
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def hosted_persistent_mt5_enabled() -> bool:
    """Master gate for the Hosted Persistent MT5 Workspace subsystem. DEFAULT OFF."""
    return _flag("HOSTED_PERSISTENT_MT5_ENABLED")


def hosted_mt5_remoteapp_enabled() -> bool:
    """Gate for the RemoteApp / seamless-window delivery path (separate deploy concern). DEFAULT OFF."""
    return _flag("HOSTED_MT5_REMOTEAPP_ENABLED")


def hosted_mt5_active_account_polling_enabled() -> bool:
    """Gate for periodic active-account attach polling (a cadence concern, kept independent). DEFAULT OFF."""
    return _flag("HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED")


def hosted_workspace_onboarding_enabled() -> bool:
    """ADR-0034 Onboarding — the SUBSYSTEM-LEVEL gate for the customer-facing Hosted Workspace onboarding /
    provisioning journey (request → provision-intent → node bind → observe → discover → confirm → ready →
    strategy-eligible). DEFAULT OFF. ANDed with the master ``HOSTED_PERSISTENT_MT5_ENABLED``: onboarding is
    reachable only when BOTH are on. Never authorises an order; onboarding stops at assignment-eligibility,
    which is strictly below arming (``execution_enabled``) and below the live order-time bridge gate."""
    return _flag("HOSTED_WORKSPACE_ONBOARDING_ENABLED")


def hosted_mt5_execution_enabled() -> bool:
    """ADR-0034 Execution Engine (Decision D, condition 2) — the SUBSYSTEM-LEVEL gate that must be on before
    ANY Hosted Workspace (Provider B) account may execute. DEFAULT OFF. This is distinct from — and ANDed
    with — the master ``HOSTED_PERSISTENT_MT5_ENABLED`` (condition 1) and the per-workspace
    ``execution_enabled`` arm (condition 4): the master flag may be on for observation while execution stays
    dark. Never authorises an order by itself; the live order-time bridge gate remains authoritative."""
    return _flag("HOSTED_MT5_EXECUTION_ENABLED")
