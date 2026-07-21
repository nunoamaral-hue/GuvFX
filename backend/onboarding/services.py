"""
Onboarding service layer — step validation, progression, and readiness gating.

This service is orchestration-only:
- It does NOT duplicate billing logic (delegates to billing.entitlements).
- It does NOT mutate execution readiness (reads existing state only).
- It does NOT store credentials or secrets (delegates to crypto/models).
"""
from __future__ import annotations

import logging
from typing import Optional

import pyotp
from django.utils import timezone

from billing.entitlements import resolve_entitlements
from billing.models import UserSubscriptionState
from core.audit import (
    log_onboarding_2fa_enabled,
    log_onboarding_account_connected,
    log_onboarding_broker_referral,
    log_onboarding_completed,
    log_onboarding_email_verified,
    log_onboarding_plan_selected,
    log_onboarding_risk_accepted,
    log_onboarding_strategy_assigned,
)
from execution.models import TerminalNode
from strategies.models import StrategyAssignment
from trading.crypto import decrypt_password, encrypt_password

logger = logging.getLogger(__name__)
from trading.models import TradingAccount

from .models import (
    BrokerPartner,
    EmailVerificationToken,
    TwoFactorSecret,
    UserBrokerReferral,
    UserOnboardingState,
)


# ─────────────────────────────────────────────────────────────────────
# Step ordering — defines valid transitions
# ─────────────────────────────────────────────────────────────────────

# Steps in canonical order, aligned with the 5-step frontend model:
#
#   Step 1: Create account   — handled by /register (no flag here)
#   Step 2: Select plan      — plan_selected (no prerequisites)
#   Step 3: Complete profile  — email_verified, 2FA (optional), risk_accepted
#   Step 4: Connect broker   — account_connected
#   Step 5: Get started      — strategy_assigned
#
# The _check_prerequisites function enforces this linear ordering:
# each step requires all prior REQUIRED steps to be complete.
# 2FA is optional and never blocks progression.
STEP_ORDER = [
    "plan_selected",
    "email_verified",
    "two_factor_enabled",  # optional — never blocks
    "risk_accepted",
    "account_connected",
    "strategy_assigned",
]

REQUIRED_STEPS = {
    "email_verified",
    "risk_accepted",
    "plan_selected",
    "account_connected",
    "strategy_assigned",
}

OPTIONAL_STEPS = {"two_factor_enabled"}


class OnboardingStepError(Exception):
    """Raised when an invalid onboarding step transition is attempted."""
    pass


# ─────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────

def get_or_create_onboarding_state(user) -> UserOnboardingState:
    """Get or create the onboarding state for a user."""
    state, _ = UserOnboardingState.objects.get_or_create(user=user)
    _apply_beta_admission(user, state)
    return state


def _apply_beta_admission(user, state) -> None:
    """CVM controlled-beta admission: for an allowlisted beta identity ONLY, admission REPLACES email
    verification (Nuno: do not require email-verify/2FA for the controlled identity) and ensures beta
    entitlement. Strictly per-identity and idempotent — the side-effects run once (when email_verified
    first flips), and non-allowlisted users are never touched (email verification still required for
    them, so public onboarding stays closed). Does NOT open the onboarding gate globally."""
    from django.db import transaction

    from billing.beta import grant_beta_entitlement, is_admitted_beta_tester
    # Estate-safety (defense-in-depth): NEVER apply beta admission to a staff/superuser (Nuno) account,
    # even if their email were mistakenly allowlisted — his estate must stay untouched.
    if state.email_verified or user.is_staff or user.is_superuser or not is_admitted_beta_tester(user):
        return
    # Commit the email-verified flip and the entitlement grant together, so a partial failure can never
    # leave the tester verified-but-un-entitled (which the run-once guard would then never self-heal).
    with transaction.atomic():
        state.email_verified = True
        state.save(update_fields=["email_verified", "updated_at"])
        grant_beta_entitlement(user)


