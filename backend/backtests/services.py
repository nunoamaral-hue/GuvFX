"""
Backtest worker services — Packet B B2/B3/B5.

Provides the atomic claim and execution lifecycle for the
platform-native DB-backed backtest worker, with local artifact
storage integration (B3), plus API-layer service helpers (B5).

No Celery, no external broker, no cloud storage.
"""
import json
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
    BacktestSummary,
    PromotionCandidate,
    ReviewStatus,
)
from backtests.artifact_storage import store_artifact
from backtests.summary_engine import compute_and_store_summary
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

    Placeholder execution only (no real engine yet).  Writes artifact
    files to local storage (B3), creates BacktestArtifact metadata
    rows, and computes BacktestSummary metrics (B4).
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

        # ── Create artifact files + metadata rows ──
        _create_artifacts(execution)

        # ── Compute and store BacktestSummary (B4) ──
        compute_and_store_summary(execution)

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


def _create_artifacts(execution: BacktestExecution) -> None:
    """
    Create artifact files on local storage and corresponding
    BacktestArtifact metadata rows.

    B3: writes real files via ``artifact_storage.store_artifact``.
    Content is still placeholder (no real engine yet), but files
    are persisted, compressed, and checksummed.

    Artifacts are immutable once created.
    """
    job = execution.backtest_job

    # ── Execution manifest (JSON) ──
    manifest_content = json.dumps(
        {
            "execution_id": execution.pk,
            "run_identifier": execution.run_identifier,
            "job_id": job.pk,
            "strategy_id": job.strategy_id,
            "symbol": job.symbol,
            "timeframe": job.timeframe,
            "start_date": str(job.start_date),
            "end_date": str(job.end_date),
            "parameter_set": job.parameter_set,
            "worker_hostname": execution.worker_hostname,
            "started_at": str(execution.started_at) if execution.started_at else None,
        },
        indent=2,
    )
    _write_artifact(execution, "execution_manifest", manifest_content, "json")

    # ── Worker execution log (text) ──
    log_content = (
        f"BacktestExecution {execution.run_identifier}\n"
        f"Job: {job.pk} | Strategy: {job.strategy_id}\n"
        f"Symbol: {job.symbol} | Timeframe: {job.timeframe}\n"
        f"Range: {job.start_date} – {job.end_date}\n"
        f"Worker: {execution.worker_hostname}\n"
        f"Started: {execution.started_at}\n"
        f"Status: placeholder execution (B3 — no real engine yet)\n"
    )
    _write_artifact(execution, "execution_log", log_content, "txt")

    # ── Result stub (JSON) ──
    result_content = json.dumps(
        {
            "execution_id": execution.pk,
            "status": "placeholder",
            "note": "Real backtest engine not yet integrated.",
        },
        indent=2,
    )
    _write_artifact(execution, "result_stub", result_content, "json")

    logger.info(
        "Created 3 artifacts for execution %s",
        execution.run_identifier,
    )


