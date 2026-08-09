"""core.host_cert — evidence-driven Hosted Workspace host-certification stage (ADR-0035 amendment).

Replaces the previous hard-coded, permanent "host certification is always BLOCKED (Sponsor)" posture with
a durable, config-driven **stage** that advances as the (Customer-Zero / disposable) host certification
actually proceeds:

    NOT_STARTED  →  IN_PROGRESS  →  BLOCKED_ON_HUMAN  →  CERTIFIED

The stage is **authoritative config** (settings-first-then-env), NOT a feature flag. This is the load-
bearing distinction the correction requires: enabling a feature flag must NEVER make certification green —
only recording a genuine ``CERTIFIED`` stage (backed by durable certification evidence) does. Any
unrecognised value fails safe to ``NOT_STARTED`` (never CERTIFIED).
"""
from __future__ import annotations

import os

from django.conf import settings

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
BLOCKED_ON_HUMAN = "BLOCKED_ON_HUMAN"
CERTIFIED = "CERTIFIED"
STAGES = (NOT_STARTED, IN_PROGRESS, BLOCKED_ON_HUMAN, CERTIFIED)


def host_cert_stage() -> str:
    """The current Hosted Workspace host-certification stage (read live, settings-first-then-env).
    Fails safe to ``NOT_STARTED`` for any missing/unrecognised value — never CERTIFIED by accident."""
    val = getattr(settings, "HOSTED_HOST_CERT_STAGE", None)
    if val is None:
        val = os.getenv("HOSTED_HOST_CERT_STAGE", "")
    v = str(val).strip().upper()
    return v if v in STAGES else NOT_STARTED


def is_certified() -> bool:
    return host_cert_stage() == CERTIFIED
