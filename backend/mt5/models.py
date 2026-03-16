from django.conf import settings
from django.db import models

class Mt5Credential(models.Model):
    STATUS_CHOICES = [
        ("NEVER", "NEVER"),
        ("PENDING", "PENDING"),
        ("SUCCESS", "SUCCESS"),
        ("FAILED", "FAILED"),
        ("TIMEOUT", "TIMEOUT"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mt5_credential")
    login = models.CharField(max_length=64)
    server = models.CharField(max_length=128)
    password_enc = models.TextField()

    last_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="NEVER")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

from django.conf import settings
from django.db import models
from django.utils import timezone

class Mt5Instance(models.Model):
    PLATFORM_CHOICES = [
        ("LINUX", "LINUX"),
        ("WINDOWS", "WINDOWS"),
    ]

    hostname = models.CharField(max_length=128, unique=True)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default="LINUX")
    is_admin = models.BooleanField(default=False)

    # Windows RDP target
    rdp_host = models.CharField(max_length=64, blank=True, default="")  # e.g. 10.50.0.2
    guac_connection_id = models.IntegerField(null=True, blank=True)
    windows_username = models.CharField(max_length=64, blank=True, default="")
    windows_password_enc = models.TextField(blank=True, default="")

    is_leased = models.BooleanField(default=False)
    leased_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mt5_leases",
    )
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)


# =========================================================================
# Packet A — Terminal Interaction Models
# =========================================================================


class TerminalBinding(models.Model):
    """
    Binds an MT5 account login to a TerminalNode slot.

    Represents a specific terminal installation/slot on a node that can
    be occupied by an InteractionSession.
    """

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        LAUNCHING = "launching", "Launching"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        MAINTENANCE = "maintenance", "Maintenance"
        LOCKED = "locked", "Locked"

    terminal_node = models.ForeignKey(
        "execution.TerminalNode",
        on_delete=models.CASCADE,
        related_name="terminal_bindings",
    )
    terminal_identifier = models.CharField(
        max_length=255,
        help_text="Unique identifier for this terminal slot on the node.",
    )
    mt5_account_login = models.CharField(
        max_length=64,
        help_text="MT5 account login number bound to this terminal.",
    )
    environment_type = models.CharField(
        max_length=32,
        help_text="Environment type, e.g. 'demo', 'live'.",
    )
    terminal_label = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Human-friendly label for this terminal binding.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )
    occupied_by_session = models.ForeignKey(
        "mt5.InteractionSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="occupied_bindings",
        help_text="The InteractionSession currently occupying this binding.",
    )
    occupied_since = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    supports_shared_view = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["terminal_node", "terminal_identifier"]
        verbose_name = "Terminal Binding"
        verbose_name_plural = "Terminal Bindings"
        constraints = [
            models.UniqueConstraint(
                fields=["terminal_node", "terminal_identifier"],
                name="uniq_binding_per_node_identifier",
            ),
        ]
        indexes = [
            models.Index(
                fields=["terminal_node", "status"],
                name="idx_binding_node_status",
            ),
        ]

    def __str__(self) -> str:
        label = self.terminal_label or self.terminal_identifier
        return f"{label} ({self.status})"


