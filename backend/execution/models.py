from django.conf import settings
from django.db import models

from trading.models import TradingAccount
from strategies.models import Strategy, StrategyAssignment


class ExecutionJob(models.Model):
    class JobType(models.TextChoices):
        TEST_CONNECTION = "TEST_CONNECTION", "Test connection"
        OPEN_TRADE = "OPEN_TRADE", "Open trade"
        CLOSE_TRADE = "CLOSE_TRADE", "Close trade"
        SYNC_POSITIONS = "SYNC_POSITIONS", "Sync positions"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    job_type = models.CharField(max_length=32, choices=JobType.choices)

    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="execution_jobs",
    )

    strategy = models.ForeignKey(
        Strategy,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="execution_jobs",
    )

    assignment = models.ForeignKey(
        StrategyAssignment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="execution_jobs",
    )

    # Arbitrary parameters for the job (symbol, volume, SL/TP, etc.)
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )

    worker_id = models.CharField(max_length=64, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_execution_jobs",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.job_type} for account {self.account_id} (status={self.status})"
