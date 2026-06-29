from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import log_event as audit_log_event
from .entitlements import resolve_entitlements
from .models import UserSubscriptionState, Invoice
from .serializers import (
    UserSubscriptionStateSerializer,
    EntitlementsSerializer,
    InvoiceSerializer,
)


class MySubscriptionView(APIView):
    """
    GET /api/billing/subscription/

    Returns the authenticated user's subscription state and computed
    entitlements.  If no UserSubscriptionState row exists, subscription
    is null but entitlements still resolve to safe viewer defaults.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            state = UserSubscriptionState.objects.get(user=request.user)
        except UserSubscriptionState.DoesNotExist:
            state = None

        sub_data = (
            UserSubscriptionStateSerializer(state).data if state is not None else None
        )
        ent_data = EntitlementsSerializer(resolve_entitlements(state)).data

        return Response({"subscription": sub_data, "entitlements": ent_data})


class SelectPlanView(APIView):
    """
    POST /api/billing/select-plan/

    Onboarding-driven plan selection.  Creates or updates the user's
    UserSubscriptionState so that the plan_selected onboarding gate
    can be satisfied without administrator intervention.

    Accepts: {"plan": "standard"|"starter_trial"|"pro"|"advanced"}
    """

    permission_classes = [permissions.IsAuthenticated]

    # Plans a user is allowed to self-select during onboarding.
    ALLOWED_PLANS = {
        UserSubscriptionState.Plan.STARTER_TRIAL,
        UserSubscriptionState.Plan.STANDARD,
    }

    def post(self, request):
        plan = (request.data.get("plan") or "").strip().lower()

        valid_plans = {p.value for p in self.ALLOWED_PLANS}
        if plan not in valid_plans:
            return Response(
                {
                    "ok": False,
                    "error": "invalid_plan",
                    "detail": f"Allowed plans: {sorted(valid_plans)}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        sub, created = UserSubscriptionState.objects.get_or_create(
            user=request.user,
            defaults={
                "current_plan": plan,
                "plan_status": UserSubscriptionState.PlanStatus.ACTIVE,
                "viewer_mode": False,
                "billing_cycle": UserSubscriptionState.BillingCycle.MONTHLY,
                "current_period_started_at": now,
            },
        )

        if not created:
            # Update existing — only if the user hasn't already selected this plan
            sub.current_plan = plan
            sub.plan_status = UserSubscriptionState.PlanStatus.ACTIVE
            sub.viewer_mode = False
            sub.billing_cycle = sub.billing_cycle or UserSubscriptionState.BillingCycle.MONTHLY
            sub.current_period_started_at = sub.current_period_started_at or now
            sub.save(update_fields=[
                "current_plan", "plan_status", "viewer_mode",
                "billing_cycle", "current_period_started_at", "updated_at",
            ])

        audit_log_event(
            request=request,
            event_type="PLAN_SELECTED",
            severity="INFO",
            entity_type="user_subscription",
            entity_id=str(sub.pk),
            metadata={"plan": plan, "created": created},
        )

        sub_data = UserSubscriptionStateSerializer(sub).data
        ent_data = EntitlementsSerializer(resolve_entitlements(sub)).data

        return Response(
            {"ok": True, "subscription": sub_data, "entitlements": ent_data},
            status=status.HTTP_200_OK,
        )


class MyInvoicesView(APIView):
    """
    GET /api/billing/invoices/

    Returns all invoices for the authenticated user, ordered by most
    recent issue_date first. User can only see their own invoice rows.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invoices = (
            Invoice.objects.filter(user=request.user)
            .order_by("-issue_date")
        )
        serializer = InvoiceSerializer(invoices, many=True)
        return Response({"invoices": serializer.data})
