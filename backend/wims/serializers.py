from rest_framework import serializers

from .models import (
    AuditEvent,
    Content,
    Context,
    EducationalTopic,
    Publish,
    Review,
)
from .services import workflow_state_for_topic


class EducationalTopicSerializer(serializers.ModelSerializer):
    workflow_state = serializers.SerializerMethodField()

    class Meta:
        model = EducationalTopic
        fields = (
            "id", "title", "description", "status",
            "workflow_state", "created_by", "created_at",
        )
        read_only_fields = ("created_by", "created_at", "status")

    def get_workflow_state(self, obj) -> str:
        return workflow_state_for_topic(obj)


class ContextSerializer(serializers.ModelSerializer):
    class Meta:
        model = Context
        fields = (
            "id", "source", "context_text", "status",
            "created_by", "created_at",
        )
        read_only_fields = ("created_by", "created_at", "status")


class ContentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Content
        fields = (
            "id", "context", "title", "content_text", "status",
            "created_by", "created_at",
        )
        read_only_fields = ("created_by", "created_at", "status")


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = (
            "id", "content", "reviewer", "review_decision",
            "review_notes", "review_timestamp",
        )
        read_only_fields = ("reviewer", "review_timestamp")


class PublishSerializer(serializers.ModelSerializer):
    class Meta:
        model = Publish
        fields = (
            "id", "content", "channel", "simulated",
            "published_by", "published_at",
        )
        read_only_fields = ("published_by", "published_at", "simulated")


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = (
            "id", "timestamp", "actor", "event",
            "object_type", "object_id", "detail",
        )
        read_only_fields = fields


# --- action payloads -------------------------------------------------------
class ReviewActionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=Review.Decision.choices)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PublishActionSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=Publish.Channel.choices)
