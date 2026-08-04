"""WP5.1 — the authoritative OperationalEvent read model (ADR-0032).

A query-optimised, NON-SECRET, owner-scoped operational timeline. It is a DERIVED, REBUILDABLE
PROJECTION (a cache in the ``.claude/rules/data.md`` sense), NOT an authoritative record:
``core.audit.AuditEvent`` remains the immutable security ledger, and the WP1A/WP3/WP2 models
(``trading.BrokerAccountValidationAttempt``, ``reliability.BrokerAccountHealth``,
``execution.BrokerRuntimePause``) remain the authoritative operational state. Unlike audit, this row is
mutable (``resolved`` may flip) precisely because it is a projection, not evidence.

Fields carry ONLY non-secret data: no credentials, no ciphertext, no host paths, no operator
diagnostics. The recording service (``events.record_event``) enforces this structurally (allow-listed
fields) and defensively (metadata key-denylist sanitiser).
"""
from __future__ import annotations

from django.db import models

from .constants import OPEN_SEVERITIES


class OperationalEvent(models.Model):
    class Category(models.TextChoices):
        VALIDATION = "VALIDATION", "Validation"
        HEALTH = "HEALTH", "Health"
        EXECUTION = "EXECUTION", "Execution"
        RUNTIME = "RUNTIME", "Runtime"
        CREDENTIAL = "CREDENTIAL", "Credential"
        CONNECTIVITY = "CONNECTIVITY", "Connectivity"
        SYSTEM = "SYSTEM", "System"

    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        ERROR = "ERROR", "Error"
        CRITICAL = "CRITICAL", "Critical"

    # Owner linkage. Nullable: a SYSTEM/estate-wide event may have no owning account (operator-only — it
    # can never surface on the owner-scoped endpoint, which requires an account_id). CASCADE keeps the
    # projection consistent when an account row is (rarely) hard-deleted; disconnect TOMBSTONES the row.
    account = models.ForeignKey(
        "trading.TradingAccount", on_delete=models.CASCADE,
        related_name="operational_events", null=True, blank=True)
    # Logical runtime identity (AccountRuntime.runtime_uuid). A soft string reference — NOT a FK — to
    # avoid coupling this read model to the terminal_provisioning lifecycle. Non-secret.
    runtime_uuid = models.CharField(max_length=64, blank=True, default="", db_index=True)

    category = models.CharField(max_length=16, choices=Category.choices, db_index=True)
    # Free-form (like core.audit event_type) so a new operational event type never needs a migration.
    event_type = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(
        max_length=16, choices=Severity.choices, default=Severity.INFO, db_index=True)
    # Optional per-event lifecycle/status token (e.g. the upstream HEALTHY/NEEDS_ATTENTION status).
    status = models.CharField(max_length=32, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Customer-safe one-line summary (no diagnostics).
    summary = models.CharField(max_length=255, blank=True, default="")
    # Emitting subsystem (see constants.SOURCE_*).
    source = models.CharField(max_length=64, blank=True, default="")
    correlation_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    # The upstream state_version (e.g. WP3 health), for correlating with state transitions.
    state_version = models.PositiveIntegerField(null=True, blank=True)
    actor = models.CharField(max_length=128, blank=True, default="")
    customer_visible = models.BooleanField(default=False, db_index=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Idempotency handle for the recording layer — a same non-empty key is inserted at most once
    # (enforced by the partial unique constraint below).
    dedup_key = models.CharField(max_length=200, blank=True, default="", db_index=True)
    # Structured, NON-SECRET detail (allow-listed projections only; defensively sanitised by the recorder).
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["account", "-created_at"], name="opev_acct_created_idx"),
            # The customer-visible timeline is the single hottest read path (every non-staff API GET).
            models.Index(fields=["account", "customer_visible", "-created_at"], name="opev_acct_vis_idx"),
            models.Index(fields=["account", "category", "-created_at"], name="opev_acct_cat_idx"),
            models.Index(fields=["account", "severity", "-created_at"], name="opev_acct_sev_idx"),
            models.Index(fields=["account", "resolved"], name="opev_acct_resolved_idx"),
            models.Index(fields=["correlation_id"], name="opev_corr_idx"),
        ]
        constraints = [
            # Idempotency: a non-empty dedup_key is unique. Empty keys ("") are exempt so un-keyed
            # events can be recorded freely.
            models.UniqueConstraint(
                fields=["dedup_key"], condition=~models.Q(dedup_key=""),
                name="opev_uniq_dedup_key"),
        ]

    def __str__(self):
        return (f"OperationalEvent(#{self.pk} {self.category}/{self.event_type}/{self.severity} "
                f"acct={self.account_id})")

    @property
    def is_open(self) -> bool:
        """An operational event is 'open' (needs attention) when it is unresolved and non-INFO."""
        return (not self.resolved) and self.severity in OPEN_SEVERITIES
