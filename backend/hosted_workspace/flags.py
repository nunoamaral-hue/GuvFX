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


def hosted_observation_scheduler_enabled() -> bool:
    """Beta Readiness Stream 2 (G2/G15) — the SCHEDULER-level gate for the periodic autonomous-provisioning
    cycle (allocate pending workspace nodes + poll hosted observations). DEFAULT OFF, and DISTINCT from the
    master ``HOSTED_PERSISTENT_MT5_ENABLED`` (which governs whether the cycle's drivers do any work) — two-
    level darkness: the ``run_hosted_observations`` command is a dormant no-op until this flag is on, and even
    then it only allocates/observes when the master flag is on. It NEVER arms execution or authorises an
    order; it only advances PROVISIONING→WAITING_FOR_LOGIN and ingests observations through the certified
    single writer. Kept independent so the scheduler can be enabled without touching delivery/execution gates."""
    return _flag("HOSTED_OBSERVATION_SCHEDULER_ENABLED")


def hosted_slot_prep_enabled() -> bool:
    """Beta Readiness Stream 4 — the gate for the host PROVISIONING engine (``prepare_hosted_slot``: create the
    Windows identity + folders + NTFS ACL + golden runtime + RDP + RemoteApp-verify + AppLocker AuditOnly for a
    hosted slot). DEFAULT OFF, and DISTINCT from the master ``HOSTED_PERSISTENT_MT5_ENABLED`` — two-level
    darkness: while this is off, node allocation advances PROVISIONING→WAITING_FOR_LOGIN exactly as before (no
    slot-prep gate); once on, allocation GATES that transition on a prepared slot. Even armed, the host-executor
    is a pluggable seam that is ``None`` in the repository-only phase, so every host step fails closed and no
    host is contacted. NEVER arms execution and NEVER performs a broker login (the engine stops before login)."""
    return _flag("HOSTED_SLOT_PREP_ENABLED")


def hosted_host_executor_enabled() -> bool:
    """Beta Readiness Stream 5 — the gate that ARMS the signed host-executor transport (the real backend↔host
    provisioning channel behind ``prepare_hosted_slot``). DEFAULT OFF. Even on, the executor still requires a
    configured keyring + base_url + envelope key or it stays dark (fail closed) — so this flag alone contacts no
    host. It grants ONLY the narrow allow-listed provisioning operations (no shell, no arbitrary command/path);
    it NEVER arms execution and NEVER performs a broker login. Arming is a Sponsor decision after host cert."""
    return _flag("HOSTED_HOST_EXECUTOR_ENABLED")


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
