"""
WIMS — Educational Content Flow (WP-1) data model.

Scope (ADR-009 boundary): WIMS owns Context, Content, Human Review and
Publishing. There is deliberately NO trading, MT5 or signal logic here.

Pipeline:

    EducationalTopic  ->  Context  ->  Content  ->  Review  ->  Publish

Every state-changing step is mirrored into an immutable AuditEvent row
(see ``wims.services``) so the workflow is fully reconstructable.
"""

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Workflow state model (Deliverable 4)
#
# The per-object ``status`` fields below encode where a single object sits in
# its own lifecycle. ``WorkflowState`` is the *operator-facing* view: given a
# topic, which stage of the pipeline is it waiting on? These stay distinct and
# are derived in ``wims.services.workflow_state_for_topic``.
# ---------------------------------------------------------------------------
class WorkflowState(models.TextChoices):
    AWAITING_CONTEXT = "AWAITING_CONTEXT", "Awaiting context"
    AWAITING_CONTENT = "AWAITING_CONTENT", "Awaiting content"
    AWAITING_REVIEW = "AWAITING_REVIEW", "Awaiting review"
    AWAITING_PUBLISH = "AWAITING_PUBLISH", "Awaiting publish (approved)"
    PUBLISHED = "PUBLISHED", "Published"
    ARCHIVED = "ARCHIVED", "Archived"


class EducationalTopic(models.Model):
    """Deliverable 1 — source object representing an educational topic.

    Examples: "What Is Market Structure?", "What Is Risk Management?".
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_topics",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Educational topic"

    def __str__(self) -> str:
        return f"[{self.status}] {self.title}"


class Context(models.Model):
    """Deliverable 2 — educational context derived from a topic.

    Transforms the source topic into the "why it matters / common mistakes /
    what traders should understand" framing that feeds content generation.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY_FOR_CONTENT = "READY_FOR_CONTENT", "Ready for content"
        ARCHIVED = "ARCHIVED", "Archived"

    # FK column name is ``source_id`` per the WP "source_id" field spec.
    source = models.ForeignKey(
        EducationalTopic,
        on_delete=models.CASCADE,
        related_name="contexts",
    )
    context_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_contexts",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Context #{self.pk} for {self.source.title!r} [{self.status}]"


class Content(models.Model):
    """Deliverable 3 — audience-facing content generated from a Context.

    Examples: a Telegram post, an X post, an educational article.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PUBLISHED = "PUBLISHED", "Published"

    # FK column name is ``context_id`` per the WP "context_id" field spec.
    context = models.ForeignKey(
        Context,
        on_delete=models.CASCADE,
        related_name="contents",
    )
    title = models.CharField(max_length=255)
    content_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_contents",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Content #{self.pk} {self.title!r} [{self.status}]"


class Review(models.Model):
    """Deliverable 5 — mandatory human review record for a piece of Content.

    Human review is mandatory for the MVP: Content cannot reach APPROVED /
    REJECTED without a Review row being written.
    """

    class Decision(models.TextChoices):
        APPROVE = "APPROVE", "Approve"
        REJECT = "REJECT", "Reject"

    content = models.ForeignKey(
        Content,
        on_delete=models.CASCADE,
        related_name="reviews",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_reviews",
    )
    review_timestamp = models.DateTimeField(auto_now_add=True)
    review_decision = models.CharField(max_length=8, choices=Decision.choices)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-review_timestamp",)

    def __str__(self) -> str:
        return f"Review #{self.pk} {self.review_decision} on Content #{self.content_id}"


class Publish(models.Model):
    """Deliverable 6 — manual publish record for approved Content.

    Automation is explicitly excluded; a Publish row is only created by an
    operator-initiated action. The actual channel delivery is simulated for
    WP-1 (no Telegram/X integration yet).
    """

    class Channel(models.TextChoices):
        TELEGRAM = "TELEGRAM", "Telegram"
        X = "X", "X"

    content = models.ForeignKey(
        Content,
        on_delete=models.CASCADE,
        related_name="publications",
    )
    published_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_publications",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    # Marks this as a simulated delivery for WP-1 (no real channel integration).
    simulated = models.BooleanField(default=True)

    class Meta:
        ordering = ("-published_at",)
        verbose_name = "Publish record"

    def __str__(self) -> str:
        return f"Publish #{self.pk} Content #{self.content_id} -> {self.channel}"


class AuditEvent(models.Model):
    """Deliverable 7 — immutable audit trail.

    One row per meaningful workflow event. Append-only: never updated or
    deleted by application code.
    """

    class Event(models.TextChoices):
        SOURCE_CREATED = "SOURCE_CREATED", "Source created"
        CONTEXT_CREATED = "CONTEXT_CREATED", "Context created"
        CONTENT_CREATED = "CONTENT_CREATED", "Content created"
        SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW", "Submitted for review"
        REVIEW_DECISION = "REVIEW_DECISION", "Review decision"
        PUBLISHED = "PUBLISHED", "Published"

    timestamp = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_audit_events",
    )
    event = models.CharField(max_length=32, choices=Event.choices)
    object_type = models.CharField(max_length=32)
    object_id = models.PositiveBigIntegerField()
    # Free-form context (e.g. decision, channel) to make the trail self-describing.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("timestamp", "id")
        indexes = [
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["event"]),
        ]

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.event} {self.object_type}#{self.object_id}"
