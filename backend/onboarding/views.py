"""
Onboarding API views — step-based progression with backend-authoritative validation.
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from billing.entitlements import MarketplaceCatalogue

from .emails import send_verification_email
from .models import BrokerPartner
from .serializers import (
    BrokerPartnerSerializer,
    BrokerReferralSerializer,
    CompleteStepSerializer,
    EmailVerifySerializer,
    OnboardingStateSerializer,
    TwoFactorVerifySerializer,
)
from .services import (
    OnboardingStepError,
    check_onboarding_permits_execution,
    complete_step,
    create_email_verification_token,
    finalize_onboarding,
    get_or_create_onboarding_state,
    resolve_setup_stage,
    setup_2fa,
    track_broker_referral,
    verify_2fa,
    verify_email_token,
)

logger = logging.getLogger(__name__)


class OnboardingStateView(APIView):
    """GET /api/onboarding/state/ — current onboarding state."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        state = get_or_create_onboarding_state(request.user)
        serializer = OnboardingStateSerializer(state)
        return Response(serializer.data)


class CompleteStepView(APIView):
    """POST /api/onboarding/complete-step/ — advance a step."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CompleteStepSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            state = complete_step(
                request.user,
                step=serializer.validated_data["step"],
                request=request,
            )
        except OnboardingStepError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)

        output = OnboardingStateSerializer(state)
        return Response(output.data)


class EmailSendVerificationView(APIView):
    """POST /api/onboarding/email/send-verification/ — generate token."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        state = get_or_create_onboarding_state(request.user)
        if state.email_verified:
            return Response(
                {"detail": "Email already verified."},
                status=status.HTTP_200_OK,
            )

        plaintext = create_email_verification_token(request.user)

        # Deliver the code via email (Google Workspace SMTP, env-configured). The token
        # is NEVER returned in the API response. If transport fails we return a truthful
        # error rather than the old stub's false "email sent".
        try:
            send_verification_email(request.user, plaintext)
        except Exception:  # noqa: BLE001 — any transport/auth error → honest 502
            logger.exception("verification email send failed for user_id=%s", request.user.id)
            return Response(
                {"detail": "We couldn't send the verification email right now. "
                           "Please try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {"detail": "Verification email sent. Check your inbox."},
            status=status.HTTP_201_CREATED,
        )


class EmailVerifyView(APIView):
    """POST /api/onboarding/email/verify/ — verify token."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = EmailVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            verify_email_token(
                request.user,
                plaintext_token=serializer.validated_data["token"],
                request=request,
            )
        except OnboardingStepError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)

        state = get_or_create_onboarding_state(request.user)
        output = OnboardingStateSerializer(state)
        return Response(output.data)


class TwoFactorSetupView(APIView):
    """POST /api/onboarding/2fa/setup/ — generate TOTP secret."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        state = get_or_create_onboarding_state(request.user)
        if state.two_factor_enabled:
            return Response(
                {"detail": "2FA already enabled."},
                status=status.HTTP_200_OK,
            )

        result = setup_2fa(request.user)
        # Return provisioning URI + secret for authenticator app setup.
        # Secret is shown ONCE. After verification, it is never exposed again.
        return Response(
            {
                "provisioning_uri": result["provisioning_uri"],
                "secret": result["secret"],
            },
            status=status.HTTP_201_CREATED,
        )


class TwoFactorVerifyView(APIView):
    """POST /api/onboarding/2fa/verify/ — verify OTP to enable 2FA."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TwoFactorVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            verify_2fa(
                request.user,
                otp_code=serializer.validated_data["otp_code"],
                request=request,
            )
        except OnboardingStepError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)

        state = get_or_create_onboarding_state(request.user)
        output = OnboardingStateSerializer(state)
        return Response(output.data)


class RiskAcceptView(APIView):
    """POST /api/onboarding/risk/accept/ — accept risk disclosure."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .services import accept_risk

        try:
            state = accept_risk(request.user, request=request)
        except OnboardingStepError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)

        output = OnboardingStateSerializer(state)
        return Response(output.data)


