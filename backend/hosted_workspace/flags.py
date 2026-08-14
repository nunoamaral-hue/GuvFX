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


def hosted_remoteapp_isolation_certified() -> bool:
    """STREAM 9E trust-model prerequisite (ADR-0041) — asserts that the host's RemoteApp / AppLocker isolation
    has been BEHAVIOURALLY certified, i.e. a hosted tenant CANNOT execute arbitrary code inside its own session.

    This is the ROOT trust anchor for Hosted Workspace observation, not a convenience toggle. The observer runs
    AS the tenant (the only context that can reach the session-bound MT5 IPC), so the login / IPC / trade_allowed
    facts it reports are only as trustworthy as the tenant. LocalSystem corroborates the OBJECTIVE host facts
    (process/owner/session/runtime/connectivity) but physically CANNOT corroborate MT5 IPC state. Therefore an
    observation is trustworthy ONLY when the tenant cannot forge the handoff — which is exactly what RemoteApp
    isolation guarantees. The certification dependency is: REMOTEAPP_ISOLATION_CERTIFIED -> HOSTED_OBSERVATION
    -> WORKSPACE_READY -> AUTONOMOUS_ONBOARDING. DEFAULT OFF.

    NO-FAKE-READY: this flag must be set ONLY after the behavioural escape-attempt certification actually passed
    on the target host. Setting it without that certification re-opens the forgeable-handoff residual (a
    code-executing tenant advancing its OWN account's readiness display). Execution integrity is NOT affected
    either way — the certified order-time runtime-identity validation (Execution Engine) is independent."""
    return _flag("HOSTED_REMOTEAPP_ISOLATION_CERTIFIED")


def hosted_mt5_observation_enabled() -> bool:
    """STREAM 9E — the gate that ARMS the live host observation transport: the real backend↔host read-only
    ``OBSERVE_WORKSPACE`` channel that triggers the per-account session-bound observer and feeds its snapshot
    through the certified producer→consumer→state-machine chain. DEFAULT OFF. Observation is CAPABILITY ONLY:
    this flag NEVER arms execution, NEVER provisions a slot, NEVER opens onboarding, and NEVER changes broker
    state — it only lets ``run_hosted_observations`` obtain a REAL snapshot instead of the dark placeholder.
    Even on, the transport still requires the configured signed executor (``hosted_host_executor_enabled`` +
    keyring/base_url), so this flag alone contacts no host (fail closed). Kept independent of every other gate.

    TRUST-MODEL PREREQUISITE (ADR-0041): a live observation is only TRUSTED — and the driver only produces one —
    when ``hosted_remoteapp_isolation_certified()`` also holds. See ``live_observe.live_observe_fn``."""
    return _flag("HOSTED_MT5_OBSERVATION_ENABLED")


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


