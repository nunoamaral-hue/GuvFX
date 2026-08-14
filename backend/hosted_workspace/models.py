"""hosted_workspace.models — durable per-account Hosted Persistent MT5 Workspace record (ADR-0033).

A ``HostedMt5Workspace`` is the SIBLING of ``AccountRuntime`` (both OneToOne on
``trading.TradingAccount`` — the 'runtime is owned by the broker account' invariant), NOT an extension
of it. ``AccountRuntime`` models a GuvFX-OWNED, headless, GuvFX-provisioned beta runtime
(``guvfx_u_<id>``, GuvFX logs in). This model instead records a USER-OWNED persistent terminal that the
CUSTOMER logs into themselves, and which GuvFX only ATTACHES to (``initialize(path=...)``, never login)
to observe which broker account is currently active.

Following the ``BrokerRuntimePause`` precedent (a distinct per-account concern gets its own model
rather than overloading ``AccountRuntime.state``), this keeps the two very different lifecycles cleanly
separate and out of the integrity-pinned ``terminal_provisioning`` app.

Ships DARK: nothing in the execution / onboarding / delivery path reads or writes this model. This
increment adds NO such wiring — the model and the pure matcher (``matching.py``) are inert foundation.

SECURITY: this model stores NO credential. ``currently_attached_login`` / ``currently_attached_server``
are broker IDENTIFIERS (like ``TradingAccount.account_number`` / ``BrokerServer.server_name``), never
secrets; there is deliberately no password / accounts.dat / windows-password column. The whole point of
the attach model is that GuvFX never receives, stores, or transports the broker password.
"""
import uuid

from django.conf import settings
from django.db import models

from hosted_workspace.state_machine import WorkspaceLifecycleState, WorkspaceReason


class WorkspaceState(models.TextChoices):
    """User-owned attach lifecycle. Distinct from ``terminal_provisioning.RuntimeState`` (which assumes
    GuvFX-driven provisioning). 'process running' != 'broker connected' != 'safe to execute' — these
    are represented independently (state + observed_connected + active_account_match)."""
    NOT_PROVISIONED = "NOT_PROVISIONED", "Not provisioned"
    PROVISIONING = "PROVISIONING", "Provisioning"
    AWAITING_USER_LOGIN = "AWAITING_USER_LOGIN", "Awaiting user login"
    CONNECTED = "CONNECTED", "Connected"
    ACTIVE_ACCOUNT_MISMATCH = "ACTIVE_ACCOUNT_MISMATCH", "Active account mismatch"
    DISCONNECTED = "DISCONNECTED", "Disconnected"
    DEGRADED = "DEGRADED", "Degraded"
    STOPPED = "STOPPED", "Stopped"
    ERROR = "ERROR", "Error"