def get_onboarding_state_dict(state: UserOnboardingState) -> dict:
    """Return the onboarding state as a serializable dict."""
    return {
        "email_verified": state.email_verified,
        "two_factor_enabled": state.two_factor_enabled,
        "risk_accepted": state.risk_accepted,
        "plan_selected": state.plan_selected,
        "account_connected": state.account_connected,
        "strategy_assigned": state.strategy_assigned,
        "onboarding_completed": state.onboarding_completed,
        "risk_accepted_at": state.risk_accepted_at.isoformat() if state.risk_accepted_at else None,
        "onboarding_completed_at": state.onboarding_completed_at.isoformat() if state.onboarding_completed_at else None,
    }


def _check_prerequisites(state: UserOnboardingState, step: str) -> None:
    """
    Validate that all prerequisite steps are complete before allowing progression.
    Raises OnboardingStepError on invalid transition.
    """
    step_idx = STEP_ORDER.index(step) if step in STEP_ORDER else -1
    if step_idx < 0:
        raise OnboardingStepError(f"Unknown step: {step}")

    for i in range(step_idx):
        prior_step = STEP_ORDER[i]
        # Optional steps (2FA) don't block progression
        if prior_step in OPTIONAL_STEPS:
            continue
        if not getattr(state, prior_step, False):
            raise OnboardingStepError(
                f"Step '{step}' requires '{prior_step}' to be completed first."
            )


def _check_completion(state: UserOnboardingState) -> bool:
    """Check if all required steps are complete and mark onboarding as completed."""
    all_done = all(getattr(state, step, False) for step in REQUIRED_STEPS)
    if all_done and not state.onboarding_completed:
        state.onboarding_completed = True
        state.onboarding_completed_at = timezone.now()
        state.save(update_fields=["onboarding_completed", "onboarding_completed_at", "updated_at"])
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Email verification
# ─────────────────────────────────────────────────────────────────────

def create_email_verification_token(user) -> str:
    """
    Create a new email verification token for the user.
    Returns the plaintext token (to be sent via email).
    The plaintext is NEVER persisted.
    """
    # Invalidate any existing unused tokens
    EmailVerificationToken.objects.filter(user=user, used=False).update(used=True)

    _token_obj, plaintext = EmailVerificationToken.create_for_user(user)
    return plaintext


def verify_email_token(user, plaintext_token: str, request=None) -> bool:
    """
    Verify an email token and update onboarding state.
    Returns True on success.
    Raises OnboardingStepError on failure.
    """
    token_hash = EmailVerificationToken.hash_token(plaintext_token)
    try:
        token = EmailVerificationToken.objects.get(
            user=user, token_hash=token_hash
        )
    except EmailVerificationToken.DoesNotExist:
        raise OnboardingStepError("Invalid verification token.")

    if token.used:
        raise OnboardingStepError("Token has already been used.")

    if token.is_expired:
        raise OnboardingStepError("Token has expired. Request a new one.")

    # Mark used
    token.used = True
    token.save(update_fields=["used"])

    # Update onboarding state
    state = get_or_create_onboarding_state(user)
    if not state.email_verified:
        state.email_verified = True
        state.save(update_fields=["email_verified", "updated_at"])
        log_onboarding_email_verified(request, user.id)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return True


# ─────────────────────────────────────────────────────────────────────
# 2FA (TOTP)
# ─────────────────────────────────────────────────────────────────────

def setup_2fa(user) -> dict:
    """
    Generate a TOTP secret for the user. Returns provisioning info.
    Secret is encrypted with Fernet (same subsystem as TradingAccount.password_enc).
    """
    raw_secret = pyotp.random_base32()

    # Encrypt before storing
    secret_enc = encrypt_password(raw_secret)

    TwoFactorSecret.objects.update_or_create(
        user=user,
        defaults={"secret_enc": secret_enc, "is_verified": False},
    )

    totp = pyotp.TOTP(raw_secret)
    provisioning_uri = totp.provisioning_uri(
        name=user.email,
        issuer_name="GuvFX",
    )

    return {
        "provisioning_uri": provisioning_uri,
        "secret": raw_secret,  # Shown once to user for manual entry
    }


