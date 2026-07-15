"""RX-2 Reliability Core — read + lifecycle APIs. Read-only except alert ack."""
from django.utils import timezone
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ComponentHealth, TradingHealthSnapshot, AlertEvent, RecoveryRecommendation, RecoveryAttempt, CircuitBreakerState
from .serializers import (
    ComponentHealthSerializer, TradingHealthSnapshotSerializer,
    AlertEventSerializer, RecoveryRecommendationSerializer, RecoveryAttemptSerializer,
)


def _latest(scope, **scope_filter):
    return (TradingHealthSnapshot.objects.filter(scope=scope, **scope_filter)
            .order_by("-computed_at").first())


class TradingHealthView(APIView):
    """GET /api/reliability/trading-health/  (GLOBAL by default)
       ?account_id=<id>  → per-account ; ?terminal=<node_id> → per-terminal."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account_id = request.query_params.get("account_id")
        terminal = request.query_params.get("terminal")
        if account_id:
            snap = _latest("ACCOUNT", trading_account_id=account_id)
        elif terminal:
            snap = _latest("TERMINAL", terminal_node_id=terminal)
        else:
            snap = _latest("GLOBAL")
        if not snap:
            return Response({"ok": False, "state": "UNKNOWN", "can_trade": False,
                             "reasons": ["No reliability data yet (reliability_tick not run / dormant)."]})
        data = TradingHealthSnapshotSerializer(snap).data
        data["ok"] = True
        return Response(data)


class HealthMatrixView(APIView):
    """GET /api/reliability/health/ — full component matrix + latest scoped snapshots."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        components = ComponentHealthSerializer(ComponentHealth.objects.all(), many=True).data
        global_snap = _latest("GLOBAL")
        return Response({
            "ok": True,
            "global": TradingHealthSnapshotSerializer(global_snap).data if global_snap else None,
            "components": components,
            "terminals": TradingHealthSnapshotSerializer(_distinct_latest("TERMINAL"), many=True).data,
            "accounts": TradingHealthSnapshotSerializer(_distinct_latest("ACCOUNT"), many=True).data,
        })


def _distinct_latest(scope):
    """Latest snapshot per scope-key (DB-agnostic; small row counts)."""
    seen, out = set(), []
    qs = TradingHealthSnapshot.objects.filter(scope=scope).order_by("-computed_at")
    key_field = "trading_account_id" if scope == "ACCOUNT" else "terminal_node_id"
    for s in qs:
        k = getattr(s, key_field)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


class AlertListView(APIView):
    """GET /api/reliability/alerts/?status=open|acknowledged|resolved"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = AlertEvent.objects.all()
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())
        return Response({"ok": True, "alerts": AlertEventSerializer(qs[:200], many=True).data})


class AlertAcknowledgeView(APIView):
    """POST /api/reliability/alerts/<id>/acknowledge/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            alert = AlertEvent.objects.get(pk=pk)
        except AlertEvent.DoesNotExist:
            return Response({"detail": "not_found"}, status=http.HTTP_404_NOT_FOUND)
        if alert.status == AlertEvent.Status.RESOLVED:
            return Response({"detail": "already_resolved"}, status=http.HTTP_409_CONFLICT)
        alert.status = AlertEvent.Status.ACKNOWLEDGED
        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = request.user if request.user.is_authenticated else None
        alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by"])
        return Response(AlertEventSerializer(alert).data)


class RecommendationListView(APIView):
    """GET /api/reliability/recommendations/?status=open"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = RecoveryRecommendation.objects.all()
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())
        return Response({"ok": True, "recommendations": RecoveryRecommendationSerializer(qs[:200], many=True).data})


class RecoveryAttemptListView(APIView):
    """GET /api/reliability/recovery-attempts/?shadow=true&policy=&limit="""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = RecoveryAttempt.objects.all()
        if request.query_params.get("policy"):
            qs = qs.filter(policy=request.query_params["policy"])
        sh = request.query_params.get("shadow")
        if sh is not None:
            qs = qs.filter(shadow=(sh.lower() == "true"))
        return Response({"ok": True, "attempts": RecoveryAttemptSerializer(qs[:200], many=True).data})


class RecoveryStatusView(APIView):
    """GET /api/reliability/recovery-status/ — RX-2G control surfaces."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .constants import auto_recovery_frozen, rx2g_enabled
        from .recovery import MarketStateService
        b = CircuitBreakerState.objects.filter(key="global").first()
        rows = list(ComponentHealth.objects.all())
        return Response({
            "ok": True,
            "auto_recovery_enabled": rx2g_enabled(),
            "auto_recovery_frozen": auto_recovery_frozen(),
            "live_actions_implemented": False,  # Phase 0: shadow-only by construction
            "market_state": MarketStateService.current(rows),
            "circuit": {
                "state": b.state if b else "CLOSED",
                "action_count": b.action_count if b else 0,
                "threshold": b.threshold if b else None,
                "window_s": b.window_s if b else None,
                "tripped_at": b.tripped_at if b else None,
            },
        })


class CircuitResetView(APIView):
    """POST /api/reliability/circuit/reset/ — manual breaker reset path."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .recovery import reset_breaker
        b = reset_breaker()
        return Response({"ok": True, "state": b.state, "reset_at": b.reset_at})


class HeartbeatIngestView(APIView):
    """POST /api/reliability/heartbeat/  — components (e.g. the non-Django
    validate worker) report liveness over HTTP. Worker-token or user auth.
    Observe-only; never affects trading or execution."""
    from execution.views import IsAuthenticatedOrWorkerToken as _WorkerOrAuth
    permission_classes = [_WorkerOrAuth]

    def post(self, request):
        source = str(request.data.get("source") or "").strip()
        if not source:
            return Response({"detail": "source required"}, status=http.HTTP_400_BAD_REQUEST)
        try:
            interval = int(request.data.get("expected_interval_s") or 60)
        except (TypeError, ValueError):
            interval = 60
        from .services.heartbeat import record_beat
        record_beat(source, interval_s=interval, detail={"via": "http"})
        return Response({"ok": True, "source": source, "expected_interval_s": interval})


class OperationsSummaryView(APIView):
    """GET /api/reliability/operations-summary/ — the single read-only operational status summary
    for the internal /operations page (health + source-aware strategy metrics + broker metrics +
    open positions/plans/candidates + dispatch + open alerts). Staff-only. Places NO order and
    mutates NOTHING (a best-effort bridge order_check for margin metrics never sends an order)."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        from .services.operations_summary import build_operations_summary
        return Response(build_operations_summary())
