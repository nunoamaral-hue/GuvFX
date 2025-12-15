from django.db.models import Q
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import BrokerServer


class BrokerServerSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrokerServer
        fields = ["id", "broker_display_name", "server_name", "environment"]


class BrokerServerViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Router endpoints:
      - GET /api/trading/broker-servers/
      - GET /api/trading/broker-servers/suggest/?q=...&demo=true|false
    """
    queryset = BrokerServer.objects.filter(is_active=True)
    serializer_class = BrokerServerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        demo = (self.request.query_params.get("demo") or "").strip().lower()
        if demo:
            is_demo = demo in ("1", "true", "yes", "y")
            env = BrokerServer.DEMO if is_demo else BrokerServer.LIVE
            qs = qs.filter(environment=env)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(server_name__icontains=q) | Q(broker_display_name__icontains=q)
            )

        return qs

    @action(detail=False, methods=["get"], url_path="suggest")
    def suggest(self, request):
        qs = self.get_queryset().order_by("broker_display_name", "server_name")[:10]
        return Response(self.get_serializer(qs, many=True).data)