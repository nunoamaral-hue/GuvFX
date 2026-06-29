from django.contrib import admin
from .models import TerminalNode, ExecutionJob, WorkerIdentity


@admin.register(TerminalNode)
class TerminalNodeAdmin(admin.ModelAdmin):
    list_display = (
        "hostname",
        "display_name",
        "status",
        "max_accounts",
        "active_accounts",
        "last_heartbeat",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("hostname", "display_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ExecutionJob)
class ExecutionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "account", "status", "worker_id", "terminal_node", "created_at")
    list_filter = ("job_type", "status")
    search_fields = ("worker_id",)
    readonly_fields = ("created_at", "started_at", "finished_at")
    raw_id_fields = ("account", "strategy", "assignment", "terminal_node", "created_by")


@admin.register(WorkerIdentity)
class WorkerIdentityAdmin(admin.ModelAdmin):
    list_display = ("worker_id", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("worker_id",)
    readonly_fields = ("created_at",)
