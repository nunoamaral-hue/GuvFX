from django.apps import AppConfig


class ExecutionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'execution'

    def ready(self):
        # AUTO-SHADOW FOUNDATION — connect the auto-router to signal_intake's post-acquire
        # signal. Imports are local (inside ready) to avoid touching the app registry at
        # import time. The receiver is fail-closed and a no-op with default config.
        from signal_intake.signals import signal_acquired
        from execution.auto_router import route_acquired_signal

        signal_acquired.connect(
            route_acquired_signal, dispatch_uid="execution.auto_router.route_acquired_signal"
        )
