"""
Minimal DRF API for the WIMS Educational Content Flow (WP-1).

Endpoints are intentionally thin wrappers over ``wims.services`` so that the
status-transition + audit guarantees enforced there apply uniformly to the API
and the admin. Read uses the model serializers; every *write that changes
state* routes through a service function.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import services
from .models import AuditEvent, Content, Context, EducationalTopic
from .serializers import (
    AuditEventSerializer,
    ContentSerializer,
    ContextSerializer,
    EducationalTopicSerializer,
    PublishActionSerializer,
    PublishSerializer,
    ReviewActionSerializer,
    ReviewSerializer,
)


def _svc(fn, **kwargs):
    """Run a service call, translating Django ValidationError -> DRF 400."""
    try:
        return fn(**kwargs)
    except DjangoValidationError as exc:
        raise DRFValidationError(exc.messages)


class EducationalTopicViewSet(viewsets.ModelViewSet):
    queryset = EducationalTopic.objects.all()
    serializer_class = EducationalTopicSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        topic = _svc(
            services.create_topic,
            title=serializer.validated_data["title"],
            description=serializer.validated_data.get("description", ""),
            actor=self.request.user,
        )
        serializer.instance = topic


class ContextViewSet(viewsets.ModelViewSet):
    queryset = Context.objects.all()
    serializer_class = ContextSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        ctx = _svc(
            services.create_context,
            topic=serializer.validated_data["source"],
            context_text=serializer.validated_data["context_text"],
            actor=self.request.user,
        )
        serializer.instance = ctx


class ContentViewSet(viewsets.ModelViewSet):
    queryset = Content.objects.all()
    serializer_class = ContentSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        content = _svc(
            services.create_content,
            context=serializer.validated_data["context"],
            title=serializer.validated_data["title"],
            content_text=serializer.validated_data["content_text"],
            actor=self.request.user,
        )
        serializer.instance = content

    @action(detail=True, methods=["post"], url_path="submit-for-review")
    def submit_for_review(self, request, pk=None):
        content = _svc(services.submit_for_review,
                       content=self.get_object(), actor=request.user)
        return Response(ContentSerializer(content).data)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        payload = ReviewActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        review = _svc(
            services.review_content,
            content=self.get_object(),
            decision=payload.validated_data["decision"],
            reviewer=request.user,
            notes=payload.validated_data["notes"],
        )
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        payload = PublishActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        pub = _svc(
            services.publish_content,
            content=self.get_object(),
            channel=payload.validated_data["channel"],
            publisher=request.user,
        )
        return Response(PublishSerializer(pub).data, status=status.HTTP_201_CREATED)


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AuditEvent.objects.all()
        object_type = self.request.query_params.get("object_type")
        object_id = self.request.query_params.get("object_id")
        if object_type:
            qs = qs.filter(object_type=object_type)
        if object_id:
            qs = qs.filter(object_id=object_id)
        return qs
