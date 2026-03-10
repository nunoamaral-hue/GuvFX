from rest_framework import serializers

from .models import UserSubscriptionState, Invoice


class UserSubscriptionStateSerializer(serializers.ModelSerializer):
    """Read-only serializer for the authenticated user's subscription state."""

    class Meta:
        model = UserSubscriptionState
        fields = [
            "current_plan",
            "plan_status",
            "viewer_mode",
            "has_ever_paid",
            "currency",
            "trial_started_at",
            "trial_expires_at",
            "billing_cycle",
            "current_period_started_at",
            "current_period_ends_at",
            "next_invoice_date",
            "next_payment_due_date",
            "last_invoice_date",
            "last_payment_at",
            "last_plan_change_at",
        ]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    """Read-only serializer for a user's invoice list."""

    class Meta:
        model = Invoice
        fields = [
            "invoice_number",
            "plan_at_issue",
            "billing_cycle_at_issue",
            "period_start",
            "period_end",
            "issue_date",
            "due_date",
            "status",
            "currency",
            "subtotal_amount",
            "tax_amount",
            "total_amount",
            "paid_at",
            "voided_at",
            "notes",
        ]
        read_only_fields = fields
