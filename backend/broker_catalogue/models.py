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

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class CatalogueVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"          # being assembled; artefacts may still be added
        ACTIVE = "ACTIVE", "Active"       # the one live version provisioning resolves (at most one)
        RETIRED = "RETIRED", "Retired"    # superseded; kept for audit + rollback

    label = models.CharField(max_length=32, unique=True)   # e.g. "v1"
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT, db_index=True)
    # Aggregate manifest hash over the version's artefacts — a single value that changes if any artefact changes,
    # so an active version is tamper-evident as a whole. Computed by the service on activation.
    manifest_sha256 = models.CharField(max_length=64, blank=True, default="")
    # The manifest algorithm used for THIS version (so a stronger algo can be introduced without breaking already-
    # ACTIVE versions). "" = legacy (broker_id:sha256 pairs only — the shipped v1). MANIFEST_ALGO on the service is
    # the current strong algo, which also binds each artefact's servers/size/kind/sanitisation, so a servers-list or
    # size mutation (not just a byte change) is detected. Verification recomputes using THIS field's algo.
    manifest_algo = models.CharField(max_length=32, blank=True, default="")
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

    def save(self, *args, **kwargs):
        # IMMUTABILITY (model-layer): an artefact may only be created or updated via the ORM ``save()`` path while
        # its version is DRAFT; once the version is ACTIVE/RETIRED, ``save()`` refuses. Any update requires a NEW
        # version. SCOPE: this guards the ``save()``/``update_or_create`` path only — a raw ``QuerySet.update()`` /
        # ``bulk_update`` (a full-trust DB-write actor) bypasses it; a DB-level trigger closing that is a tracked
        # follow-up, and resolution re-verifies the manifest as a second line of defence. Fresh status read (never
        # a stale in-memory version).
        if self.version_id is not None:
            status = (CatalogueVersion.objects.filter(pk=self.version_id)
                      .values_list("status", flat=True).first())
            if status is not None and status != CatalogueVersion.Status.DRAFT:
                raise ValidationError(
                    f"CatalogueArtefact is immutable: version {self.version_id} is {status}, not DRAFT")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"CatalogueArtefact({self.broker_id}@{self.version.label}, {self.sha256[:12]})"


class CatalogueCaptureCandidate(models.Model):
    """GFX-BROKER-INTELLIGENCE-FOUNDATION-001 Stream A / ADR-0078 — the governed NEEDS_CAPTURE entry point.

    A broker whose authoritative ``servers.dat`` has NOT yet been captured (e.g. Dominion Markets) cannot have a
    ``CatalogueArtefact`` or an ``ArtefactApproval`` — there are no bytes and no SHA to approve. This row is the
    governed placeholder that records *that a capture is outstanding* so the broker enters the catalogue pipeline
    through a reviewable front door rather than an ad-hoc artefact. It holds IDENTITY + a non-secret server hint
    ONLY: deliberately NO servers.dat bytes, NO SHA-256, NO approval, NO credential, NO account/PII. It NEVER
    becomes an artefact here — promotion from NEEDS_CAPTURE requires a separate, authorised capture step
    (STOP boundary). Writing a candidate is DARK (gated by ``broker_catalogue.flags.capture_workflow_enabled``).
    """

    class Status(models.TextChoices):
        NEEDS_CAPTURE = "NEEDS_CAPTURE", "Needs capture"   # no authoritative servers.dat captured yet
        CAPTURED = "CAPTURED", "Captured"                  # set ONLY by a separate authorised capture step
        SUPERSEDED = "SUPERSEDED", "Superseded"            # retired in favour of a newer candidate

    broker_id = models.CharField(max_length=32, unique=True)      # stable slug, e.g. "dominion_markets"
    display_name = models.CharField(max_length=64)               # e.g. "Dominion Markets"
    # Broker-published, non-PII server identity hint (e.g. "DominionMarkets-Live"). NOT an account/credential.
    server_identity_hint = models.CharField(max_length=160, blank=True, default="")
    capture_status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEEDS_CAPTURE,
                                      db_index=True)
    reason = models.CharField(max_length=200, blank=True, default="")   # non-secret governance note
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="catalogue_capture_candidates")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CatalogueCaptureCandidate({self.broker_id}, {self.capture_status})"


# ======================================================================================================
# ADR-0102 (Lane C) — governed, hash-PINNED catalogue DELTA + append-only transition audit.
#
# A ``CatalogueDelta`` is a reviewable, approvable proposed change from the current ACTIVE catalogue version
# (the BASE) to a proposed DRAFT version, carrying the privacy-safe server-identity diff and a deterministic
# checksum. It adds the governance layer the base subsystem lacks: a DRAFT→REVIEWED→APPROVED→STAGED→ACTIVATED
# (or REJECTED / ROLLED_BACK) lifecycle, separate reviewer/approver, an APPEND-ONLY transition history, and an
# EXPECTED-base-manifest hash so activation fails closed on a stale base (optimistic concurrency / CAS). It
# REUSES the existing version/artefact/manifest/sanitiser machinery underneath — it never re-implements it, and
# never writes a host/servers.dat (the host seams stay fail-closed/DARK). It stores IDENTITY + SHA-256 integrity
# hashes + enums/counts only — NEVER servers.dat bytes, NEVER a credential/account number.
# ======================================================================================================