def verify_2fa(user, otp_code: str, request=None) -> bool:
    """
    Verify TOTP code and enable 2FA on the onboarding state.
    Raises OnboardingStepError on failure.
    """
    try:
        tfa = TwoFactorSecret.objects.get(user=user)
    except TwoFactorSecret.DoesNotExist:
        raise OnboardingStepError("2FA not set up. Call setup first.")

    raw_secret = decrypt_password(tfa.secret_enc)
    totp = pyotp.TOTP(raw_secret)

    if not totp.verify(otp_code, valid_window=1):
        raise OnboardingStepError("Invalid OTP code.")

    # Mark verified
    if not tfa.is_verified:
        tfa.is_verified = True
        tfa.save(update_fields=["is_verified", "updated_at"])

    # Update onboarding state
    state = get_or_create_onboarding_state(user)
    if not state.two_factor_enabled:
        state.two_factor_enabled = True
        state.save(update_fields=["two_factor_enabled", "updated_at"])
        log_onboarding_2fa_enabled(request, user.id)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return True


# ─────────────────────────────────────────────────────────────────────
# Risk acceptance
# ─────────────────────────────────────────────────────────────────────

def accept_risk(user, request=None) -> UserOnboardingState:
    """
    Record risk acceptance. Immutable — once accepted, cannot be reversed
    through this endpoint.
    """
    state = get_or_create_onboarding_state(user)
    _check_prerequisites(state, "risk_accepted")

    if state.risk_accepted:
        return state  # Idempotent

    state.risk_accepted = True
    state.risk_accepted_at = timezone.now()
    state.save(update_fields=["risk_accepted", "risk_accepted_at", "updated_at"])
    log_onboarding_risk_accepted(request, user.id)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return state


# ─────────────────────────────────────────────────────────────────────
# Plan selection — delegates to existing billing
# ─────────────────────────────────────────────────────────────────────

def confirm_plan_selection(user, request=None) -> UserOnboardingState:
    """
    Confirm plan selection by reading the canonical billing truth.
    Uses resolve_entitlements() to validate the plan is active/valid.

    Does NOT create/modify billing state — only reads UserSubscriptionState
    and reflects the billing truth into onboarding state.
    """
    state = get_or_create_onboarding_state(user)
    _check_prerequisites(state, "plan_selected")

    # Read canonical billing source
    sub = UserSubscriptionState.objects.filter(user=user).first()
    if not sub or not sub.current_plan:
        raise OnboardingStepError("No plan found in billing. Select a plan first.")

    # Validate plan is valid/active via existing entitlement resolver
    entitlements = resolve_entitlements(sub)
    if entitlements.resolved_access_mode == "viewer":
        raise OnboardingStepError(
            "Plan is not active. Current status: "
            f"{entitlements.source_plan_status}."
        )

    if state.plan_selected:
        return state  # Idempotent

    state.plan_selected = True
    state.save(update_fields=["plan_selected", "updated_at"])
    log_onboarding_plan_selected(request, user.id, sub.current_plan)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return state


# Alias for backward compatibility with step handler dispatch
mark_plan_selected = confirm_plan_selection


# ─────────────────────────────────────────────────────────────────────
# Account connected — reads existing TradingAccount state
# ─────────────────────────────────────────────────────────────────────

def _mark_beta_runtime_ready(user, state, request=None) -> UserOnboardingState:
    """CVM-Inc-3: mark the beta "hosted runtime ready" milestone (stored on ``account_connected``) ONLY
    when the owned beta runtime is genuinely runtime_ready — materialised/launched/process-verified,
    heartbeat-fresh and carrying an immutable Verification Report. Never touches the legacy provisioning
    path or ``mt5_instance``, and never implies broker connectivity (broker_connected is separate)."""
    from terminal_provisioning.beta_activation import runtime_ready
    from terminal_provisioning.models import AccountRuntime

    account = TradingAccount.objects.filter(user=user).order_by("id").first()
    if not account:
        raise OnboardingStepError("Add a broker account first.")
    runtime = AccountRuntime.objects.filter(trading_account=account).first()
    if runtime is None or not runtime_ready(runtime):
        raise OnboardingStepError("Hosted runtime is not ready yet.")
    if state.account_connected:
        return state  # idempotent
    state.account_connected = True
    state.save(update_fields=["account_connected", "updated_at"])
    log_onboarding_account_connected(request, user.id, account.id)
    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)
    return state


