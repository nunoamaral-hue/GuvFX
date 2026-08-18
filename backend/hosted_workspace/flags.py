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


def hosted_capability_recovery_enabled() -> bool:
    """AJ#6.3 Shape-3 — the gate for the post-login MT5 automation-CAPABILITY recovery edge (re-assert
    AllowLiveTrading=1 / Enabled=1 then gracefully relaunch THIS tenant's own terminal for a CONNECTED + matched
    workspace stuck at trade_allowed=False). DEFAULT OFF, and DISTINCT from the master
    ``HOSTED_PERSISTENT_MT5_ENABLED`` — two-level darkness: the recovery runner is a dormant no-op unless BOTH
    are on. It is capability-only: it re-writes the certified common.ini keys and relaunches the tenant's own
    MT5, bounded + loop-safe (never repeatedly restarts once recovered). It NEVER logs in, changes the broker
    account, ARMS execution, or authorises/places an order — arming still requires the explicit customer
    authorization (ADR-0047) and the live order-time bridge gate remains the sole order authority."""
    return _flag("HOSTED_CAPABILITY_RECOVERY_ENABLED")


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


def hosted_deferred_identity_bind_enabled() -> bool:
    """Beta UX Correction (Sponsor 2026-08-15) — the gate for DEFERRED broker-identity binding. DEFAULT OFF.

    While OFF, ``request_hosted_workspace`` keeps requiring ``expected_login`` up front and the write-once
    identity guard is inert — every path is byte-identical to the pre-change behaviour. While ON, the customer
    may request a Hosted Workspace WITHOUT a broker login/server, the runtime materialises (already broker-
    identity agnostic — it derives everything from ``account_id``) and reaches ``WAITING_FOR_LOGIN`` with the
    broker identity still UNBOUND, and the identity is later set exactly once through the explicit, owner-scoped
    ``provisioning.bind_broker_identity`` seam (an EXTERNAL declaration from the trusted customer/API — NEVER
    copied from the forgeable observation). It NEVER relaxes the order-time identity pin: an unbound account has
    an empty expected identity, so ``account_match`` is False (workspace holds at WAITING_FOR_LOGIN, account
    stays ``is_active=False``, no ExecutionJob) and the bridge fails closed ``identity_pin_required`` — no order
    can flow before the identity is bound AND the live login matches it. Independent of every execution gate."""
    return _flag("HOSTED_DEFERRED_IDENTITY_BIND_ENABLED")


def closed_beta_open_access_enabled() -> bool:
    """Beta UX Correction (Sponsor 2026-08-15) — the GLOBAL Closed-Beta open-access gate. DEFAULT OFF.

    The Closed Beta is controlled OPERATIONALLY by who is given access, NOT by an application-level per-email
    allowlist. While OFF, Hosted Workspace access requires the pre-existing per-user sources (a commercial
    entitlement OR a ``BetaTester`` admission row) — byte-identical to before. While ON, ANY authenticated user
    gains the Hosted Workspace CAPABILITY and Wayond arm authorization WITHOUT a prior ``admit_beta_tester`` /
    ``BetaTester`` / ``INTERNAL_PILOT_ARM_APPROVED_EMAILS`` entry — so a fresh unknown Closed-Beta registrant can
    complete the journey. This is an ACCESS/VISIBILITY + arm-cohort gate ONLY: it grants NO order authority
    (the live order-time bridge gate stays authoritative), it does NOT relax capacity limits, tenant/node
    isolation, DEMO-only, AUTO_LIVE-off, or the supervised posture, and — load-bearing — it NEVER re-authorizes
    Customer Zero: the CZ-owner exclusion in ``_admitted_beta_arm_authorized`` is applied on top of this gate,
    so a user who owns a reserved Customer-Zero account is still never arm-authorized."""
    return _flag("CLOSED_BETA_OPEN_ACCESS_ENABLED")


