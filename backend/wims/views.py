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
from .models import (
    AuditEvent,
    ConsumptionContract,
    Content,
    Context,
    EducationalTopic,
)
from .serializers import (
    AuditEventSerializer,
    ConsumptionContractSerializer,
    ContentSerializer,
    ContextSerializer,
    ContractToContextSerializer,
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


class ConsumptionContractViewSet(viewsets.ModelViewSet):
    """Manual consumption-contract entry + contract→context.

    Source-type agnostic: Wayond entry signals (WP-2) and external trade results
    (WP-3, ``source_type=TRADE_RESULT``). No automation/polling/webhooks —
    contracts are created by an operator from transient external intelligence.
    """

    queryset = ConsumptionContract.objects.all()
    serializer_class = ConsumptionContractSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        v = serializer.validated_data
        contract = _svc(
            services.create_contract,
            actor=self.request.user,
            source_type=v.get("source_type", ConsumptionContract.SourceType.WAYOND),
            source_reference=v.get("source_reference", ""),
            signal_type=v.get("signal_type", ""),
            symbol=v.get("symbol", ""),
            direction=v.get("direction", ""),
            entry_price=v.get("entry_price"),
            stop_loss=v.get("stop_loss"),
            take_profit=v.get("take_profit"),
            confidence=v.get("confidence"),
            raw_signal=v.get("raw_signal", ""),
            # WP-3 — trade-result fields
            exit_price=v.get("exit_price"),
            result_type=v.get("result_type", ""),
            profit_loss=v.get("profit_loss"),
            pips=v.get("pips"),
            close_time=v.get("close_time"),
            commentary=v.get("commentary", ""),
            tags=v.get("tags"),
        )
        serializer.instance = contract

    @action(detail=True, methods=["post"], url_path="generate-context")
    def generate_context(self, request, pk=None):
        payload = ContractToContextSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ctx = _svc(
            services.create_context_from_contract,
            contract=self.get_object(),
            context_text=payload.validated_data["context_text"],
            actor=request.user,
        )
        return Response(ContextSerializer(ctx).data, status=status.HTTP_201_CREATED)


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