class UserToTerminalAuthorization(models.Model):
    """
    Grants a user access to a specific TerminalBinding with scoped
    capabilities (launch, resume, manual trade, chart interaction).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="terminal_authorizations",
    )
    terminal_binding = models.ForeignKey(
        TerminalBinding,
        on_delete=models.CASCADE,
        related_name="authorizations",
    )
    access_mode = models.CharField(
        max_length=32,
        help_text="Access mode, e.g. 'full', 'view_only', 'trade_only'.",
    )

    can_launch = models.BooleanField(default=False)
    can_resume = models.BooleanField(default=False)
    can_manual_trade = models.BooleanField(default=False)
    can_chart_interact = models.BooleanField(default=False)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="granted_terminal_authorizations",
    )
    granted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "User-to-Terminal Authorization"
        verbose_name_plural = "User-to-Terminal Authorizations"
        indexes = [
            models.Index(
                fields=["user", "terminal_binding"],
                name="idx_auth_user_binding",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Auth: user {self.user_id} -> binding {self.terminal_binding_id} "
            f"({self.access_mode})"
        )


class InteractionSession(models.Model):
    """
    Represents a user's interaction session with a terminal binding.

    Lifecycle: requested -> authorized -> started -> ended.
    """

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested"
        AUTHORIZED = "authorized", "Authorized"
        STARTING = "starting", "Starting"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        ENDED = "ended", "Ended"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="interaction_sessions",
    )
    terminal_binding = models.ForeignKey(
        TerminalBinding,
        on_delete=models.CASCADE,
        related_name="interaction_sessions",
    )
    authorization = models.ForeignKey(
        UserToTerminalAuthorization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="interaction_sessions",
    )
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.REQUESTED,
        db_index=True,
    )

    requested_at = models.DateTimeField(null=True, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    terminated_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Interaction Session"
        verbose_name_plural = "Interaction Sessions"
        indexes = [
            models.Index(
                fields=["user", "state"],
                name="idx_isession_user_state",
            ),
            models.Index(
                fields=["terminal_binding", "state"],
                name="idx_isession_binding_state",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Session {self.pk}: user {self.user_id} "
            f"binding {self.terminal_binding_id} ({self.state})"
        )


class MT5Session(models.Model):
    """
    Represents a concrete MT5 terminal process session within an
    InteractionSession.  Tracks adapter connectivity and heartbeat.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        LAUNCHING = "launching", "Launching"
        CONNECTED = "connected", "Connected"
        SUSPENDED = "suspended", "Suspended"
        ENDED = "ended", "Ended"
        FAILED = "failed", "Failed"

    interaction_session = models.ForeignKey(
        InteractionSession,
        on_delete=models.CASCADE,
        related_name="mt5_sessions",
    )
    terminal_binding = models.ForeignKey(
        TerminalBinding,
        on_delete=models.CASCADE,
        related_name="mt5_sessions",
    )
    adapter_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Adapter implementation type, e.g. 'guacamole_rdp', 'direct_wine'.",
    )
    adapter_session_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Adapter-assigned session identifier.",
    )
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )

    launch_issued_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)

    launch_descriptor_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Snapshot of launch parameters at session creation time.",
    )
    adapter_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Adapter-specific metadata (connection params, etc.).",
    )
    failure_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "MT5 Session"
        verbose_name_plural = "MT5 Sessions"
        indexes = [
            models.Index(
                fields=["interaction_session", "state"],
                name="idx_mt5session_isession_state",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"MT5Session {self.pk}: binding {self.terminal_binding_id} "
            f"({self.state})"
        )


class TerminalInteractionAudit(models.Model):
    """
    Append-only audit trail for terminal interaction lifecycle events.

    Captures state transitions, actor identity, and before/after snapshots.
    """

    interaction_session = models.ForeignKey(
        InteractionSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    mt5_session = models.ForeignKey(
        MT5Session,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="terminal_audit_entries",
    )
    action_type = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Action that occurred, e.g. 'session_started', 'mt5_connected'.",
    )
    before_state = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    after_state = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
    )
    timestamp = models.DateTimeField(
        db_index=True,
        help_text="When the audited action occurred.",
    )

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Terminal Interaction Audit"
        verbose_name_plural = "Terminal Interaction Audit Entries"
        indexes = [
            models.Index(
                fields=["interaction_session", "timestamp"],
                name="idx_tia_isession_ts",
            ),
            models.Index(
                fields=["action_type", "timestamp"],
                name="idx_tia_action_ts",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"[{self.action_type}] session={self.interaction_session_id} "
            f"at {self.timestamp}"
        )