def hosted_order_bridge_auto_activate_enabled() -> bool:
    """FINAL Closed-Beta stream (Sponsor 2026-08-15) — autonomous per-node ORDER-BRIDGE activation. DEFAULT OFF.

    While OFF, ``prepare_hosted_slot`` behaves EXACTLY as before this stream: no bridge activation step, no
    ``order_bridge_base_url`` write, no host contact for the bridge — byte-identical (a beta node's bridge then
    stays a manual operational step). While ON, the materialise pipeline treats bridge activation as a REQUIRED,
    fail-closed host primitive (``activate_order_bridge``) in the same family as ``materialise_runtime`` /
    ``ensure_remoteapp``: it activates the node's dedicated pin-enforcing bridge, verifies its health, and
    persists the node's ``order_bridge_base_url`` BEFORE advancing to WAITING_FOR_LOGIN — so a fresh customer
    reaches a first DEMO trade with NO manual SSH/PowerShell/backend step. It grants NO order authority (the
    per-job identity pin + the order-time bridge gate stay authoritative), never touches Customer Zero (the
    reserved-account guard + the forbidden-node + never-overwrite-a-different-endpoint guards all fail closed),
    and does not relax DEMO-only / AUTO_LIVE-off / node isolation / the supervised posture. See
    ``hosted_workspace/slot_preparation.py`` (Stage 5c)."""
    return _flag("HOSTED_ORDER_BRIDGE_AUTO_ACTIVATE_ENABLED")


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


def hosted_delivery_lifecycle_enabled() -> bool:
    """BETA BLOCKER #1 corrective (Sponsor 2026-08-16) — complete the Hosted delivery LIFECYCLE. DEFAULT OFF.

    A single gate for the three additive, fail-closed lifecycle-completion edges that turn a provisioned-but-
    stuck workspace into one whose customer can actually open MetaTrader. While OFF, every one of them is
    byte-identical to before this corrective (no new host contact, no new delivery-state write, no new journey
    projection value), so Customer Zero and every existing workspace are unchanged. While ON:

    1. ``slot_preparation`` (Stage 10) promotes the observer registration from a best-effort DEFERRED step to a
       REQUIRED, stage-timed host primitive (``register_observer`` → signed ``PREPARE_OBSERVER``), so a fresh
       non-CZ hosted account receives its read-only session-bound observer AUTONOMOUSLY — no operator step.
    2. ``onboarding_read_model.delivery_readiness`` emits a NEW ``DELIVERY_DELIVERABLE`` state (distinct from
       ``CONNECTED``) once the delivery authority's preconditions hold, so the frontend can surface "Open
       MetaTrader" as soon as the workspace is authoritatively deliverable — BREAKING the button⇄CONNECTED
       circular dependency WITHOUT redefining CONNECTED (which stays "an actually established session").
    3. ``delivery_observe_runner`` drives the existing single delivery-state writer's
       ``record_remoteapp_connected`` / ``record_remoteapp_disconnected`` from the TRUSTED, tenant-unforgeable
       LocalSystem host-observation corroboration (process present + owner match + interactive session>0),
       never from RemoteApp publication alone and never from a client self-report.

    Customer Zero is excluded FOUR ways (the reserved-account guard in slot prep, the CZ refusal in the signed
    executor / host dispatch, the explicit non-CZ guard in the delivery-observe pass, and — as today — CZ's
    ``observe`` returning fail-closed). Grants NO order authority: the per-job identity pin + the order-time
    bridge gate stay authoritative, and delivery-state remains read-model-only (it can never place an order)."""
    return _flag("HOSTED_DELIVERY_LIFECYCLE_ENABLED")


def hosted_execution_path_gate_enabled() -> bool:
    """ADR-0048 — node EXECUTION-PATH allocation gate (DARK, default OFF). When OFF, node allocation
    (``provisioning.allocate_workspace_node``) behaves EXACTLY as before — the current beta journey
    allocates a node and commissions its execution path (bridge + dedicated worker) afterwards, and the
    read model stays honest via ``node_execution.execution_path_state`` (execution_path_ready=false with a
    reason until commissioned). When ON, allocation for a hosted automated-execution account REQUIRES an
    execution-operational node (``node_execution.node_execution_operational``) and FAILS CLOSED
    (``ALLOC_NODE_NOT_EXECUTION_OPERATIONAL``) if none exists — so once the operator has commissioned the
    fleet's nodes, an automated customer can never be allocated to a node that cannot claim its orders.
    Grants NO order authority and arms NO customer; the per-job pin + order-time bridge gate stay sole
    authority (ADR-0048 keeps NODE COMMISSIONING and CUSTOMER EXECUTION AUTHORIZATION distinct)."""
    return _flag("HOSTED_EXECUTION_PATH_GATE_ENABLED")