def mark_account_connected(user, request=None) -> UserOnboardingState:
    """
    Mark account_connected=True on onboarding state.
    Validates that the user actually has an active TradingAccount.
    """
    state = get_or_create_onboarding_state(user)
    _check_prerequisites(state, "account_connected")

    # CVM-Inc-3 (Nuno control 5 + truthful semantics): for an admitted controlled-beta user, this step
    # means the OWNED beta runtime is RUNTIME-READY (materialised/launched/verified + Verification
    # Report) — NOT that a broker is connected. It NEVER runs the legacy shared-instance provisioning
    # and never binds mt5_instance; broker_connected stays a separate, later, false-until-verified state.
    from billing.beta import beta_onboarding_open, is_admitted_beta_tester
    if is_admitted_beta_tester(user):
        return _mark_beta_runtime_ready(user, state, request=request)

    # GFX-BETA-PHASE0 Increment 4 — server-side gate. External beta onboarding stays CLOSED until the
    # Phase-4 isolation gates pass; a non-staff user cannot progress past this step while it's closed.
    if not beta_onboarding_open() and not user.is_staff:
        raise OnboardingStepError("Beta onboarding is not open yet.")

    account = TradingAccount.objects.filter(user=user, is_active=True).first()
    if not account:
        raise OnboardingStepError("No active trading account found. Connect one first.")

    if state.account_connected:
        return state  # Idempotent

    state.account_connected = True
    state.save(update_fields=["account_connected", "updated_at"])
    log_onboarding_account_connected(request, user.id, account.id)

    # Auto-provision terminal access for the connected account.
    # GFX-BETA-PHASE0 Increment 2: do NOT swallow provisioning failures. Record them durably on the
    # account's runtime state machine (an immutable RuntimeEvent + FAILED state) so the Account Status
    # panel shows a truthful FAILED instead of silence. Still non-blocking — terminal access is a
    # convenience, not an onboarding gate — but the failure is now evidence, not a swallowed exception.
    from terminal_provisioning.runtime_state import get_or_create_runtime, record_transition
    from terminal_provisioning.models import RuntimeState
    runtime = get_or_create_runtime(account)  # durable 1:1 record (stays NOT_PROVISIONED here)
    try:
        from mt5.services.terminal_provisioning_service import provision_terminal_for_account
        provision_terminal_for_account(user, account)
    except Exception as exc:  # noqa: BLE001 - recorded durably, not swallowed
        record_transition(
            runtime, RuntimeState.FAILED, event_type="FAILURE",
            reason_code="provision_terminal_error", detail=str(exc))
        logger.exception(
            "onboarding: terminal provisioning failed for account=%s (recorded on runtime)", account.id)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return state


# ─────────────────────────────────────────────────────────────────────
# Strategy assigned — reads existing StrategyAssignment state
# ─────────────────────────────────────────────────────────────────────

def mark_strategy_assigned(user, request=None) -> UserOnboardingState:
    """
    Mark strategy_assigned=True on onboarding state.
    Validates that the user actually has an active StrategyAssignment.
    """
    state = get_or_create_onboarding_state(user)
    _check_prerequisites(state, "strategy_assigned")

    # GFX-BETA-PHASE0 Increment 4 — server-side gate (see mark_account_connected).
    from billing.beta import beta_onboarding_open
    if not beta_onboarding_open() and not user.is_staff:
        raise OnboardingStepError("Beta onboarding is not open yet.")

    assignment = StrategyAssignment.objects.filter(
        account__user=user, is_active=True
    ).first()
    if not assignment:
        raise OnboardingStepError("No active strategy assignment found. Assign one first.")

    if state.strategy_assigned:
        return state  # Idempotent

    state.strategy_assigned = True
    state.save(update_fields=["strategy_assigned", "updated_at"])
    log_onboarding_strategy_assigned(request, user.id, assignment.id)

    _check_completion(state)
    if state.onboarding_completed:
        log_onboarding_completed(request, user.id)

    return state


# ─────────────────────────────────────────────────────────────────────
# Broker referral tracking
# ─────────────────────────────────────────────────────────────────────

