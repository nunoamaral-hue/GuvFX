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


class ConsumptionContract(models.Model):
    """Deliverable 1 (WP-2) — intelligence handed to WIMS for education.

    The first WIMS-side persisted object in the signal-sourced flow. This is a
    *consumption contract*, NOT a Signal / Trade / Execution object: WIMS records
    the externally-sourced intelligence it has *received* so it can produce
    educational context and content. WIMS never acts on it, never trades it, and
    never persists Signal / Trade / MT5 / Broker / Execution objects (ADR-009:
    "WIMS never trades"). The price/direction fields are descriptive metadata of
    the received intelligence, not an executable order.

    ``contract_id`` is the model primary key (``id``).
    """

    class SourceType(models.TextChoices):
        WAYOND = "WAYOND", "Wayond"
        MANUAL = "MANUAL", "Manual entry"
        TRADE_RESULT = "TRADE_RESULT", "Trade result"  # WP-3 — external outcome intel

    class SignalType(models.TextChoices):
        ENTRY = "ENTRY", "Entry"
        EXIT = "EXIT", "Exit"
        UPDATE = "UPDATE", "Update"

    class Direction(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class ResultType(models.TextChoices):  # WP-3 — outcome category of a trade result
        WIN = "WIN", "Win"
        LOSS = "LOSS", "Loss"
        BREAKEVEN = "BREAKEVEN", "Breakeven"

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        ARCHIVED = "ARCHIVED", "Archived"

    source_type = models.CharField(
        max_length=16, choices=SourceType.choices, default=SourceType.WAYOND
    )
    source_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="External provenance only (e.g. provider name, message id). "
                  "Not a broker/execution reference.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="wims_contracts",
    )
    signal_type = models.CharField(
        max_length=16, choices=SignalType.choices, blank=True
    )
    symbol = models.CharField(max_length=32, blank=True)
    direction = models.CharField(
        max_length=8, choices=Direction.choices, blank=True
    )
    entry_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    stop_loss = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    take_profit = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    confidence = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Reported confidence of the source intelligence, 0–100.",
    )
    # --- WP-3: trade-result intelligence (descriptive only; NOT a trade record).
    # Populated when source_type == TRADE_RESULT. All optional so WAYOND/MANUAL
    # entry contracts are unaffected.
    exit_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    result_type = models.CharField(
        max_length=12, choices=ResultType.choices, blank=True
    )
    profit_loss = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="Reported P/L of the source result (account currency). "
                  "Descriptive metadata, not a settled deal.",
    )
    pips = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    close_time = models.DateTimeField(null=True, blank=True)
    commentary = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RECEIVED
    )
    raw_signal = models.TextField(
        blank=True, help_text="Verbatim external input as received."
    )

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Consumption contract"

    def __str__(self) -> str:
        sym = self.symbol or "?"
        return f"Contract #{self.pk} {self.direction} {sym} [{self.status}]"


class Context(models.Model):
    """Deliverable 2 — educational context.

    Originates from EITHER an ``EducationalTopic`` (WP-1) or a
    ``ConsumptionContract`` (WP-2). Exactly one origin is set; the Content /
    Review / Publish flow downstream is identical regardless of origin.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY_FOR_CONTENT = "READY_FOR_CONTENT", "Ready for content"
        ARCHIVED = "ARCHIVED", "Archived"

    # WP-1 origin (topic). FK column ``source_id``. Now nullable so a Context
    # can instead originate from a ConsumptionContract (WP-2) — additive, no
    # WP-1 behaviour change (WP-1 always sets this).
    source = models.ForeignKey(
        EducationalTopic,
        on_delete=models.CASCADE,
        related_name="contexts",
        null=True,
        blank=True,
    )
    # WP-2 origin (consumption contract).
    contract = models.ForeignKey(
        ConsumptionContract,
        on_delete=models.CASCADE,
        related_name="contexts",
        null=True,
        blank=True,
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
        constraints = [
            models.CheckConstraint(
                name="wims_context_exactly_one_origin",
                check=(
                    models.Q(source__isnull=False, contract__isnull=True)
                    | models.Q(source__isnull=True, contract__isnull=False)
                ),
            )
        ]

    @property
    def origin(self):
        """Return the originating object (topic or contract)."""
        return self.source or self.contract

    def __str__(self) -> str:
        return f"Context #{self.pk} from {self.origin} [{self.status}]"


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
        # WP-2 — consumption contract lifecycle (existing events unchanged).
        CONTRACT_CREATED = "CONTRACT_CREATED", "Contract created"
        CONTRACT_PROCESSED = "CONTRACT_PROCESSED", "Contract processed"
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
