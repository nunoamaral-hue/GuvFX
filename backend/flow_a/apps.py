from django.apps import AppConfig


class FlowAConfig(AppConfig):
    """Flow A — Shadow Delivery pipeline (no models, no persistence).

    Like ``intelligence``, this app persists **no** models. It is a transient,
    pure-transform pipeline that ends in a *suppressed* OPEN_TRADE candidate.
    It is not wired into production ``INSTALLED_APPS``; it is exercised only via
    the isolated ``flow_a._shadow_settings`` shim (management command + tests).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "flow_a"
