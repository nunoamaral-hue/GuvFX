from django.contrib import admin

from .models import BrokerServer, TradingAccount


@admin.register(BrokerServer)
class BrokerServerAdmin(admin.ModelAdmin):
    list_display = ("broker_display_name", "server_name", "environment", "is_active", "updated_at")
    list_filter = ("environment", "is_active")
    search_fields = ("broker_display_name", "server_name", "aliases")


@admin.register(TradingAccount)
class TradingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "account_number", "broker_server", "broker_name", "is_demo", "is_active", "created_at")
    list_filter = ("is_demo", "is_active")
    search_fields = ("name", "account_number", "broker_name", "broker_server__server_name", "user__email")
