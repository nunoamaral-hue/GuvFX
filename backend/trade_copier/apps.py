from django.apps import AppConfig


class TradeCopierConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "trade_copier"
    verbose_name = "MT5 Master/Follower Copier (DARK)"
