from django.apps import AppConfig


class SignalIntakeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "signal_intake"
    verbose_name = "Signal Intake (Wayond → pending approval; shadow, no execution)"
