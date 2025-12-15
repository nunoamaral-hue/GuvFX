from django.contrib import admin

from .models import (
    HostingProvider,
    VpsPlan,
    VpsInstance,
    Mt5Instance,
    UserHostingSubscription,
    HostingRequest,
)


@admin.register(HostingProvider)
class HostingProviderAdmin(admin.ModelAdmin):
  list_display = ("name", "api_type", "is_active")
  list_filter = ("api_type", "is_active")
  search_fields = ("name",)


@admin.register(VpsPlan)
class VpsPlanAdmin(admin.ModelAdmin):
  list_display = (
    "name",
    "provider",
    "cpu_cores",
    "memory_mb",
    "disk_gb",
    "monthly_price_usd",
    "is_shared",
    "max_mt5_instances",
    "is_user_visible",
  )
  list_filter = ("provider", "is_shared", "is_user_visible")
  search_fields = ("name", "provider__name")


@admin.register(VpsInstance)
class VpsInstanceAdmin(admin.ModelAdmin):
  list_display = (
    "hostname",
    "public_ip",
    "provider",
    "plan",
    "status",
    "is_dedicated",
    "current_mt5_count",
  )
  list_filter = ("provider", "plan", "status", "is_dedicated")
  search_fields = ("hostname", "public_ip", "external_id")


@admin.register(Mt5Instance)
class Mt5InstanceAdmin(admin.ModelAdmin):
  list_display = ("label", "broker_name", "account_login", "owner", "vps", "status", "created_at")
  list_filter = ("broker_name", "status")
  search_fields = ("label", "account_login", "broker_name", "owner__email")


@admin.register(UserHostingSubscription)
class UserHostingSubscriptionAdmin(admin.ModelAdmin):
  list_display = ("user", "plan", "billing_status", "vps", "mt5_instance", "created_at")
  list_filter = ("billing_status", "plan")
  search_fields = ("user__email", "user__username", "plan__name")


@admin.register(HostingRequest)
class HostingRequestAdmin(admin.ModelAdmin):
  list_display = ("id", "owner", "status", "created_at")
  list_filter = ("status",)
  search_fields = ("owner__email", "owner__username", "note")
  readonly_fields = ("owner", "created_at", "updated_at")