class BrokerPartnerListView(APIView):
    """GET /api/onboarding/brokers/ — list active broker partners."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partners = BrokerPartner.objects.filter(is_active=True)
        serializer = BrokerPartnerSerializer(partners, many=True)
        return Response(serializer.data)


class BrokerReferralView(APIView):
    """POST /api/onboarding/brokers/referral/ — track referral click."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BrokerReferralSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            referral = track_broker_referral(
                request.user,
                broker_code=serializer.validated_data["broker_code"],
                referral_code=serializer.validated_data.get("referral_code", ""),
                request=request,
            )
        except OnboardingStepError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)

        return Response(
            {
                "broker_code": referral.broker_partner.broker_code,
                "clicked_at": referral.clicked_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class ExecutionReadinessView(APIView):
    """GET /api/onboarding/readiness/ — check onboarding gating for execution."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        result = check_onboarding_permits_execution(request.user)
        return Response(result)


class AccountStatusView(APIView):
    """GFX-BETA-PHASE0 Increment 3 — GET /api/onboarding/account-status/?account_id=<id>

    Truthful per-account status panel. Account-owner scoped (staff bypass). Runtime/terminal stages
    reflect the durable AccountRuntime state and NEVER imply a live MT5 terminal while automatic
    provisioning is undeployed (they read NOT_CONFIGURED). Read-only; never creates a runtime row.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from trading.models import TradingAccount
        from terminal_provisioning.account_status import build_account_status

        account_id = request.query_params.get("account_id")
        qs = TradingAccount.objects.all()
        if not request.user.is_staff:
            qs = qs.filter(user=request.user)  # a user only sees their own accounts
        if account_id:
            try:
                acct = qs.filter(id=int(account_id)).first()
            except (TypeError, ValueError):
                return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        else:
            acct = qs.filter(user=request.user).order_by("id").first()  # the caller's primary account
        if acct is None:
            return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"ok": True, **build_account_status(acct)})


class OnboardingCompleteView(APIView):
    """POST /api/onboarding/complete/ — finalize onboarding (idempotent).

    The 'finish setup' action the final onboarding screen calls once the minimum required steps
    (email + plan + risk) are done. Broker connection and strategy assignment are POST-onboarding platform
    setup, not prerequisites here. Returns the onboarding state PLUS the resolved next setup stage/route so
    the UI can hand off directly into platform setup.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        state = finalize_onboarding(request.user, request=request)
        data = OnboardingStateSerializer(state).data
        return Response({**data, "setup": resolve_setup_stage(request.user)})


class SetupStatusView(APIView):
    """GET /api/onboarding/setup-status/ — the intelligent setup router.

    Returns the customer's current post-onboarding setup stage and the route that resumes it (see
    ``services.resolve_setup_stage``): onboarding -> connect_broker -> provisioning -> select_strategy ->
    enable_trading -> complete. Onboarding completion and platform-setup completion stay separate.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(resolve_setup_stage(request.user))


class BetaMarketplaceView(APIView):
    """GET /api/onboarding/marketplace/ — the onboarding strategy marketplace.

    ADR-0021 Visibility layer: the entitlement layer OWNS which marketplace CATALOGUES a customer may
    browse. Each item declares its enduring ``catalogue`` (not a rollout phase); this view NEVER evaluates
    an entitlement boolean — it asks the entitlement for the permitted catalogues and renders the items
    whose catalogue is in that set. Visibility is deliberately SEPARATE from ACTIVATION: each item carries
    a truthful ``available``/``provisioning_available`` (gated by the onboarding-open + provisioning
    state), so the UI can render "coming soon", never an "activate now" the customer cannot use.
    """
    permission_classes = [IsAuthenticated]

    # Marketplace items declare the catalogue they belong to (the view owns CONTENT; the entitlement layer
    # owns the visibility POLICY).
    _MARKETPLACE_ITEMS = [
        {"key": "wayond_auto_demo", "name": "Wayond Auto Demo",
         "description": "Copies the Wayond demo signal feed.",
         "catalogue": MarketplaceCatalogue.SIGNAL_COPY},
        {"key": "wayond_wim", "name": "Wayond WIM Strategy",
         "description": "Copies the TI Signals feed (WIM).",
         "catalogue": MarketplaceCatalogue.SIGNAL_COPY},
    ]

    def get(self, request):
        from billing.entitlements import ALL_MARKETPLACE_CATALOGUES, resolve_entitlements
        from billing.models import UserSubscriptionState
        from billing.beta import beta_onboarding_open

        ent = resolve_entitlements(UserSubscriptionState.objects.filter(user=request.user).first())
        # VISIBILITY (entitlement-owned): which catalogues may this customer browse? Staff see all (an
        # admin/auth override, not an entitlement rule). No entitlement boolean is evaluated here.
        browsable = ALL_MARKETPLACE_CATALOGUES if request.user.is_staff else ent.visible_marketplace_catalogues
        visible = [item for item in self._MARKETPLACE_ITEMS if item["catalogue"] in browsable]
        if not visible:
            return Response({"ok": True, "entitled": False, "onboarding_open": False, "strategies": []})

        available = beta_onboarding_open()  # ACTIVATION layer — separate from visibility
        strategies = [{
            **s,
            "available": available,             # truthful: not activatable yet
            "activation_available": available,
            "provisioning_available": False,    # per-user provisioning is undeployed (Phase 2)
            "reason": None if available else "Not available yet — onboarding is not open.",
        } for s in visible]
        return Response({"ok": True, "entitled": True, "onboarding_open": available,
                         "strategies": strategies})
