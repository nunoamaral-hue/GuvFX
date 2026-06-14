"""
WP-1 acceptance tests for the WIMS Educational Content Flow.

These assert the acceptance criteria:
  * each pipeline object is created with the right status,
  * human review is mandatory (cannot skip states),
  * publishing requires approval,
  * the audit trail records every step,
  * workflow states stay distinguishable.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import IntegrityError
from django.test import TestCase

from wims import services
from wims.models import (
    AuditEvent,
    ConsumptionContract,
    Content,
    Context,
    EducationalTopic,
    Publish,
    Review,
    WorkflowState,
)

User = get_user_model()


class EducationalContentFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", email="op@example.invalid", password="x"
        )

    def _make_content(self):
        topic = services.create_topic(title="What Is Risk Management?", actor=self.user)
        ctx = services.create_context(topic=topic, context_text="why it matters", actor=self.user)
        content = services.create_content(
            context=ctx, title="t", content_text="body", actor=self.user
        )
        return topic, ctx, content

    def test_happy_path_end_to_end(self):
        topic, ctx, content = self._make_content()
        self.assertEqual(content.status, Content.Status.DRAFT)

        services.submit_for_review(content=content, actor=self.user)
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.READY_FOR_REVIEW)
        self.assertEqual(workflow_state := services.workflow_state_for_topic(topic),
                         WorkflowState.AWAITING_REVIEW)

        review = services.review_content(
            content=content, decision=Review.Decision.APPROVE, reviewer=self.user
        )
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.APPROVED)
        self.assertEqual(services.workflow_state_for_topic(topic), WorkflowState.AWAITING_PUBLISH)

        pub = services.publish_content(
            content=content, channel=Publish.Channel.TELEGRAM, publisher=self.user
        )
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.PUBLISHED)
        self.assertTrue(pub.simulated)
        self.assertEqual(services.workflow_state_for_topic(topic), WorkflowState.PUBLISHED)

        # 6 evidence records exist
        self.assertEqual(EducationalTopic.objects.count(), 1)
        self.assertEqual(Context.objects.count(), 1)
        self.assertEqual(Content.objects.count(), 1)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Publish.objects.count(), 1)

        # audit: one row per step (source, context, content, submit, review, publish)
        events = list(AuditEvent.objects.values_list("event", flat=True))
        self.assertEqual(
            events,
            [
                AuditEvent.Event.SOURCE_CREATED,
                AuditEvent.Event.CONTEXT_CREATED,
                AuditEvent.Event.CONTENT_CREATED,
                AuditEvent.Event.SUBMITTED_FOR_REVIEW,
                AuditEvent.Event.REVIEW_DECISION,
                AuditEvent.Event.PUBLISHED,
            ],
        )

    def test_review_is_mandatory_cannot_publish_draft(self):
        _, _, content = self._make_content()
        with self.assertRaises(ValidationError):
            services.publish_content(
                content=content, channel=Publish.Channel.X, publisher=self.user
            )

    def test_cannot_review_before_submission(self):
        _, _, content = self._make_content()
        with self.assertRaises(ValidationError):
            services.review_content(
                content=content, decision=Review.Decision.APPROVE, reviewer=self.user
            )

    def test_rejected_content_cannot_be_published(self):
        _, _, content = self._make_content()
        services.submit_for_review(content=content, actor=self.user)
        services.review_content(
            content=content, decision=Review.Decision.REJECT, reviewer=self.user
        )
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.REJECTED)
        with self.assertRaises(ValidationError):
            services.publish_content(
                content=content, channel=Publish.Channel.TELEGRAM, publisher=self.user
            )

    def test_workflow_states_are_distinguishable(self):
        topic = services.create_topic(title="What Is A Trend?", actor=self.user)
        self.assertEqual(services.workflow_state_for_topic(topic), WorkflowState.AWAITING_CONTEXT)
        ctx = services.create_context(topic=topic, context_text="...", actor=self.user)
        self.assertEqual(services.workflow_state_for_topic(topic), WorkflowState.AWAITING_CONTENT)
        content = services.create_content(context=ctx, title="t", content_text="b", actor=self.user)
        services.submit_for_review(content=content, actor=self.user)
        self.assertEqual(services.workflow_state_for_topic(topic), WorkflowState.AWAITING_REVIEW)


class WayondSignalFlowTests(TestCase):
    """WP-2 acceptance tests — Wayond Signal Flow (contract-sourced)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="op2", email="op2@example.invalid", password="x"
        )

    def _make_contract(self):
        return services.create_contract(
            actor=self.user,
            source_type=ConsumptionContract.SourceType.WAYOND,
            symbol="XAUUSD",
            direction=ConsumptionContract.Direction.BUY,
            entry_price=Decimal("3350"),
            stop_loss=Decimal("3335"),
            take_profit=Decimal("3370"),
            raw_signal="BUY XAUUSD\nEntry 3350\nSL 3335\nTP 3370",
        )

    def test_contract_to_publish_end_to_end(self):
        contract = self._make_contract()
        self.assertEqual(contract.status, ConsumptionContract.Status.RECEIVED)
        self.assertEqual(
            services.workflow_state_for_contract(contract), WorkflowState.AWAITING_CONTEXT
        )

        ctx = services.create_context_from_contract(
            contract=contract, context_text="educational definitions", actor=self.user
        )
        contract.refresh_from_db()
        self.assertEqual(contract.status, ConsumptionContract.Status.PROCESSED)
        self.assertEqual(ctx.contract_id, contract.id)
        self.assertIsNone(ctx.source_id)  # contract origin, not a topic
        self.assertEqual(ctx.origin, contract)

        content = services.create_content(
            context=ctx, title="terms", content_text="neutral education", actor=self.user
        )
        services.submit_for_review(content=content, actor=self.user)
        services.review_content(
            content=content, decision=Review.Decision.APPROVE, reviewer=self.user
        )
        services.publish_content(
            content=content, channel=Publish.Channel.TELEGRAM, publisher=self.user
        )
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.PUBLISHED)
        self.assertEqual(
            services.workflow_state_for_contract(contract), WorkflowState.PUBLISHED
        )

        # D4 — audit covers the two new contract events plus the WP-1 chain
        events = list(AuditEvent.objects.values_list("event", flat=True))
        self.assertEqual(
            events,
            [
                AuditEvent.Event.CONTRACT_CREATED,
                AuditEvent.Event.CONTEXT_CREATED,
                AuditEvent.Event.CONTRACT_PROCESSED,
                AuditEvent.Event.CONTENT_CREATED,
                AuditEvent.Event.SUBMITTED_FOR_REVIEW,
                AuditEvent.Event.REVIEW_DECISION,
                AuditEvent.Event.PUBLISHED,
            ],
        )

    def test_contract_processed_only_once(self):
        contract = self._make_contract()
        services.create_context_from_contract(
            contract=contract, context_text="x", actor=self.user
        )
        with self.assertRaises(ValidationError):
            services.create_context_from_contract(
                contract=contract, context_text="y", actor=self.user
            )

    def test_context_requires_exactly_one_origin(self):
        # Neither origin -> DB check constraint violation.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Context.objects.create(context_text="orphan")

    def test_no_prohibited_object_types_persisted(self):
        # ADR-009: wims must not define Signal/Trade/MT5/Broker/Execution models.
        from django.apps import apps
        names = [m.__name__.lower() for m in apps.get_app_config("wims").get_models()]
        for banned in ("signal", "trade", "mt5", "broker", "execution"):
            self.assertFalse(
                any(banned in n for n in names),
                f"prohibited model type {banned!r} found in wims: {names}",
            )
