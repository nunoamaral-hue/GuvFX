"""
Flow A — Shadow-mode suppression path.

This is the execution boundary. Flow A's terminal action on an ACCEPTed
candidate is to **log and audit** it — never to dispatch it. The suppression
guarantee is *structural*, not a runtime flag that could be toggled:

  * Flow A imports nothing from ``execution`` and never constructs an
    ``execution.ExecutionJob`` (verified by :mod:`flow_a.tests`).
  * No pollable ``PENDING OPEN_TRADE`` job is created, so the MT5 worker — which
    polls ``/api/execution/jobs/next/`` for ``PENDING`` jobs — cannot pick the
    candidate up.
  * :func:`emit_shadow_candidate` refuses anything that is a Django ``Model``
    instance, so a persisted ORM object can never travel this path.

EA remains the sole live decider. Execution is suppressed. No live trading.
"""

from __future__ import annotations

import json
import logging

from .types import OpenTradeCandidate

logger = logging.getLogger("flow_a.shadow")


def emit_shadow_candidate(candidate: OpenTradeCandidate, *, run_id: str) -> dict:
    """Log + audit a suppressed OPEN_TRADE candidate. Returns an audit record.

    This is the only "output" of an accepted Flow A run. It performs no
    execution, enqueues no job, and returns a plain dict (transient artifact).
    """
    # Hard guard: never let a persisted/ORM object reach the suppression path.
    if _is_django_model_instance(candidate):
        raise TypeError(
            "Suppression refuses Django model instances — Flow A emits transient "
            "candidates only, never persisted/pollable execution objects."
        )
    if not isinstance(candidate, OpenTradeCandidate):
        raise TypeError("emit_shadow_candidate expects an OpenTradeCandidate")

    record = {
        "event": "FLOW_A_SHADOW_CANDIDATE_SUPPRESSED",
        "run_id": run_id,
        "execution_suppressed": True,
        "execution_job_created": False,
        "candidate": candidate.to_dict(),
        "note": "Shadow mode — candidate logged only; EA remains sole live decider.",
    }
    logger.info("flow_a.shadow.candidate_suppressed %s", json.dumps(record))
    return record


def _is_django_model_instance(obj) -> bool:
    try:
        from django.db.models import Model
    except Exception:  # pragma: no cover - Django always present here
        return False
    return isinstance(obj, Model)
