"""
Reconciliation event model — detection-only.

Stores discrepancies detected between MT5 source data and GuvFX platform
trade records.  No mutation of Trade or TradingAccount rows is permitted
anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json

from django.db import models
from trading.models import TradingAccount


class ReconciliationEvent(models.Model):
    """
    A single detected discrepancy between an MT5 source value and the
    corresponding GuvFX platform value for one field of one trade.

    Immutability of financial records is enforced by design: the
    reconciliation subsystem only *reads* Trade/TradingAccount rows and
    *creates* ReconciliationEvent rows.
    """

    # ------------------------------------------------------------------
    # Severity
    # ------------------------------------------------------------------
    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    # ------------------------------------------------------------------
    # Resolution status
    # ------------------------------------------------------------------
    class ResolutionStatus(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="reconciliation_events",
    )

    reconciliation_run_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Opaque identifier for the reconciliation run that produced this event.",
    )

    reconciliation_type = models.CharField(
        max_length=64,
        help_text="Category of reconciliation check (e.g. 'trade_field_mismatch').",
    )

    ticket = models.CharField(
        max_length=64,
        help_text="MT5 deal/position ticket as string.",
    )

    field_name = models.CharField(
        max_length=64,
        help_text="Name of the field that differs (e.g. 'profit', 'volume').",
    )

    mt5_value = models.TextField(
        blank=True,
        default="",
        help_text="Value as reported by MT5 source (text representation).",
    )

    platform_value = models.TextField(
        blank=True,
        default="",
        help_text="Value as stored on the GuvFX platform (text representation).",
    )

    severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.WARNING,
        db_index=True,
    )

    resolution_status = models.CharField(
        max_length=16,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.OPEN,
        db_index=True,
    )

    signature = models.CharField(
        max_length=64,
        db_index=True,
        help_text=(
            "Deterministic SHA-256 hex digest derived from "
            "(run_id, account_id, ticket, field_name, mt5_value, platform_value)."
        ),
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Bounded structured context.  Must not contain secrets, "
            "credentials, or arbitrary raw MT5 payload blobs."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------
    class Meta:
        app_label = "reconciliation"
        ordering = ["-created_at"]
        constraints = [
            # Duplicate suppression: within the same run, the exact same
            # discrepancy (account + ticket + field + signature) can only
            # appear once.
            models.UniqueConstraint(
                fields=[
                    "reconciliation_run_id",
                    "account",
                    "ticket",
                    "field_name",
                    "signature",
                ],
                name="uniq_recon_event_per_run",
            ),
        ]
        indexes = [
            models.Index(
                fields=["reconciliation_run_id", "account"],
                name="recon_run_account_idx",
            ),
        ]
        verbose_name = "Reconciliation Event"
        verbose_name_plural = "Reconciliation Events"

    def __str__(self) -> str:
        return (
            f"[{self.severity}] {self.reconciliation_type} "
            f"ticket={self.ticket} field={self.field_name} "
            f"run={self.reconciliation_run_id}"
        )

    # ------------------------------------------------------------------
    # Deterministic signature generation
    # ------------------------------------------------------------------
    @staticmethod
    def compute_signature(
        reconciliation_run_id: str,
        account_id: int | str,
        ticket: str,
        field_name: str,
        mt5_value: str,
        platform_value: str,
    ) -> str:
        """
        Produce a deterministic SHA-256 hex digest from the discrepancy
        identity tuple.  Stable serialization is achieved by JSON-encoding
        a list of the components (which are all converted to str first).
        """
        payload = json.dumps(
            [
                str(reconciliation_run_id),
                str(account_id),
                str(ticket),
                str(field_name),
                str(mt5_value),
                str(platform_value),
            ],
            separators=(",", ":"),
            sort_keys=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
