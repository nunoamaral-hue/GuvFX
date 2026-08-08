"""ADR-0034 Execution Engine (G10) — Hosted Workspace idempotency + ambiguous-result reconciliation (pure).

Two safety-critical, side-effect-free pieces:

1. ``hosted_idempotency_key`` — a deterministic PROVENANCE/AUDIT identity that binds one execution attempt to
   its full intended context (workspace + broker login/server + job + operation + strategy). It is injective
   (any differing component → a different key) and secret-free (the broker login is an identifier folded into
   a SHA-256 digest, never exposed in customer-visible output). It is **provenance only**: it is stamped onto
   the ``ExecutionJob`` + the ``HostedWorkspaceExecution`` rows and read by NOTHING at the order boundary
   (the bridge never consults it). Because ``job_id`` is a component, two ExecutionJobs for the same logical
   intent get two DIFFERENT keys — so the key is NOT the duplicate-order guard. The actual order-time
   duplicate prevention is: single-claim (``select_for_update(skip_locked)`` in ``views.next_job``) + the
   backend never re-enqueuing a resolved job + the bridge's per-job COMMENT open-position guard
   (``find_existing_execution``) + the ``(job, phase)`` uniqueness on ``HostedWorkspaceExecution``.

2. ``classify_ambiguous_result`` — after an ambiguous ``order_send`` (timeout / crash / unknown response),
   the outcome is decided from reconciled broker/terminal TRUTH, never re-sent blindly. Only
   ``CONFIRMED_NOT_EXECUTED`` may become eligible for a controlled retry; ``STILL_AMBIGUOUS`` fails closed
   (quarantine). This is the hard rule that prevents a duplicate order after an ambiguous send.
"""
from __future__ import annotations

import hashlib

# Ambiguous-result outcomes.
CONFIRMED_EXECUTED = "confirmed_executed"
CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
STILL_AMBIGUOUS = "still_ambiguous"


def hosted_idempotency_key(*, workspace_uuid, expected_login, expected_server, job_id, operation,
                           strategy_id="") -> str:
    """Deterministic, collision-free hosted execution idempotency key. Same inputs → same key; ANY differing
    component (workspace / login / server / job / operation / strategy) → a different key. Secret-free."""
    parts = [
        f"ws={str(workspace_uuid or '').strip()}",
        f"login={str(expected_login or '').strip()}",
        f"server={str(expected_server or '').strip()}",
        f"job={str(job_id or '').strip()}",
        f"op={str(operation or '').strip()}",
        f"strat={str(strategy_id or '').strip()}",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return "HWX-" + digest[:32]


def classify_ambiguous_result(*, reconciliation_authoritative: bool, order_found: bool,
                              position_found: bool, deal_found: bool) -> str:
    """Classify an ambiguous send from reconciled broker/terminal truth. Fail-closed: absence of evidence is
    ``CONFIRMED_NOT_EXECUTED`` ONLY when the reconciliation query was itself authoritative/complete; otherwise
    ``STILL_AMBIGUOUS``. Any positive evidence ⇒ ``CONFIRMED_EXECUTED`` (adopt the existing result, never
    re-send)."""
    if order_found is True or position_found is True or deal_found is True:
        return CONFIRMED_EXECUTED
    if reconciliation_authoritative is True:
        return CONFIRMED_NOT_EXECUTED
    return STILL_AMBIGUOUS  # could not authoritatively prove non-execution → do not retry


def may_retry_after_ambiguous(classification: str) -> bool:
    """The ONLY class eligible for a controlled retry is CONFIRMED_NOT_EXECUTED. Everything else (executed or
    still-ambiguous) must NOT be re-sent."""
    return classification == CONFIRMED_NOT_EXECUTED
