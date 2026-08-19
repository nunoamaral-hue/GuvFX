from django.apps import AppConfig


class CustomerNotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "customer_notifications"

    def ready(self):
        # Registration only. Handlers enqueue after commit, catch every failure, and never
        # participate in execution decisions.
        from . import signals  # noqa: F401