def _write_artifact(
    execution: BacktestExecution,
    artifact_type: str,
    content: str,
    extension: str,
) -> None:
    """
    Write a single artifact file and create the BacktestArtifact row.

    Uses ``artifact_storage.store_artifact`` for safe path generation,
    compression, checksum, and immutability enforcement.
    """
    stored = store_artifact(
        execution_id=execution.pk,
        artifact_type=artifact_type,
        content=content,
        extension=extension,
    )

    BacktestArtifact.objects.create(
        execution=execution,
        artifact_type=artifact_type,
        file_path=stored.file_path,
        file_size=stored.file_size,
        checksum=stored.checksum,
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


# =========================================================================
# Packet B — B5: API service helpers
# =========================================================================


def create_backtest_request(
    user,
    strategy,
    symbol: str,
    timeframe: str,
    start_date,
    end_date,
    parameter_set: dict | None = None,
    data_source: str = "",
) -> tuple[BacktestJob, BacktestExecution]:
    """
    Create a BacktestJob and initial BacktestExecution in queued state.

    This is the canonical API-layer entrypoint for submitting a new
    backtest request.  The worker will pick up the queued execution.

    Returns (job, execution) tuple.
    """
    with transaction.atomic():
        job = BacktestJob.objects.create(
            user=user,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            parameter_set=parameter_set or {},
            data_source=data_source,
            status=BacktestStatus.QUEUED,
        )

        execution = BacktestExecution.objects.create(
            backtest_job=job,
            run_identifier=f"exec-{job.pk}-{uuid.uuid4().hex[:8]}",
            status=BacktestStatus.QUEUED,
        )

    logger.info(
        "Created BacktestJob %d + BacktestExecution %s for user %s",
        job.pk,
        execution.run_identifier,
        user.pk,
    )

    return job, execution


def get_backtest_status_for_user(job_id: int, user) -> dict:
    """
    Return safe status/lifecycle information for a BacktestJob.

    Enforces user-scoped access (non-staff see only own jobs).
    Returns a dict with safe fields for API serialization.

    Raises BacktestJob.DoesNotExist if not found or not accessible.
    """
    job = _get_user_job(job_id, user)
    execution = (
        job.executions.select_related("backtest_job")
        .order_by("-created_at")
        .first()
    )

    result = {
        "job_id": job.pk,
        "status": job.status,
        "strategy_id": job.strategy_id,
        "symbol": job.symbol,
        "timeframe": job.timeframe,
        "start_date": job.start_date,
        "end_date": job.end_date,
        "requested_at": job.requested_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "worker_id": job.worker_id or None,
        "execution_id": None,
        "execution_status": None,
        "execution_run_identifier": None,
        "execution_started_at": None,
        "execution_completed_at": None,
        "execution_duration_seconds": None,
    }

    if execution:
        result.update({
            "execution_id": execution.pk,
            "execution_status": execution.status,
            "execution_run_identifier": execution.run_identifier,
            "execution_started_at": execution.started_at,
            "execution_completed_at": execution.completed_at,
            "execution_duration_seconds": (
                float(execution.duration_seconds)
                if execution.duration_seconds is not None
                else None
            ),
        })

    return result


def get_backtest_results_for_user(job_id: int, user) -> dict:
    """
    Return BacktestSummary + safe execution result metadata.

    Enforces user-scoped access.  Returns honest status if summary
    is not yet available.

    Raises BacktestJob.DoesNotExist if not found or not accessible.
    """
    job = _get_user_job(job_id, user)
    execution = (
        job.executions.order_by("-created_at").first()
    )

    result = {
        "job_id": job.pk,
        "status": job.status,
        "summary_available": False,
        "summary": None,
        "execution_id": None,
        "execution_status": None,
        "artifact_count": 0,
        "promotion_candidate": None,
    }

    if execution:
        result["execution_id"] = execution.pk
        result["execution_status"] = execution.status
        result["artifact_count"] = execution.artifacts.count()

        # Promotion candidate (B7)
        try:
            result["promotion_candidate"] = execution.promotion_candidate
        except PromotionCandidate.DoesNotExist:
            pass

        try:
            summary = execution.summary
            result["summary_available"] = True
            result["summary"] = {
                "total_trades": summary.total_trades,
                "win_rate": (
                    float(summary.win_rate) if summary.win_rate is not None else None
                ),
                "profit_factor": (
                    float(summary.profit_factor)
                    if summary.profit_factor is not None
                    else None
                ),
                "max_drawdown": (
                    float(summary.max_drawdown)
                    if summary.max_drawdown is not None
                    else None
                ),
                "sharpe_ratio": (
                    float(summary.sharpe_ratio)
                    if summary.sharpe_ratio is not None
                    else None
                ),
                "expectancy": (
                    float(summary.expectancy)
                    if summary.expectancy is not None
                    else None
                ),
            }
        except BacktestSummary.DoesNotExist:
            pass

    return result


def list_backtest_artifacts_for_user(job_id: int, user) -> list[dict]:
    """
    Return safe artifact metadata listing for a BacktestJob.

    Enforces user-scoped access.  Returns metadata only (no file
    contents, no absolute paths).

    Raises BacktestJob.DoesNotExist if not found or not accessible.
    """
    job = _get_user_job(job_id, user)
    execution = job.executions.order_by("-created_at").first()

    if execution is None:
        return []

    artifacts = execution.artifacts.order_by("created_at")
    return [
        {
            "artifact_id": a.pk,
            "artifact_type": a.artifact_type,
            "file_path": a.file_path,
            "file_size": a.file_size,
            "checksum": a.checksum,
            "created_at": a.created_at,
        }
        for a in artifacts
    ]


def _get_user_job(job_id: int, user) -> BacktestJob:
    """
    Fetch a BacktestJob by ID with user-scoped access control.

    Non-staff users can only access their own jobs.
    Staff users can access any job.

    Raises BacktestJob.DoesNotExist if not found or not accessible.
    """
    qs = BacktestJob.objects.select_related("strategy")
    if not user.is_staff:
        qs = qs.filter(user=user)
    return qs.get(pk=job_id)


def _get_user_execution(execution_id: int, user) -> BacktestExecution:
    """
    Fetch a BacktestExecution by ID with user-scoped access control.

    Ownership is enforced via the parent BacktestJob.user field.
    Non-staff users can only access executions belonging to their own jobs.

    Raises BacktestExecution.DoesNotExist if not found or not accessible.
    """
    qs = BacktestExecution.objects.select_related("backtest_job")
    if not user.is_staff:
        qs = qs.filter(backtest_job__user=user)
    return qs.get(pk=execution_id)


# =========================================================================
# Packet B — B7: Promotion candidate service helpers
# =========================================================================


def get_promotion_candidate_for_execution_for_user(
    execution_id: int, user
) -> PromotionCandidate | None:
    """
    Return the PromotionCandidate for an execution, or None.

    Enforces user-scoped ownership via the execution → job chain.
    Raises BacktestExecution.DoesNotExist if execution not found/accessible.
    """
    execution = _get_user_execution(execution_id, user)
    try:
        return execution.promotion_candidate
    except PromotionCandidate.DoesNotExist:
        return None


def create_promotion_candidate_for_execution_for_user(
    execution_id: int, user, request=None
) -> tuple[PromotionCandidate, bool]:
    """
    Idempotent create of a PromotionCandidate for an execution.

    If a candidate already exists, returns it unchanged (created=False).
    Otherwise creates one with review_status=pending (created=True).

    Emits BACKTEST_PROMOTION_CREATED audit event only on actual creation.

    Returns (candidate, created) tuple.
    Raises BacktestExecution.DoesNotExist if execution not found/accessible.
    """
    execution = _get_user_execution(execution_id, user)

    try:
        existing = execution.promotion_candidate
        return existing, False
    except PromotionCandidate.DoesNotExist:
        pass

    candidate = PromotionCandidate.objects.create(
        backtest_execution=execution,
        review_status=ReviewStatus.PENDING,
    )

    from core.audit import log_backtest_promotion_created

    log_backtest_promotion_created(
        request,
        promotion_id=candidate.pk,
        execution_id=execution.pk,
        job_id=execution.backtest_job_id,
    )

    logger.info(
        "Created PromotionCandidate %d for execution %d (job %d, user %s)",
        candidate.pk,
        execution.pk,
        execution.backtest_job_id,
        user.pk,
    )

    return candidate, True


# =========================================================================
# Packet C1: Promotion candidate review
# =========================================================================


def review_promotion_candidate(
    candidate_id: int,
    decision: str,
    notes: str,
    reviewer,
    request=None,
) -> PromotionCandidate:
    """
    Apply a review decision (approved/rejected) to a PromotionCandidate.

    Updates review_status, reviewed_by, reviewed_at, and review_notes.
    Emits BACKTEST_PROMOTION_REVIEWED audit event.

    Allows overwriting a previous review decision (latest review is truth).

    Args:
        candidate_id: PK of the PromotionCandidate.
        decision: "approved" or "rejected".
        notes: Optional review notes.
        reviewer: The admin user performing the review.
        request: HTTP request for audit context.

    Returns the updated PromotionCandidate.
    Raises PromotionCandidate.DoesNotExist if not found.
    """
    candidate = PromotionCandidate.objects.select_related(
        "backtest_execution", "backtest_execution__backtest_job"
    ).get(pk=candidate_id)

    candidate.review_status = decision
    candidate.reviewed_by = reviewer
    candidate.reviewed_at = timezone.now()
    candidate.review_notes = notes
    candidate.save(
        update_fields=[
            "review_status",
            "reviewed_by",
            "reviewed_at",
            "review_notes",
            "updated_at",
        ]
    )

    from core.audit import log_backtest_promotion_reviewed

    log_backtest_promotion_reviewed(
        request,
        promotion_id=candidate.pk,
        execution_id=candidate.backtest_execution_id,
        decision=decision,
        reviewer_id=reviewer.pk,
        has_notes=bool(notes),
    )

    logger.info(
        "PromotionCandidate %d reviewed as '%s' by user %s",
        candidate.pk,
        decision,
        reviewer.pk,
    )

    return candidate
