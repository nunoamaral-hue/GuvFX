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
