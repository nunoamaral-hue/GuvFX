from django.apps import AppConfig


class HostedWorkspaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hosted_workspace"
    verbose_name = "Hosted Persistent MT5 Workspace (ADR-0033, DARK)"
