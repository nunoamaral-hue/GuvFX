"""RX-2 Reliability Core — read + lifecycle APIs. Read-only except alert ack."""
from django.utils import timezone
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ComponentHealth, TradingHealthSnapshot, AlertEvent, RecoveryRecommendation, RecoveryAttempt, CircuitBreakerState
from trading.models import TradingAccount
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
        is_staff = request.user.is_staff
        # GFX-BETA-PHASE0 (C14 + IDOR): a non-staff user may only read their OWN account's health.
        # GLOBAL/operator health and arbitrary terminals are staff-only — never show a beta user the
        # estate's health as if it were their own, and never let them probe another account_id.
        if account_id:
            if not is_staff and not TradingAccount.objects.filter(
                    id=account_id, user=request.user).exists():
                return Response({"ok": False, "state": "UNKNOWN", "can_trade": False,
                                 "reasons": ["Account not found."]}, status=http.HTTP_404_NOT_FOUND)
            snap = _latest("ACCOUNT", trading_account_id=account_id)
        elif terminal:
            if not is_staff:
                return Response({"ok": False, "state": "UNKNOWN", "can_trade": False,
                                 "reasons": ["Provide your own account_id."]})
            snap = _latest("TERMINAL", terminal_node_id=terminal)
        elif not is_staff:
            # non-staff, no account_id → do NOT fall back to GLOBAL operator health.
            return Response({"ok": False, "state": "UNKNOWN", "can_trade": False,
                             "reasons": ["Provide your own account_id."]})
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
    permission_classes = [IsAdminUser]

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
        # GFX-BETA-PHASE0 (C15): non-staff users see ONLY alerts for their own trading accounts.
        # Operator/GLOBAL alerts (trading_account is NULL) and other users' alerts stay staff-only —
        # otherwise a beta user would see the whole estate's operational state.
        if not request.user.is_staff:
            qs = qs.filter(trading_account__user=request.user)
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())
        return Response({"ok": True, "alerts": AlertEventSerializer(qs[:200], many=True).data})


class AlertAcknowledgeView(APIView):
    """POST /api/reliability/alerts/<id>/acknowledge/ — staff-only (D5 hardening)."""
    permission_classes = [IsAdminUser]

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


class AssignmentSetActiveView(APIView):
    """D4 — the ONLY execution-adjacent staff action on the ops page: pause / re-enable an
    already-armed SOURCE-BOUND strategy assignment. POST body ``{"active": bool}``.

    Deliberately narrow + safe by construction: it flips ONLY ``StrategyAssignment.is_active`` and
    NOTHING else — never ``execution_mode``, never ``SignalSourceConfig`` lots, never
    ``ExecutionControl.auto_execution_enabled`` / ``signal_execution_mode`` / the kill switch. It
    therefore cannot place an order, resize, or change the global arming — pausing a source just
    removes it from routing (matches "disarm = pause the assignment"). Only assignments with a
    non-empty ``signal_source`` are togglable (so you cannot touch a non-source strategy). Audited.
    Enabling (active=True) is the Amber direction — it re-includes a source in routing."""
    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        from strategies.models import StrategyAssignment
        active = request.data.get("active")
        if not isinstance(active, bool):
            return Response({"detail": "body must include boolean 'active'"},
                            status=http.HTTP_400_BAD_REQUEST)
        try:
            asn = StrategyAssignment.objects.select_related("account").get(pk=pk)
        except StrategyAssignment.DoesNotExist:
            return Response({"detail": "not_found"}, status=http.HTTP_404_NOT_FOUND)
        if not getattr(asn, "signal_source", ""):
            return Response({"detail": "not a source-bound assignment"},
                            status=http.HTTP_400_BAD_REQUEST)
        # Mirror model.clean(): never activate an assignment on an inactive account.
        if active and asn.account and not getattr(asn.account, "is_active", True):
            return Response({"detail": "cannot enable on an inactive account"},
                            status=http.HTTP_409_CONFLICT)
        asn.is_active = active
        asn.save(update_fields=["is_active"])
        try:
            from core.audit import log_event
            log_event(request=request, event_type="ASSIGNMENT_SET_ACTIVE", severity="INFO",
                      entity_type="strategy_assignment", entity_id=str(asn.id),
                      metadata={"active": active, "signal_source": asn.signal_source})
        except Exception:
            pass
        return Response({"id": asn.id, "signal_source": asn.signal_source, "is_active": asn.is_active})


class RecommendationListView(APIView):
    """GET /api/reliability/recommendations/?status=open"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = RecoveryRecommendation.objects.all()
        # GFX-BETA-PHASE0 (C15): non-staff users see ONLY recommendations for their own trading
        # accounts. Operator/GLOBAL recommendations (trading_account NULL) stay staff-only.
        if not request.user.is_staff:
            qs = qs.filter(trading_account__user=request.user)
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())
        return Response({"ok": True, "recommendations": RecoveryRecommendationSerializer(qs[:200], many=True).data})


class RecoveryAttemptListView(APIView):
    """GET /api/reliability/recovery-attempts/?shadow=true&policy=&limit="""
    permission_classes = [IsAdminUser]

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
    permission_classes = [IsAdminUser]

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
    permission_classes = [IsAdminUser]

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
