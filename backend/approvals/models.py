"""A5 — a generic, reusable, human-gated approval primitive that binds an approval to an EXACT artefact SHA-256.

Purpose: a candidate artefact (e.g. a broker ``servers.dat`` for the broker catalogue) may only become trusted
after an explicit human YES, and the YES binds to the exact bytes (SHA-256). This model is intentionally
narrow and non-secret — it stores only an artefact IDENTITY + SHA-256 + a decision, never the artefact bytes
or any credential. It is consumed fail-closed by provisioning (``services.is_artefact_approved``).

Governance: the PENDING -> APPROVED/REJECTED transition is human-only (``services.decide`` requires a staff
user; there is deliberately no DRF endpoint and no auto-approve path), satisfying the CLAUDE.md rule that model
output must never approve anything without an explicit human-gated control path.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class ArtefactApproval(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    # WHAT is being approved. ``artefact_kind`` groups a family (e.g. "broker_servers_dat"); ``artefact_ref`` is
    # the stable logical reference within that family (e.g. "pepperstone/v1"); ``sha256`` pins the exact bytes.
    artefact_kind = models.CharField(max_length=64)
    artefact_ref = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="artefact_approvals_requested")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="artefact_approvals_decided")
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True, default="")
    # Non-secret provenance only (broker, servers, sanitisation verdict, source build). NEVER artefact bytes.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # The same exact bytes for the same ref can be registered at most once.
            models.UniqueConstraint(
                fields=["artefact_kind", "artefact_ref", "sha256"], name="artefact_approval_unique_sha"),
            # At most ONE approved SHA per (kind, ref): approving a new version cannot silently leave two
            # approved artefacts for the same logical reference (a second APPROVE must supersede, not coexist).
            models.UniqueConstraint(
                fields=["artefact_kind", "artefact_ref"], condition=Q(status="APPROVED"),
                name="artefact_approval_one_approved_per_ref"),
        ]
        indexes = [models.Index(fields=["artefact_kind", "artefact_ref", "status"], name="artefact_approval_idx")]

    def mark(self, *, approve: bool, decided_by, reason: str = "", now=None) -> None:
        self.status = self.Status.APPROVED if approve else self.Status.REJECTED
        self.decided_by = decided_by
        self.decided_at = now or timezone.now()
        self.reason = str(reason or "")[:200]