def hosted_wx_isolation_enabled() -> bool:
    """STREAM 10D (ADR-0043) — the gate for the PER-TENANT, SLOT-PROVISIONING half of the Hosted Workspace W^X
    (write-xor-execute) native-code-elimination model: the G5v2 inverted ACL (root Read+Execute; Modify only on
    the enumerated data subdirs; common.ini + code dirs tenant-deny-write) + the per-tenant AppLocker execute-Deny
    fragments (the positive Deny(*)-with-exec-allowlist). DEFAULT OFF. It is the repository gate for the canonical
    invariant ``TENANT-WRITABLE => NON-EXECUTABLE`` / ``TENANT-EXECUTABLE => NON-WRITABLE``; while off,
    slot_preparation uses the certified G5v1 ACL and composes no per-tenant W^X denies (per-tenant behaviour
    unchanged). It NEVER arms execution, NEVER performs a broker login, and NEVER contacts a host on its own (the
    host-executor seam stays None -> fail closed). DISTINCT from every execution/observation gate.

    NOT gated by this flag (deliberately): the MetaEditor ``BinaryName`` pin lives in the MACHINE-WIDE BASE allow
    model (``applocker_policy.generate_base_policy`` + the committed ``guvfx-hosted-{auditonly,enforce}.xml``
    templates), because it is a deny-by-default TIGHTENING consistent with ADR-0042 (it only DENIES metaeditor64
    and every non-terminal64 MetaQuotes tool — it opens nothing). It therefore ships whenever the base policy is
    (re)deployed via ``Set-GuvfxAppLocker.ps1``, independent of this flag. RULE-11 pre-Enforce control: an
    Enabled-mode base redeploy must first prove on-host that terminal64.exe's EMBEDDED signature BinaryName equals
    the pinned literal (a mismatch would deny terminal64 itself); until then the pin is exercised in AuditOnly only.
    See ADR-0043 + docs/operations/hosted-workspace/APPLOCKER_HARDENING.md.

    NO-FAKE-READY: turning this on tightens isolation, but ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` stays a
    SEPARATE behavioural marker that may be emitted ONLY after the on-host W^X escape battery (portable-copy,
    MetaEditor, common.ini mutation, #import, writable EXE/DLL/Script, signed-DLL COM-hijack, restart persistence)
    passes on a disposable hosted tenant with Customer Zero preserved (ADR-0043)."""
    return _flag("HOSTED_WX_ISOLATION_ENABLED")


def supervised_single_tenant_beta_enabled() -> bool:
    """ADR-0044 — the SUPERVISED_SINGLE_TENANT_BETA operational posture (DARK, default OFF).

    An explicitly bounded interim gate (Sponsor decision 2026-08-14) that permits the FIRST end-to-end product
    validation to advance a Hosted Workspace to EXECUTION_READY *without* ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED``
    — but ONLY under the fail-closed boundary enforced in ``supervised_beta.supervised_single_tenant_beta_active``:
    one non-Customer-Zero DEMO tenant, alone on a dedicated ACTIVE non-Customer-Zero node. It is NOT the full
    behavioural certification and it emits NO certification marker: it is a coarse operational carve-out that
    bounds the (still un-certified) forgeable-observation risk to a single supervised disposable tenant.

    Same production code paths in both postures — only this gate differs — so when the full cert lands and
    ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` is set, this flag is simply turned OFF and the posture dissolves
    with no architectural change. NEVER arms execution by itself, performs no broker login, contacts no host.

    NO-FAKE-READY: turning this on does NOT set or imply the isolation cert; the two are independent flags, and
    ``live_observe.live_observe_fn`` reads them as an OR only after enforcing the single-tenant boundary."""
    return _flag("SUPERVISED_SINGLE_TENANT_BETA_ENABLED")


def hosted_tenant_node_isolation_enabled() -> bool:
    """ADR-0043 Addendum B — host-level CO-RESIDENCY guard (DARK, default OFF). When on, the node allocator
    (``provisioning.allocate_workspace_node``) and the execution-node single writer
    (``execution.hosted_provisioning.assign_workspace_execution_node``) fail closed rather than bind a
    NON-Customer-Zero hosted workspace to a ``TerminalNode`` that serves Customer Zero (or an rdp_host listed
    in ``settings.HOSTED_BETA_FORBIDDEN_RDP_HOSTS``). It is the COARSE-GRAINED complement to the in-host W^X
    model (``HOSTED_WX_ISOLATION_ENABLED``): W^X isolates tenants that SHARE one host; this keeps beta tenants
    off Customer Zero's PHYSICAL host entirely while ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` is still
    outstanding, bounding the blast radius of any un-certified escape to disposable beta tenants on a throwaway
    host. OFF = the allocator behaves exactly as before (zero behaviour change). It NEVER arms execution,
    performs a broker login, or contacts a host. See ``hosted_workspace/tenant_isolation.py`` + ADR-0043."""
    return _flag("HOSTED_TENANT_NODE_ISOLATION_ENABLED")
