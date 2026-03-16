"""
Backtest worker services — Packet B B2.

Provides the atomic claim and execution lifecycle for the
platform-native DB-backed backtest worker.

No Celery, no external broker, no storage integration.
"""
import logging
import platform
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from backtests.models import (
    BacktestArtifact,
    BacktestExecution,
    BacktestJob,
    BacktestStatus,
)
from core.audit import (
    log_backtest_execution_claimed,
    log_backtest_execution_completed,
    log_backtest_execution_failed,
    log_backtest_execution_started,
)

logger = logging.getLogger(__name__)


def get_worker_hostname() -> str:
    """Return a deterministic worker identifier from the OS hostname."""
    return platform.node() or "unknown-worker"


def claim_next_execution(worker_hostname: str) -> BacktestExecution | None:
    """
    Atomically claim the oldest queued BacktestExecution.

    Uses ``select_for_update(skip_locked=True)`` inside a transaction
    to prevent double-claim by concurrent workers.

    Returns the claimed BacktestExecution (now in ``running`` status),
    or ``None`` if no eligible execution exists.
    """
    with transaction.atomic():
        execution = (
            BacktestExecution.objects.select_for_update(skip_locked=True)
            .filter(status=BacktestStatus.QUEUED)
            .order_by("created_at")
            .first()
        )
        if execution is None:
            return None

        now = timezone.now()

        # Claim the execution
        execution.status = BacktestStatus.RUNNING
        execution.worker_hostname = worker_hostname
        execution.started_at = now
        execution.save(
            update_fields=["status", "worker_hostname", "started_at", "updated_at"]
        )

        # Update the parent BacktestJob to running (if still queued)
        job = execution.backtest_job
        if job.status == BacktestStatus.QUEUED:
            job.status = BacktestStatus.RUNNING
            job.started_at = now
            job.worker_id = worker_hostname
            job.save(update_fields=["status", "started_at", "worker_id", "updated_at"])

        log_backtest_execution_claimed(
            execution_id=execution.pk,
            job_id=job.pk,
            worker_hostname=worker_hostname,
            run_identifier=execution.run_identifier,
        )

        return execution


def run_backtest_execution(execution: BacktestExecution) -> None:
    """
    Execute a single backtest and update lifecycle fields.

    B2 scope: placeholder execution only.  No real engine, no artifact
    file I/O, no summary calculation.

    Creates placeholder BacktestArtifact metadata rows and updates
    BacktestExecution + BacktestJob status/timestamps.
    """
    job = execution.backtest_job
    worker_hostname = execution.worker_hostname

    log_backtest_execution_started(
        execution_id=execution.pk,
        job_id=job.pk,
        worker_hostname=worker_hostname,
    )

    try:
        # ── Placeholder backtest execution ──
        # B2: no real engine.  This is where the actual backtesting
        # engine integration will be wired in a future packet.
        _run_placeholder_backtest(execution)

        # ── Create placeholder artifact rows ──
        _create_placeholder_artifacts(execution)

        # ── Mark execution completed ──
        now = timezone.now()
        duration = None
        if execution.started_at:
            delta = now - execution.started_at
            duration = Decimal(str(round(delta.total_seconds(), 3)))

        execution.status = BacktestStatus.COMPLETED
        execution.completed_at = now
        execution.duration_seconds = duration
        execution.save(
            update_fields=[
                "status",
                "completed_at",
                "duration_seconds",
                "updated_at",
            ]
        )

        # ── Update parent job ──
        _update_job_on_completion(job, BacktestStatus.COMPLETED)

        log_backtest_execution_completed(
            execution_id=execution.pk,
            job_id=job.pk,
            duration_seconds=float(duration) if duration else None,
        )

        logger.info(
            "BacktestExecution %s completed (%.3fs)",
            execution.run_identifier,
            float(duration) if duration else 0,
        )

    except Exception as exc:
        _handle_execution_failure(execution, job, exc)
        raise


def _run_placeholder_backtest(execution: BacktestExecution) -> None:
    """
    Placeholder backtest logic for B2.

    Does nothing beyond logging.  A future packet will wire the
    real backtesting engine here.
    """
    logger.info(
        "Placeholder backtest for execution %s (job %s, strategy %s, %s %s %s–%s)",
        execution.run_identifier,
        execution.backtest_job_id,
        execution.backtest_job.strategy_id,
        execution.backtest_job.symbol,
        execution.backtest_job.timeframe,
        execution.backtest_job.start_date,
        execution.backtest_job.end_date,
    )


def _create_placeholder_artifacts(execution: BacktestExecution) -> None:
    """
    Create minimal placeholder BacktestArtifact rows.

    B2 scope: metadata rows only, no actual file I/O.
    Artifacts are immutable once created.
    """
    placeholder_run_id = execution.run_identifier

    BacktestArtifact.objects.create(
        execution=execution,
        artifact_type="execution_log",
        file_path=f"placeholder://backtests/{placeholder_run_id}/execution.log",
        file_size=0,
        checksum="placeholder-no-file",
    )

    BacktestArtifact.objects.create(
        execution=execution,
        artifact_type="result_stub",
        file_path=f"placeholder://backtests/{placeholder_run_id}/result.json",
        file_size=0,
        checksum="placeholder-no-file",
    )

    logger.info(
        "Created 2 placeholder artifacts for execution %s",
        execution.run_identifier,
    )


def _update_job_on_completion(job: BacktestJob, status: str) -> None:
    """
    Update BacktestJob status and timestamps after execution finishes.

    Minimal: sets completed_at and final status.  Does not implement
    multi-execution orchestration (one job / one execution in B2).
    """
    now = timezone.now()
    job.status = status
    job.completed_at = now
    job.save(update_fields=["status", "completed_at", "updated_at"])


def _handle_execution_failure(
    execution: BacktestExecution,
    job: BacktestJob,
    exc: Exception,
) -> None:
    """
    Mark both execution and job as failed on unhandled exception.

    Timestamps remain consistent.  No partial artifact/summary
    side effects because those are out of scope in B2.
    """
    now = timezone.now()
    error_msg = str(exc)[:500]

    duration = None
    if execution.started_at:
        delta = now - execution.started_at
        duration = Decimal(str(round(delta.total_seconds(), 3)))

    try:
        execution.status = BacktestStatus.FAILED
        execution.completed_at = now
        execution.duration_seconds = duration
        execution.log_path = ""
        execution.save(
            update_fields=[
                "status",
                "completed_at",
                "duration_seconds",
                "log_path",
                "updated_at",
            ]
        )
    except Exception as save_exc:
        logger.error(
            "Failed to save execution failure state for %s: %s",
            execution.run_identifier,
            save_exc,
        )

    try:
        _update_job_on_completion(job, BacktestStatus.FAILED)
    except Exception as save_exc:
        logger.error(
            "Failed to save job failure state for job %s: %s",
            job.pk,
            save_exc,
        )

    log_backtest_execution_failed(
        execution_id=execution.pk,
        job_id=job.pk,
        error=error_msg,
    )

    logger.error(
        "BacktestExecution %s failed: %s",
        execution.run_identifier,
        error_msg,
    )
