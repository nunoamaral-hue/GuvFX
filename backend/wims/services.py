"""
WIMS workflow services (WP-1).

All state transitions for the Educational Content Flow go through this module
so that:

  * the legal status transitions are enforced in one place, and
  * every transition writes a matching ``AuditEvent`` row in the same DB
    transaction (audit can never silently drift from state).

The pipeline:

    create_topic -> create_context -> create_content
        -> submit_for_review -> review (approve/reject) -> publish
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    AuditEvent,
    Content,
    Context,
    EducationalTopic,
    Publish,
    Review,
    WorkflowState,
)


def record_audit(actor, event: str, obj, **detail) -> AuditEvent:
    """Append an immutable audit row describing ``event`` on ``obj``."""
    return AuditEvent.objects.create(
        actor=actor,
        event=event,
        object_type=obj.__class__.__name__,
        object_id=obj.pk,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Step 1 — Educational Topic (source)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_topic(*, title: str, description: str = "", actor=None,
                 status: str = EducationalTopic.Status.DRAFT) -> EducationalTopic:
    topic = EducationalTopic.objects.create(
        title=title, description=description, created_by=actor, status=status
    )
    record_audit(actor, AuditEvent.Event.SOURCE_CREATED, topic, title=title)
    return topic


# ---------------------------------------------------------------------------
# Step 2 — Context
# ---------------------------------------------------------------------------
@transaction.atomic
def create_context(*, topic: EducationalTopic, context_text: str, actor=None,
                   status: str = Context.Status.READY_FOR_CONTENT) -> Context:
    if topic.status == EducationalTopic.Status.ARCHIVED:
        raise ValidationError("Cannot create context for an archived topic.")
    ctx = Context.objects.create(
        source=topic, context_text=context_text, created_by=actor, status=status
    )
    record_audit(actor, AuditEvent.Event.CONTEXT_CREATED, ctx, source_id=topic.pk)
    return ctx


# ---------------------------------------------------------------------------
# Step 3 — Content
# ---------------------------------------------------------------------------
@transaction.atomic
def create_content(*, context: Context, title: str, content_text: str,
                   actor=None) -> Content:
    content = Content.objects.create(
        context=context,
        title=title,
        content_text=content_text,
        created_by=actor,
        status=Content.Status.DRAFT,
    )
    record_audit(actor, AuditEvent.Event.CONTENT_CREATED, content,
                 context_id=context.pk)
    return content


# ---------------------------------------------------------------------------
# Step 4 — Submit for review
# ---------------------------------------------------------------------------
@transaction.atomic
def submit_for_review(*, content: Content, actor=None) -> Content:
    if content.status != Content.Status.DRAFT:
        raise ValidationError(
            f"Only DRAFT content can be submitted for review (was {content.status})."
        )
    content.status = Content.Status.READY_FOR_REVIEW
    content.save(update_fields=["status"])
    record_audit(actor, AuditEvent.Event.SUBMITTED_FOR_REVIEW, content)
    return content


# ---------------------------------------------------------------------------
# Step 5 — Human review (mandatory)
# ---------------------------------------------------------------------------
@transaction.atomic
def review_content(*, content: Content, decision: str, reviewer=None,
                   notes: str = "") -> Review:
    if content.status != Content.Status.READY_FOR_REVIEW:
        raise ValidationError(
            f"Content must be READY_FOR_REVIEW to be reviewed (was {content.status})."
        )
    if decision not in Review.Decision.values:
        raise ValidationError(f"Unknown review decision: {decision!r}")

    review = Review.objects.create(
        content=content,
        reviewer=reviewer,
        review_decision=decision,
        review_notes=notes,
    )
    content.status = (
        Content.Status.APPROVED
        if decision == Review.Decision.APPROVE
        else Content.Status.REJECTED
    )
    content.save(update_fields=["status"])
    record_audit(reviewer, AuditEvent.Event.REVIEW_DECISION, content,
                 decision=decision, review_id=review.pk)
    return review


# ---------------------------------------------------------------------------
# Step 6 — Publish (manual only; channel delivery simulated for WP-1)
# ---------------------------------------------------------------------------
@transaction.atomic
def publish_content(*, content: Content, channel: str, publisher=None,
                    simulated: bool = True) -> Publish:
    if content.status != Content.Status.APPROVED:
        raise ValidationError(
            f"Only APPROVED content can be published (was {content.status})."
        )
    if channel not in Publish.Channel.values:
        raise ValidationError(f"Unknown channel: {channel!r}")

    pub = Publish.objects.create(
        content=content,
        published_by=publisher,
        channel=channel,
        simulated=simulated,
    )
    content.status = Content.Status.PUBLISHED
    content.save(update_fields=["status"])
    record_audit(publisher, AuditEvent.Event.PUBLISHED, content,
                 channel=channel, publish_id=pub.pk, simulated=simulated)
    return pub


# ---------------------------------------------------------------------------
# Deliverable 4 — operator-facing workflow state for a topic
# ---------------------------------------------------------------------------
def workflow_state_for_topic(topic: EducationalTopic) -> str:
    """Derive the single stage the topic's pipeline is currently waiting on.

    Distinct from the per-object ``status`` fields: this answers the operator's
    question "what is this topic blocked on right now?".
    """
    if topic.status == EducationalTopic.Status.ARCHIVED:
        return WorkflowState.ARCHIVED

    contents = list(
        Content.objects.filter(context__source=topic).order_by("-created_at")
    )
    if any(c.status == Content.Status.PUBLISHED for c in contents):
        return WorkflowState.PUBLISHED
    if any(c.status == Content.Status.APPROVED for c in contents):
        return WorkflowState.AWAITING_PUBLISH
    if any(c.status == Content.Status.READY_FOR_REVIEW for c in contents):
        return WorkflowState.AWAITING_REVIEW

    has_context = Context.objects.filter(source=topic).exists()
    if not has_context:
        return WorkflowState.AWAITING_CONTEXT
    # Context exists but no content past draft yet.
    return WorkflowState.AWAITING_CONTENT
