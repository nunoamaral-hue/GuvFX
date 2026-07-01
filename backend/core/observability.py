"""
GFX-PKT-OPS-OBSERVABILITY-FOUNDATION — structured execution-lifecycle logging + metrics.

ADDITIVE, FAIL-OPEN ONLY. Every function here is a pure side-effect: it NEVER
raises into the caller, NEVER changes control flow, and NEVER touches
execution/risk/trading logic. It emits single-line JSON records to dedicated
loggers so a future Grafana/Loki stack can ingest them — no new infrastructure
(no Prometheus/StatsD/message bus). Records propagate to the root console handler
(settings.LOGGING) at INFO, i.e. to stdout → container logs.

Public API:
  - new_correlation_id() -> str            : a fresh correlation id (uuid4 hex).
  - log_stage(stage, correlation_id, **f)  : one `execution_lifecycle` record.
  - emit_metric(metric, value, ...)        : one `execution_metric` record.

Lives in ``core`` so the upstream ``signal_intake`` app and the ``execution``
app + worker can all import it without crossing the one-way app boundary.
"""

from __future__ import annotations

import json
import logging
import uuid

STAGE_LOGGER = logging.getLogger("guvfx.execution.lifecycle")
METRIC_LOGGER = logging.getLogger("guvfx.execution.metrics")

# Canonical ordered lifecycle stages (single source of truth for stage names).
LIFECYCLE_STAGES = (
    "signal_received",
    "parse_complete",
    "planning_complete",
    "shadow_job_created",
    "worker_claimed",
    "order_check_request",
    "order_check_response",
    "validation_outcome",
    "cleanup_complete",
)


def new_correlation_id() -> str:
    """Return a fresh correlation id (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def _emit(logger: logging.Logger, record: dict) -> None:
    # Fail-open: observability must never break the pipeline it observes.
    try:
        logger.info(json.dumps(record, default=str, sort_keys=True))
    except Exception:
        pass


def log_stage(stage: str, correlation_id, **fields) -> None:
    """Emit one structured execution-lifecycle record (fail-open).

    ``stage`` should be one of ``LIFECYCLE_STAGES``; ``correlation_id`` ties every
    stage of a single execution attempt together. Extra ``**fields`` are merged
    into the JSON record (keep them small and label-like for Loki).
    """
    record = {
        "event": "execution_lifecycle",
        "stage": stage,
        "correlation_id": correlation_id or "",
    }
    record.update(fields)
    _emit(STAGE_LOGGER, record)


def emit_metric(metric: str, value, *, correlation_id=None, unit=None, **labels) -> None:
    """Emit one structured metric record (fail-open, log-based — no metrics backend).

    Example: ``emit_metric("worker_claim_latency", 42, correlation_id=cid,
    unit="ms", job_type="PLACE_ORDER_SHADOW")``. Rates (e.g. validation success
    rate) are computed downstream in Grafana/Loki from the emitted counts.
    """
    record = {"event": "execution_metric", "metric": metric, "value": value}
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    if unit is not None:
        record["unit"] = unit
    record.update(labels)
    _emit(METRIC_LOGGER, record)
