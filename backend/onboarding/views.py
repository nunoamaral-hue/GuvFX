"""
Onboarding API views — step-based progression with backend-authoritative validation.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

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
    get_or_create_onboarding_state,
    setup_2fa,
    track_broker_referral,
    verify_2fa,
    verify_email_token,
)


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

        # TODO: Integrate email sending service here.
        # The plaintext token must be delivered via email, never in the API response.
        # Email delivery integration is a separate infrastructure task.
        _ = plaintext  # consumed by email sender when integrated

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
