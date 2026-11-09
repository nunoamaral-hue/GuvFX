"""Staff admin for broker configuration (Django admin is auth-gated staff-only; never publicly exposed).

BrokerProfile is editable by staff (the broker administration interface); BrokerProfileAudit is strictly read-only
(the append-only configuration history). No credentials are shown here — profiles hold only config.
"""
from __future__ import annotations

from django.contrib import admin

from .models import (BrokerEmailIdentity, BrokerEmailIdentityAudit, BrokerProfile, BrokerProfileAudit,
                     GenericWithdrawalPattern, ReviewCandidateAction, WithdrawalReviewCandidate)


@admin.register(BrokerProfile)
class BrokerProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "approved_sender_domains", "trusted_authserv_ids", "auth_policy",
                    "supported_categories", "provenance", "parser_name", "parser_version", "is_active", "updated_at")
    list_filter = ("is_active", "auth_policy", "provenance")
    search_fields = ("name", "slug", "official_domains", "approved_sender_domains", "server_names")
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        # Record WHO changed the config in the append-only audit (BrokerProfile.save reads this transient).
        obj._changed_by = getattr(request.user, "email", "") or getattr(request.user, "username", "") or "staff"
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        # Brokers are RETIRED via is_active=False, never deleted — deleting one would destroy its append-only audit
        # history (and is blocked at the DB by the PROTECT FK anyway). Keeps the config record permanent.
        return False


@admin.register(BrokerEmailIdentity)
class BrokerEmailIdentityAdmin(admin.ModelAdmin):
    """View-only. A VERIFIED binding is NEVER created or edited by hand here — it must go through the audited
    verification service (``identity_verification.verify_identity``) so every attribution is proof-backed and
    recorded. Staff may read the registry; mutation is disabled."""
    list_display = ("masked_email", "broker_name", "status", "user", "trading_account", "connected_mailbox",
                    "updated_at")
    list_filter = ("status", "broker_name", "origin")
    search_fields = ("broker_name",)                     # NOT email — owner-private; search by broker only
    readonly_fields = tuple(f.name for f in BrokerEmailIdentity._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BrokerEmailIdentityAudit)
class BrokerEmailIdentityAuditAdmin(admin.ModelAdmin):
    """Append-only verification history — strictly read-only (written only by the verification service)."""
    list_display = ("created_at", "action", "outcome", "masked_email", "broker_name", "subject_user", "actor",
                    "status_before", "status_after")
    list_filter = ("action", "outcome", "broker_name")
    search_fields = ("broker_name", "actor")
    readonly_fields = tuple(f.name for f in BrokerEmailIdentityAudit._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BrokerProfileAudit)
class BrokerProfileAuditAdmin(admin.ModelAdmin):
    list_display = ("broker_profile", "action", "changed_by", "created_at")
    list_filter = ("action",)
    search_fields = ("broker_profile__name", "changed_by")
    readonly_fields = ("broker_profile", "action", "snapshot", "changed_by", "created_at")

    def has_add_permission(self, request):          # append-only history — written by the app, never by hand
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ADR-0088 — generic withdrawal detection: the pattern registry (staff-editable config) + redacted review
# candidates and their append-only audit (read-only). Django admin is auth-gated staff-only; no PII is shown
# (candidates store only redacted structural signals).

@admin.register(GenericWithdrawalPattern)
class GenericWithdrawalPatternAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "broker_display_name", "enabled", "confidence", "provenance",
                    "effective_date", "reviewer", "updated_at")
    list_filter = ("enabled", "provenance")
    search_fields = ("name", "broker_display_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(WithdrawalReviewCandidate)
class WithdrawalReviewCandidateAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "disposition", "proposed_state", "tier", "candidate_broker_name",
                    "authenticated_sender_domain", "masked_account_suffix", "amount", "currency", "confidence",
                    "operator_action", "observed_at")
    list_filter = ("disposition", "proposed_state", "tier", "operator_action")
    search_fields = ("candidate_broker_name", "authenticated_sender_domain", "evidence_sha256", "matched_pattern")
    readonly_fields = tuple(f.name for f in WithdrawalReviewCandidate._meta.fields)

    def has_add_permission(self, request):          # candidates are written by the detector, never by hand
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReviewCandidateAction)
class ReviewCandidateActionAdmin(admin.ModelAdmin):
    list_display = ("id", "candidate", "action", "actor", "reason", "created_at")
    list_filter = ("action",)
    search_fields = ("actor", "evidence_sha256")
    readonly_fields = tuple(f.name for f in ReviewCandidateAction._meta.fields)

    def has_add_permission(self, request):          # append-only audit — written by the API, never by hand
        return False

    def has_delete_permission(self, request, obj=None):
        return False
