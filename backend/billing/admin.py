from django.contrib import admin

from .models import UserSubscriptionState, Invoice


@admin.register(UserSubscriptionState)
class UserSubscriptionStateAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "current_plan",
        "plan_status",
        "viewer_mode",
        "billing_cycle",
        "current_period_ends_at",
        "created_at",
    )
    list_filter = ("current_plan", "plan_status", "viewer_mode", "billing_cycle")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "user",
        "plan_at_issue",
        "status",
        "total_amount",
        "currency",
        "issue_date",
        "due_date",
        "paid_at",
    )
    list_filter = ("status", "plan_at_issue", "currency")
    search_fields = ("invoice_number", "user__email", "user__username")
    readonly_fields = ("created_at", "updated_at")