def track_broker_referral(
    user, broker_code: str, referral_code: str = "", request=None,
) -> UserBrokerReferral:
    """Track a broker referral click. No credentials, no execution linkage."""
    try:
        partner = BrokerPartner.objects.get(broker_code=broker_code, is_active=True)
    except BrokerPartner.DoesNotExist:
        raise OnboardingStepError(f"Unknown or inactive broker: {broker_code}")

    referral = UserBrokerReferral.objects.create(
        user=user,
        broker_partner=partner,
        referral_code=referral_code,
    )
    log_onboarding_broker_referral(request, user.id, broker_code)
    return referral


# ─────────────────────────────────────────────────────────────────────
# Step dispatcher
# ─────────────────────────────────────────────────────────────────────

STEP_HANDLERS = {
    "risk_accepted": accept_risk,
    "plan_selected": mark_plan_selected,
    "account_connected": mark_account_connected,
    "strategy_assigned": mark_strategy_assigned,
}


def complete_step(user, step: str, request=None) -> UserOnboardingState:
    """
    Complete a single onboarding step via the generic /complete-step endpoint.
    Steps like email_verified and two_factor_enabled have dedicated endpoints.
    """
    if step in ("email_verified", "two_factor_enabled"):
        raise OnboardingStepError(
            f"Step '{step}' must be completed via its dedicated endpoint."
        )

    handler = STEP_HANDLERS.get(step)
    if not handler:
        raise OnboardingStepError(f"Unknown or unsupported step: {step}")

    return handler(user, request=request)


# ─────────────────────────────────────────────────────────────────────
# Execution readiness gating
# ─────────────────────────────────────────────────────────────────────

def check_onboarding_permits_execution(user) -> dict:
    """
    Check whether onboarding AND readiness gates permit progression toward execution.

    Returns a dict with TWO separate sections:
      A. onboarding_state — onboarding step completion
      B. readiness_state  — computed from existing system sources

    This does NOT replace execution readiness logic.
    It only REPORTS whether gates are satisfied using existing sources.

    Readiness sources:
      1. TradingAccount.is_active        → trading.TradingAccount
      2. StrategyAssignment.is_active     → strategies.StrategyAssignment
      3. stage == LIVE                    → strategies.StrategyAssignment.stage
      4. entitlement valid               → billing.entitlements.resolve_entitlements()
      5. terminal node valid             → execution.TerminalNode (status=ACTIVE)
    """
    state = get_or_create_onboarding_state(user)

    # ── A. Onboarding state ──
    missing_steps = sorted([
        step for step in REQUIRED_STEPS
        if not getattr(state, step, False)
    ])

    # ── B. Readiness state (reads existing system sources only) ──

    # 1. TradingAccount.is_active → trading.models.TradingAccount
    has_active_account = TradingAccount.objects.filter(
        user=user, is_active=True,
    ).exists()

    # 2+3. StrategyAssignment.is_active + stage == LIVE
    #       → strategies.models.StrategyAssignment
    has_live_assignment = StrategyAssignment.objects.filter(
        account__user=user, is_active=True, stage="LIVE",
    ).exists()

    # 4. Entitlement valid → billing.entitlements.resolve_entitlements()
    sub = UserSubscriptionState.objects.filter(user=user).first()
    entitlements = resolve_entitlements(sub)
    entitlement_valid = entitlements.can_deploy_automation

    # 5. Terminal node valid → execution.models.TerminalNode (status=ACTIVE)
    #    Check via TradingAccount.terminal_node linkage
    terminal_node_valid = False
    if has_active_account:
        terminal_node_valid = TradingAccount.objects.filter(
            user=user,
            is_active=True,
            terminal_node__isnull=False,
            terminal_node__status=TerminalNode.Status.ACTIVE,
        ).exists()

    readiness_checks = {
        "has_active_account": has_active_account,
        "has_live_assignment": has_live_assignment,
        "entitlement_valid": entitlement_valid,
        "terminal_node_valid": terminal_node_valid,
    }
    all_ready = all(readiness_checks.values())

    return {
        # A. Onboarding state
        "onboarding_completed": state.onboarding_completed,
        "missing_steps": missing_steps,
        # B. Readiness state
        "readiness_eligible": all_ready,
        "readiness_checks": readiness_checks,
        # Composite
        "permitted": state.onboarding_completed and all_ready,
    }
