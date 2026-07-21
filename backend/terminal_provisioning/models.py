"""
TX-1A / TX-1B — Terminal Isolation Provisioning models.

System of record for the per-account isolation foundation:
  TradingAccount  →  dedicated Windows identity (guvfx_u_<id>)
                  →  dedicated MT5 runtime (C:\\GuvFX\\accounts\\<id>\\)

ADDITIVE ONLY. This does not alter trading, execution, reliability, Guacamole,
VNC, or the legacy shared-Administrator runtime. It records and controls the
isolation profile for an account; Windows materialisation is performed by an
idempotent provisioning script driven from this record.

Secrets: the generated Windows password is stored Fernet-encrypted via the
existing trading.crypto helper and is NEVER exposed through the API/UI/audit.
"""
import uuid

from django.db import models


class AccountProvisioning(models.Model):
    """One isolation profile per TradingAccount (identity + runtime + mapping)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"          # record exists, not yet materialised on host
        PROVISIONED = "PROVISIONED", "Provisioned"
        DISABLED = "DISABLED", "Disabled"
        RETIRED = "RETIRED", "Retired"

    # ── Mapping anchor: deterministic, one-to-one with the trading account ──
    trading_account = models.OneToOneField(
        "trading.TradingAccount",
        on_delete=models.PROTECT,
        related_name="isolation_profile",
    )

    # ── Dedicated identity (TX1-R1 / TX1-R4) ──
    windows_username = models.CharField(max_length=64, unique=True)
    password_enc = models.TextField(blank=True, default="")  # Fernet; never exposed
    is_admin = models.BooleanField(
        default=False,
        help_text="MUST remain False — customer identities are non-administrator.",
    )

    # ── Dedicated runtime (TX1-R2) ──
    runtime_root = models.CharField(max_length=255, unique=True)
    runtime_structure = models.JSONField(
        default=dict,
        help_text="Subdirectory map, e.g. {'terminal': '...\\\\terminal', ...}.",
    )

    # ── Lifecycle (TX1-R6 auditable lifecycle) ──
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    # Materialisation flags — set True once the Windows host actually reflects it.
    identity_materialized = models.BooleanField(default=False)
    runtime_materialized = models.BooleanField(default=False)
    # TX-1D: viewer MT5 binaries placed into <runtime>\terminal\ (view-only).
    runtime_populated = models.BooleanField(default=False)
    runtime_version = models.CharField(max_length=32, blank=True, default="")

    provisioning_version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Account Provisioning (TX-1)"
        verbose_name_plural = "Account Provisioning (TX-1)"
        constraints = [
            models.UniqueConstraint(fields=["windows_username"], name="tx1_uniq_windows_username"),
            models.UniqueConstraint(fields=["runtime_root"], name="tx1_uniq_runtime_root"),
        ]

    def __str__(self):
        return f"acct={self.trading_account_id} {self.windows_username} [{self.status}]"


class SessionAssignment(models.Model):
    """
    TX-1C — customer-session routing record.

    Binds  account → identity → runtime  (resolving through AccountProvisioning)
    and records whether the account is ELIGIBLE and ENABLED for future customer
    session routing, plus a deterministic readiness verdict.

    DORMANT INFRASTRUCTURE: nothing in the live Guacamole/VNC launch path reads
    this model yet. Customer traffic continues on the legacy Administrator path
    until a later cutover phase wires routing in. Additive only.
    """

    class Readiness(models.TextChoices):
        READY = "READY", "Ready"
        NOT_READY = "NOT_READY", "Not Ready"
        INVALID = "INVALID", "Invalid"

    trading_account = models.OneToOneField(
        "trading.TradingAccount",
        on_delete=models.PROTECT,
        related_name="session_assignment",
    )
    provisioning = models.ForeignKey(
        AccountProvisioning,
        on_delete=models.PROTECT,
        related_name="session_assignments",
    )
    # Snapshots for routing recovery / queryability (source of truth = provisioning).
    windows_username = models.CharField(max_length=64)
    runtime_root = models.CharField(max_length=255)

    eligible = models.BooleanField(
        default=False, help_text="Eligible for FUTURE customer assignment (not a cutover)."
    )
    enabled = models.BooleanField(
        default=False, help_text="Routing switch — dormant; live path does not read this yet."
    )

    readiness = models.CharField(
        max_length=16, choices=Readiness.choices, default=Readiness.NOT_READY, db_index=True
    )
    readiness_detail = models.JSONField(default=dict)
    last_readiness_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Session Assignment (TX-1C)"
        verbose_name_plural = "Session Assignments (TX-1C)"

    def __str__(self):
        return f"route acct={self.trading_account_id} -> {self.windows_username} [{self.readiness} enabled={self.enabled}]"


class RuntimeState(models.TextChoices):
    """Option A §8 provisioning state machine (durable). See BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A."""
    NOT_PROVISIONED = "NOT_PROVISIONED", "Not provisioned"
    QUEUED = "QUEUED", "Queued"
    BLOCKED = "BLOCKED", "Blocked"
    PROVISIONING = "PROVISIONING", "Provisioning"
    STARTING = "STARTING", "Starting"
    AUTHENTICATING = "AUTHENTICATING", "Authenticating"
    RUNNING = "RUNNING", "Running"
    DEGRADED = "DEGRADED", "Degraded"
    REPAIRING = "REPAIRING", "Repairing"
    STOPPING = "STOPPING", "Stopping"
    STOPPED = "STOPPED", "Stopped"
    DEPROVISIONING = "DEPROVISIONING", "Deprovisioning"
    REMOVED = "REMOVED", "Removed"
    FAILED = "FAILED", "Failed"


class AccountRuntime(models.Model):
    """GFX-BETA-PHASE0 Increment 2 — durable per-account provisioning-state machine (Option A §8).

    **1:1 owned by TradingAccount** (the MT5 runtime is owned by the broker account, never by a
    strategy or a session). The user-facing state is derived from THIS durable record, NEVER inferred
    solely from a transient live process/health check.

    Phase-0 boundary: this records STATE only. It does NOT perform provisioning (that is the
    architecture-dependent Phase-2 provisioner). Until that is deployed every runtime stays
    ``NOT_PROVISIONED`` — so nothing may imply an MT5 terminal exists or is connected.
    """

    class Cohort(models.TextChoices):
        PRODUCTION = "PRODUCTION", "Production"   # Nuno's existing runtimes — EXCLUDED from beta caps/logic
        BETA = "BETA", "Beta"                     # controlled co-hosted headless beta pool

    trading_account = models.OneToOneField(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="runtime")
    state = models.CharField(max_length=20, choices=RuntimeState.choices,
                             default=RuntimeState.NOT_PROVISIONED)
    attempt = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")  # sanitised, user-safe (no raw agent strings)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ── GFX-BETA-HEADLESS Increment 1 — ownership + runtime-layout fields (additive; PRODUCTION default
    #    keeps every existing/Nuno runtime out of the beta caps and beta code paths). ──
    #: Immutable per-runtime identity (compensating control 3/4). Server-generated; never client-supplied.
    runtime_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    cohort = models.CharField(max_length=16, choices=Cohort.choices,
                              default=Cohort.PRODUCTION, db_index=True)
    #: Canonical portable-MT5 directory (control 1/4). Set ONLY by the server-side path generator.
    runtime_root = models.CharField(max_length=255, blank=True, default="")
    runtime_version = models.CharField(max_length=32, blank=True, default="")
    credential_version = models.PositiveIntegerField(default=0)
    #: Dedicated bridge-routing identity (control 6). Requests carrying anything else fail closed.
    bridge_identity = models.CharField(max_length=64, blank=True, default="", db_index=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_failure_reason = models.CharField(max_length=64, blank=True, default="")  # sanitised code only
    #: Per-runtime quarantine (control 17) — a quarantined runtime is not re-provisioned until cleared.
    quarantined = models.BooleanField(default=False, db_index=True)
    quarantine_reason = models.CharField(max_length=64, blank=True, default="")

    _IMMUTABLE_BINDING = ("runtime_uuid", "cohort", "trading_account_id")

    def save(self, *args, **kwargs):
        """Enforce the immutable owner/UUID/cohort binding (control 3). The hot-path
        ``record_transition`` uses ``update_fields`` that never touch these, so the guard's
        extra fetch runs only on a full save or an explicit attempt to change a bound field."""
        if not self._state.adding:
            uf = kwargs.get("update_fields")
            touches_binding = uf is None or any(
                f in uf for f in ("runtime_uuid", "cohort", "trading_account", "trading_account_id"))
            if touches_binding:
                old = type(self).objects.filter(pk=self.pk).values(
                    "runtime_uuid", "cohort", "trading_account_id").first()
                if old and (str(old["runtime_uuid"]) != str(self.runtime_uuid)
                            or old["cohort"] != self.cohort
                            or old["trading_account_id"] != self.trading_account_id):
                    raise ValueError(
                        "AccountRuntime owner/UUID/cohort binding is immutable after creation")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"AccountRuntime(acct={self.trading_account_id}, {self.cohort}, {self.state})"


class RuntimeEvent(models.Model):
    """Immutable, append-only, chronological provisioning evidence for an ``AccountRuntime``.

    NOTE: distinct from ``strategies.StrategyRuntimeEvent`` — different app/table, no collision.
    Content-immutable: updates are refused at the app layer (``save()``) AND at the DB layer (a
    BEFORE-UPDATE trigger, migration 0005). Direct ``delete()`` is refused at the app layer; rows are
    removable only by CASCADE when the owning account/runtime is deleted (a lifecycle op, not a rewrite).
    """

    runtime = models.ForeignKey(AccountRuntime, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=32, default="TRANSITION")  # TRANSITION / RETRY / FAILURE
    from_state = models.CharField(max_length=20, blank=True, default="")
    to_state = models.CharField(max_length=20, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    detail = models.TextField(blank=True, default="")  # sanitised
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]  # chronological
        indexes = [models.Index(fields=["runtime", "id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("RuntimeEvent is append-only/immutable; updates are not allowed")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("RuntimeEvent is append-only/immutable; deletes are not allowed")

    def __str__(self):
        return f"RuntimeEvent(rt={self.runtime_id}, {self.from_state}->{self.to_state})"


class BetaCapacityLock(models.Model):
    """GFX-BETA-HEADLESS Increment 1 — a singleton row used purely as a serialising lock so that
    concurrent beta runtime-slot reservations cannot both slip past the global cap. Reservers
    ``select_for_update()`` this row inside the reservation transaction (see ``beta_capacity``)."""
    singleton = models.PositiveSmallIntegerField(primary_key=True, default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Beta Capacity Lock"
        verbose_name_plural = "Beta Capacity Lock"

    def __str__(self):
        return "BetaCapacityLock(singleton)"
