from django.apps import AppConfig


class BrokerPublicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "broker_public"
    verbose_name = "Public broker directory & metrics (DARK)"
