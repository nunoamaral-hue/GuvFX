"""
Admin interface for the WIMS Educational Content Flow (WP-1).

Designed to be *sufficient to demonstrate the workflow* end-to-end:
operators can create topics/contexts/contents inline, run the review and
publish actions, and inspect the immutable audit trail.
"""

from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from . import services
from .models import (
    AuditEvent,
    ConsumptionContract,
    Content,
    Context,
    EducationalTopic,
    Publish,
    Review,
)
from .services import workflow_state_for_contract, workflow_state_for_topic


class ContextInline(admin.TabularInline):
    model = Context
    extra = 0
    fields = ("context_text", "status", "created_by", "created_at")
    readonly_fields = ("created_at",)


@admin.register(EducationalTopic)
class EducationalTopicAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "workflow_state", "created_by", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "description")
    readonly_fields = ("created_at",)
    inlines = [ContextInline]

    @admin.display(description="Workflow state")
    def workflow_state(self, obj: EducationalTopic) -> str:
        return workflow_state_for_topic(obj)


@admin.register(ConsumptionContract)
class ConsumptionContractAdmin(admin.ModelAdmin):
    """Manual consumption-contract entry (WP-2 D5) + contract→context action (D2)."""

    list_display = (
        "id", "source_type", "symbol", "direction", "status",
        "workflow_state", "created_by", "created_at",
    )
    list_filter = ("status", "source_type", "direction")
    search_fields = ("symbol", "source_reference", "raw_signal")
    readonly_fields = ("created_at",)
    actions = ("action_generate_context",)

    @admin.display(description="Workflow state")
    def workflow_state(self, obj: ConsumptionContract) -> str:
        return workflow_state_for_contract(obj)

    @admin.action(description="Generate educational Context from selected contract(s)")
    def action_generate_context(self, request, queryset):
        done = 0
        for contract in queryset:
            try:
                services.create_context_from_contract(
                    contract=contract,
                    context_text=(
                        "Educational context (placeholder — edit before content). "
                        "Explain the market/instrument involved, what the order "
                        "direction means as a concept, and what stop-loss and "
                        "take-profit levels are. Neutral and educational only; no "
                        "trade recommendation or signal validation."
                    ),
                    actor=request.user,
                )
                done += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"Contract #{contract.pk}: {exc.messages[0]}",
                    level=messages.ERROR,
                )
        if done:
            self.message_user(
                request, f"Generated context for {done} contract(s).",
                level=messages.SUCCESS,
            )


@admin.register(Context)
class ContextAdmin(admin.ModelAdmin):
    list_display = ("id", "origin", "source", "contract", "status", "created_by", "created_at")
    list_filter = ("status",)
    search_fields = ("context_text",)
    readonly_fields = ("created_at",)

    @admin.display(description="Origin")
    def origin(self, obj: Context):
        return obj.origin


@admin.register(Content)
class ContentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "context", "created_by", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "content_text")
    readonly_fields = ("created_at",)
    actions = ("action_submit_for_review", "action_approve", "action_reject", "action_publish_telegram")

    def _run(self, request, queryset, fn, ok_msg):
        done = 0
        for content in queryset:
            try:
                fn(content, request.user)
                done += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"Content #{content.pk}: {exc.messages[0]}", level=messages.ERROR
                )
        if done:
            self.message_user(request, ok_msg.format(n=done), level=messages.SUCCESS)

    @admin.action(description="Submit selected content for review")
    def action_submit_for_review(self, request, queryset):
        self._run(
            request, queryset,
            lambda c, u: services.submit_for_review(content=c, actor=u),
            "{n} content item(s) submitted for review.",
        )

    @admin.action(description="Review → APPROVE selected content")
    def action_approve(self, request, queryset):
        self._run(
            request, queryset,
            lambda c, u: services.review_content(
                content=c, decision=Review.Decision.APPROVE, reviewer=u,
                notes="Approved via admin action.",
            ),
            "{n} content item(s) approved.",
        )

    @admin.action(description="Review → REJECT selected content")
    def action_reject(self, request, queryset):
        self._run(
            request, queryset,
            lambda c, u: services.review_content(
                content=c, decision=Review.Decision.REJECT, reviewer=u,
                notes="Rejected via admin action.",
            ),
            "{n} content item(s) rejected.",
        )

    @admin.action(description="Publish selected content → Telegram (simulated)")
    def action_publish_telegram(self, request, queryset):
        self._run(
            request, queryset,
            lambda c, u: services.publish_content(
                content=c, channel=Publish.Channel.TELEGRAM, publisher=u,
            ),
            "{n} content item(s) published to Telegram (simulated).",
        )


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "content", "review_decision", "reviewer", "review_timestamp")
    list_filter = ("review_decision",)
    readonly_fields = ("review_timestamp",)


@admin.register(Publish)
class PublishAdmin(admin.ModelAdmin):
    list_display = ("id", "content", "channel", "simulated", "published_by", "published_at")
    list_filter = ("channel", "simulated")
    readonly_fields = ("published_at",)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "event", "object_type", "object_id", "actor")
    list_filter = ("event", "object_type")
    search_fields = ("object_id",)
    readonly_fields = ("timestamp", "actor", "event", "object_type", "object_id", "detail")

    def has_add_permission(self, request):  # audit trail is append-only via services
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
