from django.conf import settings
from django.db import transaction

from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    HostingProvider,
    VpsPlan,
    VpsInstance,
    Mt5Instance,
    UserHostingSubscription,
    HostingRequest,
)
from .serializers import (
    HostingProviderSerializer,
    VpsPlanSerializer,
    VpsInstanceSerializer,
    Mt5InstanceSerializer,
    UserHostingSubscriptionMeSerializer,
    HostingRequestSerializer,
)


class IsStaffAdmin(permissions.BasePermission):
    """
    Allow access only to staff users.
    For now this is a simple staff guard; later you can do more granular roles.
    """

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)


class HostingProviderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = HostingProvider.objects.all().order_by("name")
    serializer_class = HostingProviderSerializer
    permission_classes = [IsStaffAdmin]


class VpsPlanViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = VpsPlan.objects.all().order_by("provider__name", "name")
    serializer_class = VpsPlanSerializer
    permission_classes = [permissions.IsAuthenticated]


class VpsInstanceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        VpsInstance.objects.select_related("provider", "plan")
        .all()
        .order_by("provider__name", "hostname", "public_ip")
    )
    serializer_class = VpsInstanceSerializer
    permission_classes = [IsStaffAdmin]


class Mt5InstanceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        Mt5Instance.objects.select_related("vps", "owner")
        .all()
        .order_by("-created_at")
    )
    serializer_class = Mt5InstanceSerializer
    permission_classes = [IsStaffAdmin]


class MyHostingView(APIView):
    """
    Returns hosting subscriptions for the current user.
    Read-only, user-scoped view.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (
            UserHostingSubscription.objects.filter(user=request.user)
            .select_related("plan", "plan__provider", "vps", "mt5_instance")
            .order_by("created_at")
        )
        serializer = UserHostingSubscriptionMeSerializer(qs, many=True)
        return Response({"subscriptions": serializer.data})


def build_guac_url(connection_id: str) -> str:
    """
    Build a full Guacamole client URL using GUAC_BASE_URL and the given connection/client id.
    """
    base = getattr(settings, "GUAC_BASE_URL", "").rstrip("/")
    return f"{base}/#/client/{connection_id}"


class MyConsolesView(APIView):
    """
    Return the list of hosted consoles (VPS instances with Guacamole connection IDs)
    that belong to the authenticated user.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        qs = (
            VpsInstance.objects.filter(subscriptions__user=user)
            .select_related("plan")
            .order_by("id")
            .distinct()
        )

        consoles = []
        for vps in qs:
            if not vps.guac_connection_id:
                continue

            plan = getattr(vps, "plan", None)
            plan_code = getattr(plan, "code", None)
            plan_name = getattr(plan, "name", "") if plan else ""

            label = getattr(vps, "display_name", None) or getattr(
                vps, "hostname", None
            ) or f"VPS #{vps.pk}"

            consoles.append(
                {
                    "vps_id": vps.pk,
                    "vps_label": label,
                    "plan_code": plan_code,
                    "plan_name": plan_name,
                    "guac_url": build_guac_url(vps.guac_connection_id),
                    "status": getattr(vps, "status", "UNKNOWN"),
                }
            )

        return Response(consoles)


class HostingRequestViewSet(viewsets.ModelViewSet):
    """
    Allows users to create hosting requests.

    - Normal users: can create & list their own requests.
    - Staff users: can see all requests.
    """

    serializer_class = HostingRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = HostingRequest.objects.select_related("owner").order_by("-created_at")
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return qs
        return qs.filter(owner=user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @decorators.action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
    )
    @transaction.atomic
    def approve(self, request, pk=None):
        hosting_request = self.get_object()
        if hosting_request.status != HostingRequest.Status.PENDING:
            return response.Response(
                {"detail": "Request is not pending."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plan_id = request.data.get("plan")
        plan = None
        if plan_id:
            try:
                plan = VpsPlan.objects.get(id=plan_id)
            except VpsPlan.DoesNotExist:
                return response.Response(
                    {"detail": "Plan not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if plan is None:
            plan = VpsPlan.objects.order_by("id").first()
        if plan is None:
            return response.Response(
                {"detail": "No VPS plan configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        subscription = UserHostingSubscription.objects.create(
            user=hosting_request.owner,
            plan=plan,
            billing_status=UserHostingSubscription.STATUS_ACTIVE,
        )

        hosting_request.status = HostingRequest.Status.APPROVED
        hosting_request.save(update_fields=["status", "updated_at"])

        return response.Response(
            {
                "request": HostingRequestSerializer(hosting_request).data,
                "subscription_id": subscription.id,
            },
            status=status.HTTP_200_OK,
        )

    @decorators.action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
    )
    @transaction.atomic
    def reject(self, request, pk=None):
        hosting_request = self.get_object()
        if hosting_request.status != HostingRequest.Status.PENDING:
            return response.Response(
                {"detail": "Request is not pending."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note = request.data.get("note", "").strip()
        if note:
            hosting_request.note = note
        hosting_request.status = HostingRequest.Status.REJECTED
        hosting_request.save(update_fields=["status", "note", "updated_at"])

        return response.Response(
            HostingRequestSerializer(hosting_request).data,
            status=status.HTTP_200_OK,
        )