class CatalogueDelta(models.Model):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "Draft (proposed; not yet reviewed)"
        REVIEWED = "REVIEWED", "Dry-run validated / reviewed"
        APPROVED = "APPROVED", "Approved (NOT activated)"
        STAGED = "STAGED", "Staged (deployment artifact prepared; ready to activate)"
        ACTIVATED = "ACTIVATED", "Activated (the proposed version is now ACTIVE)"
        REJECTED = "REJECTED", "Rejected"
        ROLLED_BACK = "ROLLED_BACK", "Rolled back to the base version"

    TERMINAL = frozenset({"ACTIVATED", "REJECTED", "ROLLED_BACK"})

    # BASE = the expected current ACTIVE version at propose time; ``base_manifest_sha256`` is the hash-pin re-checked
    # (CAS) at activation. Nullable for the first-ever delta (no prior ACTIVE version).
    base_version = models.ForeignKey(CatalogueVersion, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="deltas_from")
    base_manifest_sha256 = models.CharField(max_length=64, blank=True, default="")
    # PROPOSED = the DRAFT version this delta would make ACTIVE (built from APPROVED artefacts by the base subsystem).
    proposed_version = models.ForeignKey(CatalogueVersion, on_delete=models.PROTECT, related_name="deltas_to")
    proposed_manifest_sha256 = models.CharField(max_length=64, blank=True, default="")

    # Privacy-safe server-identity diff. Each entry: {broker_id, display_name, servers:[...], sha256}. Server names
    # are broker-PUBLISHED identifiers (never a credential/account); sha256 is a non-secret integrity hash.
    added_identities = models.JSONField(default=list, blank=True)
    removed_identities = models.JSONField(default=list, blank=True)
    changed_identities = models.JSONField(default=list, blank=True)   # {broker_id, from_servers, to_servers, from_sha, to_sha}
    delta_sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)

    state = models.CharField(max_length=12, choices=State.choices, default=State.DRAFT, db_index=True)
    # True while in the pre-activation workflow (DRAFT/REVIEWED/APPROVED/STAGED). A partial-unique on this enforces
    # AT MOST ONE in-flight delta — two concurrent proposals cannot both race to activate. Set False on a terminal.
    in_flight = models.BooleanField(default=True)

    proposer = models.CharField(max_length=128, blank=True, default="")
    reviewer = models.CharField(max_length=128, blank=True, default="")
    approver = models.CharField(max_length=128, blank=True, default="")
    ticket = models.CharField(max_length=64, blank=True, default="")
    reason = models.CharField(max_length=256, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    staged_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["in_flight"], condition=models.Q(in_flight=True),
                                    name="broker_catalogue_one_in_flight_delta"),
        ]
        indexes = [models.Index(fields=["state"]), models.Index(fields=["proposed_version"])]

    @staticmethod
    def _canon(value):
        """Deterministic canonicalisation: lists sorted by canonical JSON, dict keys sorted, inner server lists
        sorted — so a content-identical diff hashes identically regardless of ordering."""
        import json as _json
        if isinstance(value, dict):
            return {str(k): CatalogueDelta._canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (list, tuple)):
            items = [CatalogueDelta._canon(v) for v in value]
            return sorted(items, key=lambda x: _json.dumps(x, sort_keys=True, ensure_ascii=True))
        return value

    def compute_delta_sha256(self) -> str:
        import hashlib as _hashlib
        import json as _json
        payload = _json.dumps({
            "base_manifest": self.base_manifest_sha256, "proposed_manifest": self.proposed_manifest_sha256,
            "added": self._canon(self.added_identities), "removed": self._canon(self.removed_identities),
            "changed": self._canon(self.changed_identities),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return _hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL

    def __str__(self):
        return (f"CatalogueDelta(#{self.pk} {self.state}, "
                f"{(self.base_version.label if self.base_version else '∅')}→{self.proposed_version.label})")


class CatalogueDeltaAudit(models.Model):
    """ADR-0102 — APPEND-ONLY transition/approval history for a ``CatalogueDelta``. Write-once; never updated or
    deleted (model guards + a DB trigger). Records each lifecycle action, the actor, a sanitised reason/ticket,
    the resulting state, and the base/delta integrity hashes. NO bytes/credential/PII."""

    class Action(models.TextChoices):
        PROPOSE = "PROPOSE", "Proposed"
        REVIEW = "REVIEW", "Reviewed / dry-run validated"
        APPROVE = "APPROVE", "Approved"
        STAGE = "STAGE", "Staged"
        ACTIVATE = "ACTIVATE", "Activated"
        REJECT = "REJECT", "Rejected"
        ROLLBACK = "ROLLBACK", "Rolled back"

    delta = models.ForeignKey(CatalogueDelta, on_delete=models.PROTECT, related_name="audit")
    action = models.CharField(max_length=10, choices=Action.choices)
    actor = models.CharField(max_length=128)
    reason = models.CharField(max_length=256, blank=True, default="")
    ticket = models.CharField(max_length=64, blank=True, default="")
    state_after = models.CharField(max_length=12, blank=True, default="")
    base_manifest_sha256 = models.CharField(max_length=64, blank=True, default="")
    delta_sha256 = models.CharField(max_length=64, blank=True, default="")
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["delta", "id"]), models.Index(fields=["action"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("CatalogueDeltaAudit is append-only; a correction is a new row")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("CatalogueDeltaAudit is append-only; it is never deleted")

    def __str__(self):
        return f"CatalogueDeltaAudit(delta={self.delta_id}, {self.action}, {self.actor})"