class HostedMt5Workspace(models.Model):
    class SupervisionState(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        SUPERVISED = "SUPERVISED", "Supervised"
        UNSUPERVISED = "UNSUPERVISED", "Unsupervised"

    class DeliveryState(models.TextChoices):
        """ADR-0034 Workspace Delivery — lifecycle of the RemoteApp delivery seam for this workspace.
        DISTINCT from the canonical execution lifecycle (``canonical_state``) and from the legacy attach
        ``state``: this tracks ONLY whether a delivery descriptor was minted and whether the customer's
        RemoteApp is currently connected to the persistent Windows session. Display/read-model only — it
        never gates order execution (that remains ``evaluate_binding`` in the bridge)."""
        NONE = "NONE", "Not delivered"              # no delivery descriptor ever minted
        AUTHORIZED = "AUTHORIZED", "Authorized"      # a signed delivery descriptor was minted for the owner
        CONNECTED = "CONNECTED", "Connected"         # RemoteApp reported connected to the persistent session
        DISCONNECTED = "DISCONNECTED", "Disconnected"  # RemoteApp disconnected; persistent session retained
        FAILED = "FAILED", "Failed"                  # authorization/derivation failed closed

    trading_account = models.OneToOneField(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="hosted_workspace")
    #: Immutable per-workspace identity (server-generated; never client-supplied).
    workspace_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    # Indexed via the named Meta.indexes entry below (not db_index=True) to avoid a duplicate index.
    state = models.CharField(max_length=32, choices=WorkspaceState.choices,
                             default=WorkspaceState.NOT_PROVISIONED)

    #: Portable-MT5 directory GuvFX attaches to (``initialize(path=...)``). Server-set only.
    attach_path = models.CharField(max_length=255, blank=True, default="")

    # Last OBSERVED active-account identity (broker identifiers, NOT secrets). Mutable — updated on each
    # attach observation; the customer may switch the active Navigator account at any time.
    currently_attached_login = models.CharField(max_length=64, blank=True, default="")
    currently_attached_server = models.CharField(max_length=128, blank=True, default="")

    # Last observation snapshot (a CACHE; authoritative truth is always a fresh attach). Null = unknown.
    observed_connected = models.BooleanField(null=True, blank=True)
    observed_trade_allowed = models.BooleanField(null=True, blank=True)
    observed_is_demo = models.BooleanField(null=True, blank=True)
    #: Result of the last ``evaluate_active_account_match`` — display/readiness ONLY, NOT the order gate.
    active_account_match = models.BooleanField(null=True, blank=True)
    last_reason = models.CharField(max_length=64, blank=True, default="")  # stable, secret-free code

    supervision_state = models.CharField(max_length=16, choices=SupervisionState.choices,
                                         default=SupervisionState.UNKNOWN)
    remoteapp_ready = models.BooleanField(default=False)

    # --- ADR-0034 Workspace Delivery: RemoteApp delivery state (DARK, read-model only) --------------------
    # The execution HOST this workspace's persistent Windows session / RemoteApp is delivered from. Server-
    # assigned only; the delivery seam DERIVES the RDP host from this node (never from the client). Nullable:
    # a workspace with no assigned node cannot be delivered (fail-closed). SET_NULL so retiring a node never
    # cascades away workspaces. Written ONLY by ``delivery_persistence`` (the single delivery-state writer).
    workspace_node = models.ForeignKey(
        "execution.TerminalNode", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="hosted_workspaces")
    delivery_state = models.CharField(max_length=16, choices=DeliveryState.choices,
                                      default=DeliveryState.NONE)
    delivery_reason = models.CharField(max_length=64, blank=True, default="")  # stable, secret-free code
    # Monotonic per-workspace RemoteApp connect/disconnect event sequence LAST APPLIED (mirrors the M3c
    # writer's ``observation_version``). Staleness key: a reordered/replayed connect/disconnect whose seq is
    # ``<=`` this is rejected, so "last-actual" wins rather than "last-arrived", and telemetry dedups on it.
    delivery_event_seq = models.PositiveBigIntegerField(default=0)
    last_delivery_attempt = models.DateTimeField(null=True, blank=True)
    last_delivery_success = models.DateTimeField(null=True, blank=True)
    # Delivery-OWNED correlation of the last delivery action. DISTINCT from ``last_correlation_id`` (which the
    # certified M3c single writer ``persist_workspace_decision`` owns for canonical decisions): the delivery
    # writer must NEVER stamp the canonical field (single-writer boundary — ADR-0034 Workspace Delivery §N).
    last_delivery_correlation_id = models.CharField(max_length=128, blank=True, default="")

    last_observed_at = models.DateTimeField(null=True, blank=True)
    last_switch_at = models.DateTimeField(null=True, blank=True)
    attach_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- ADR-0034 / M3c: authoritative canonical persisted state (ADR-0034 §3/§4) ---------------------
    # The M2a canonical lifecycle is the state every hosted-workspace subsystem consumes. It is DISTINCT
    # from the legacy ``state`` (WorkspaceState) above, which is the inert ADR-0033 foundation and is left
    # untouched by the M3c writer. Written EXCLUSIVELY by ``persistence.persist_workspace_decision`` (the
    # single authoritative state writer); no other code path mutates these fields. Still display/read-model
    # only — the order-time authority remains ``evaluate_binding`` in the bridge.
    canonical_state = models.CharField(
        max_length=32, choices=WorkspaceLifecycleState.choices,
        default=WorkspaceLifecycleState.PROVISIONING)
    canonical_reason = models.CharField(
        max_length=32, choices=WorkspaceReason.choices, default=WorkspaceReason.NONE)
    # Caller-supplied strictly-increasing per-workspace observation sequence LAST APPLIED. Stale-observation
    # protection: the writer rejects any observation whose version is <= this. 0 = never observed.
    observation_version = models.PositiveBigIntegerField(default=0)
    # Monotonic count of MATERIAL decisions applied (== number of WorkspaceTransition rows). Incremented by
    # exactly one per accepted material decision; never decreases. Used as the OperationalEvent state_version.
    decision_version = models.PositiveBigIntegerField(default=0)
    last_decision_at = models.DateTimeField(null=True, blank=True)  # any accepted decision (incl. no-op)
    last_transition_at = models.DateTimeField(null=True, blank=True)  # canonical-state change only
    # Latest-observation health projection (a derived CACHE of the last applied observation; null = unknown).
    # NOT the order gate — parity with the ``observed_*`` legacy cache but keyed to the canonical writer.
    proj_process_running = models.BooleanField(null=True, blank=True)
    proj_ipc_available = models.BooleanField(null=True, blank=True)
    proj_connected = models.BooleanField(null=True, blank=True)
    proj_account_match = models.BooleanField(null=True, blank=True)
    proj_trade_allowed = models.BooleanField(null=True, blank=True)
    proj_execution_ready = models.BooleanField(null=True, blank=True)
    last_correlation_id = models.CharField(max_length=128, blank=True, default="")

    # --- ADR-0034 Execution Engine: explicit per-workspace ARM (Decision D, condition 4) ---------------
    # The durable, per-workspace switch that must be True before this workspace may execute. DEFAULT FALSE.
    # No migration ever sets it True; nothing auto-arms. It is one AND-term among the layered arming gate
    # (global flag + execution feature flag + provider + this + canonical state + demo-only + the LIVE
    # order-time gates) — never sufficient on its own, and NEVER the order authority.
    execution_enabled = models.BooleanField(default=False)

    # --- ADR-0044: durable operator-disarm intent (reversibility / "disarm still wins") -------------------
    # Set True by ``disarm_hosted_workspace_execution`` and cleared ONLY by an explicit
    # ``arm_hosted_workspace_execution``. The autonomous ``auto_arm_runner`` EXCLUDES suppressed workspaces, so a
    # deliberate operator disarm is never silently reverted by the next cron cycle. Defaults False (a fresh
    # workspace is auto-armable once it legitimately reaches EXECUTION_READY). Never the order authority.
    auto_arm_suppressed = models.BooleanField(default=False)

    # --- ADR-0034 Execution Engine capstone (PART 2/3): durable workspace->node execution binding ---------
    # The ONE authorised execution TerminalNode this workspace resolves to (Decision C). NULL ⇒ NOT
    # execution-routable (fail-closed). Server-assigned only, via the provisioning contract
    # (hosted_provisioning.assign_workspace_execution_node); never client-supplied, never set by a migration.
    # ``execution_binding_generation`` versions the binding (increments by one on each (re)assignment) so a
    # reassignment is explicit + auditable while DARK. SET_NULL so retiring a node never cascades a workspace.
    # It must AGREE with the account's terminal_node (resolve_hosted_route enforces workspace==account==job
    # node); it is routing/authority CONTEXT only — never the order-time gate (the live bridge remains sole).
    execution_node = models.ForeignKey(
        "execution.TerminalNode", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="bound_hosted_workspaces")
    execution_binding_generation = models.PositiveIntegerField(default=0)

    # --- ADR-0034 Onboarding: owning user -------------------------------------------------------------
    # There is NO separate ``owner`` FK. Ownership is the ONE immutable fact ``trading_account.user`` (the
    # OneToOne workspace<->account binding is immutable, so the owner cannot drift). Both onboarding and the
    # delivery authority derive the owner from ``trading_account.user`` — a single source of truth, no
    # duplicated durable relationship to keep in sync (ADR-0034 §9 / Onboarding simplification review).

    _IMMUTABLE_BINDING = ("workspace_uuid", "trading_account_id")

    class Meta:
        indexes = [
            models.Index(fields=["state"], name="hostedws_state_idx"),
            models.Index(fields=["canonical_state"], name="hostedws_canon_state_idx"),
        ]

    def save(self, *args, **kwargs):
        """Enforce the immutable workspace-uuid / trading-account binding after creation (mirrors
        ``AccountRuntime.save``). The guard's extra fetch runs only when a bound field is touched. Ownership is
        NOT a separate field — it is ``trading_account.user``, made immutable by this same binding guard."""
        if not self._state.adding:
            uf = kwargs.get("update_fields")
            touches_binding = uf is None or any(
                f in uf for f in ("workspace_uuid", "trading_account", "trading_account_id"))
            if touches_binding:
                old = type(self).objects.filter(pk=self.pk).values(
                    "workspace_uuid", "trading_account_id").first()
                if old and (str(old["workspace_uuid"]) != str(self.workspace_uuid)
                            or old["trading_account_id"] != self.trading_account_id):
                    raise ValueError(
                        "HostedMt5Workspace workspace_uuid/owner binding is immutable after creation")
        super().save(*args, **kwargs)

    @property
    def is_execution_ready(self) -> bool:
        """Display / readiness signal ONLY — True iff the last observation left the workspace CONNECTED
        with a positive active-account match. This is NOT the order-time safety boundary: the
        authoritative gate before every ``order_send`` remains ``evaluate_binding`` in the bridge
        (ADR-0033). A stale readiness flag can never authorise an order on its own."""
        return self.state == WorkspaceState.CONNECTED and self.active_account_match is True

    def contract(self) -> dict:
        """Stable, secret-free snapshot for readiness / observability consumers. The active login is
        MASKED (never emit a full broker login) — parity with ``login_masked`` / agent_status_presenter."""
        login = self.currently_attached_login or ""
        return {
            "account_id": self.trading_account_id,
            "workspace_uuid": str(self.workspace_uuid),
            "state": self.state,
            "active_account_match": self.active_account_match,
            "active_login_masked": ("***" + login[-3:]) if login else "",
            "server": self.currently_attached_server or "",
            "is_execution_ready": self.is_execution_ready,
            "supervision_state": self.supervision_state,
            "remoteapp_ready": self.remoteapp_ready,
            "last_reason": self.last_reason,
            "last_observed_at": self.last_observed_at.isoformat() if self.last_observed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @property
    def canonical_execution_ready(self) -> bool:
        """Read-model execution-readiness derived from the CANONICAL M3c state (not the legacy
        ``is_execution_ready``). True iff the last applied decision left the workspace EXECUTION_READY.
        Display/read-model ONLY — never the order-time gate (that remains ``evaluate_binding``)."""
        return self.canonical_state == WorkspaceLifecycleState.EXECUTION_READY

    def __str__(self):
        return f"HostedMt5Workspace(acct={self.trading_account_id}, {self.state}/{self.canonical_state})"


class WorkspaceTransition(models.Model):
    """ADR-0034 / M3c — append-only provenance of every MATERIAL canonical-state decision applied by the
    single authoritative writer (``persistence.persist_workspace_decision``).

    One row per material change (canonical-state change, reason change, or execution-readiness change). It
    is the audit trail behind ``HostedMt5Workspace.canonical_state`` AND the idempotency/dedupe source for
    the matching ``workspace.*`` operational event: ``dedupe_key`` is reused verbatim as the event's
    ``dedup_key``, so a replayed observation can never double-append a transition OR double-emit an event.

    Immutable by contract: rows are only ever created, never updated (no field mutation path exists). Carries
    NO credential — ``from_state``/``to_state``/``reason`` are canonical enum values; identifiers only.
    """
    workspace = models.ForeignKey(
        HostedMt5Workspace, on_delete=models.CASCADE, related_name="transitions")
    from_state = models.CharField(max_length=32)
    to_state = models.CharField(max_length=32)
    reason = models.CharField(max_length=32, blank=True, default="")
    # The observation sequence + resulting decision sequence that produced this transition (provenance).
    observation_version = models.PositiveBigIntegerField()
    decision_version = models.PositiveBigIntegerField()
    state_changed = models.BooleanField(default=False)  # canonical_state actually moved
    execution_ready_changed = models.BooleanField(default=False)
    telemetry_event = models.CharField(max_length=64, blank=True, default="")  # emitted workspace.* type
    source = models.CharField(max_length=64, blank=True, default="")
    correlation_id = models.CharField(max_length=128, blank=True, default="")
    # Idempotency handle: unique per (workspace, observation_version, to_state, reason). A replay collides.
    dedupe_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="hostedws_tr_ws_created_idx"),
            models.Index(fields=["correlation_id"], name="hostedws_tr_corr_idx"),
        ]

    def __str__(self):
        return (f"WorkspaceTransition(ws={self.workspace_id}, {self.from_state}->{self.to_state}, "
                f"obs={self.observation_version}, dec={self.decision_version})")
