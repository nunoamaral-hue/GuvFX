"""broker_catalogue.models — Broker Catalogue V1 immutable, versioned, hash-bound store.

Two models realise the packet's catalogue contract:

* ``CatalogueVersion`` — an immutable catalogue *version* (e.g. "v1"). Exactly ONE may be ACTIVE at a time
  (the single atomic active pointer, enforced by a partial unique constraint). A version's artefact set is
  frozen once it leaves DRAFT; ``rollback_to`` records the version this one supersedes so a promotion is
  reversible by re-activating the predecessor.
* ``CatalogueArtefact`` — one broker's bootstrap artefact within a version: the broker identity, the exact
  ``servers.dat`` SHA-256 (bound to the bytes staged on the host at ``host_relpath``), size, capture
  provenance, sanitisation + certification verdicts, and a link to the human ``ArtefactApproval`` that
  authorised it. It stores IDENTITY + SHA + provenance only — NEVER artefact bytes and NEVER any credential.

The DB is the authority for *which* version is active and the *expected* SHA of each artefact; the immutable
bytes live on the host under ``catalogue/versions/<label>/<broker_id>/servers.dat``. Provisioning
(``broker_catalogue.preseed``) copies ONLY an APPROVED, SHA-verified artefact into a fresh runtime, and
fails closed / falls back to native discovery otherwise. Nothing here mutates the golden image.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class CatalogueVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"          # being assembled; artefacts may still be added
        ACTIVE = "ACTIVE", "Active"       # the one live version provisioning resolves (at most one)
        RETIRED = "RETIRED", "Retired"    # superseded; kept for audit + rollback

    label = models.CharField(max_length=32, unique=True)   # e.g. "v1"
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT, db_index=True)
    # Aggregate manifest hash over the version's (broker_id, sha256) set — a single value that changes if any
    # artefact changes, so an active version is tamper-evident as a whole. Computed by the service on activation.
    manifest_sha256 = models.CharField(max_length=64, blank=True, default="")
    rollback_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by")
    notes = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Exactly one ACTIVE version at any time — the atomic active pointer (DB-enforced, not a mutable file).
            models.UniqueConstraint(fields=["status"], condition=models.Q(status="ACTIVE"),
                                    name="broker_catalogue_one_active_version"),
        ]

    def __str__(self):
        return f"CatalogueVersion({self.label}, {self.status})"


class CatalogueArtefact(models.Model):
    version = models.ForeignKey(CatalogueVersion, on_delete=models.CASCADE, related_name="artefacts")
    broker_id = models.CharField(max_length=32)                 # stable slug, e.g. "pepperstone", "is6"
    display_name = models.CharField(max_length=64)              # e.g. "Pepperstone", "IS6 Technologies"
    # Broker server names this artefact makes selectable (e.g. ["IS6Technologies-Demo","IS6Technologies-Live"]).
    # Provisioning resolves a customer's account server to a broker via THIS list — never by email/account guessing.
    servers = models.JSONField(default=list)
    artefact_kind = models.CharField(max_length=64, default="broker_servers_dat")
    artefact_ref = models.CharField(max_length=200)            # the approval ref, e.g. "pepperstone/v1"
    sha256 = models.CharField(max_length=64)                   # EXACT bytes of the staged servers.dat
    size_bytes = models.PositiveIntegerField(default=0)
    source_mt5_build = models.CharField(max_length=32, blank=True, default="")
    # Host-relative path of the immutable versioned bytes: "versions/<label>/<broker_id>/servers.dat".
    host_relpath = models.CharField(max_length=255)
    capture_provenance = models.JSONField(default=dict)        # non-secret only (source ref, no accounts.dat, etc.)
    sanitisation_result = models.CharField(max_length=16, default="")   # PASS / FAIL
    certification_result = models.CharField(max_length=32, default="")  # PASS / PENDING / FAIL (behavioural)
    # The human ArtefactApproval (approvals app) that authorised these exact bytes. NULL while unapproved.
    approval = models.ForeignKey("approvals.ArtefactApproval", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="catalogue_artefacts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One artefact per (version, broker_id) — a version has at most one bootstrap per broker.
            models.UniqueConstraint(fields=["version", "broker_id"], name="broker_catalogue_one_artefact_per_broker"),
        ]
        indexes = [models.Index(fields=["broker_id"], name="broker_catalogue_broker_idx")]

    def __str__(self):
        return f"CatalogueArtefact({self.broker_id}@{self.version.label}, {self.sha256[:12]})"
