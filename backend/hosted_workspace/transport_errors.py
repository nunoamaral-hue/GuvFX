"""hosted_workspace.transport_errors — truthful, sanitised taxonomy for signed host-transport outcomes (ADR-0090).

Packet 046 proved that the single ``host_unavailable`` reason conflated three unrelated conditions: a host that
could not be reached, a host that accepted the connection and closed it before any HTTP response (the executor
daemon's connection-cap ``shutdown_request`` under observation over-subscription), and an explicit HTTP error
status. That conflation sent a live diagnosis toward "host down" when the host was healthy but saturated.

This module is the single source of truth for classifying a raised transport exception into a stable, secret-free
lower_snake reason code. It NEVER inspects or returns a request body, token, signature, URL, header or raw
exception text — only the exception *type* (and, for an HTTP error, the integer status code).

Compatibility: these codes are a strict superset of the legacy ``host_unavailable``. Consumers that merely store
or echo the reason as an opaque breadcrumb (``poll_diagnostics``) keep working unchanged and simply gain a more
specific code; consumers that branch should use :func:`is_host_unavailable_reason`, which groups every
transport-failure code (new and legacy) so their "the observe/op did not reach a usable response" semantics are
preserved byte-for-byte.
"""
from __future__ import annotations

import http.client
import socket
import urllib.error

# ── the canonical vocabulary ──────────────────────────────────────────────────────────────────────────────────
# Connection/DNS/timeout failure BEFORE a usable HTTP exchange — the host was not reached.
HOST_UNREACHABLE = "host_unreachable"
# The connection was accepted but closed before any HTTP response arrived. This is exactly what the executor
# daemon does at its connection cap (``process_request`` -> ``shutdown_request``): socket closed, no bytes, no
# handler, nothing logged host-side. It is NOT, on its own, proof of capacity exhaustion — a mid-exchange reset
# looks the same to the client — so it is labelled by what was OBSERVED (a pre-response drop), never inferred.
HOST_CONNECTION_DROPPED_PRE_RESPONSE = "host_connection_dropped_pre_response"
# The host returned an explicit non-2xx HTTP status. Only the integer status code is retained (never the body).
HOST_HTTP_ERROR = "host_http_error"
# BACKEND-side (not transport) outcome: the local observation-concurrency budget (ADR-0090) is the limiter, so
# excess observations are queued/deferred behind the budget rather than dispatched. Operator-facing label for the
# bounded-observation cycle's deferral metric; it is never produced by :func:`classify_transport_exception`.
HOST_OBSERVATION_CAPACITY_DEFERRED = "host_observation_capacity_deferred"
# A transport exception that matched none of the above. Still NOT the misleading ``host_unavailable`` — generic,
# but truthful about being an unclassified transport fault.
HOST_TRANSPORT_ERROR = "host_transport_error"

# The legacy code. Retained as a named constant so the grouping helper and any compatibility shim can reference it
# explicitly rather than hard-coding the string.
LEGACY_HOST_UNAVAILABLE = "host_unavailable"

# Every code that means "the signed operation did not reach a usable host response". Consumers that previously
# branched on ``reason == "host_unavailable"`` use this set so their semantics are preserved as the vocabulary
# widens.
TRANSPORT_FAILURE_REASONS = frozenset({
    HOST_UNREACHABLE,
    HOST_CONNECTION_DROPPED_PRE_RESPONSE,
    HOST_HTTP_ERROR,
    HOST_TRANSPORT_ERROR,
    LEGACY_HOST_UNAVAILABLE,
})


def is_host_unavailable_reason(reason) -> bool:
    """True iff ``reason`` is any transport-failure code (new taxonomy or the legacy ``host_unavailable``). Use
    this in place of ``reason == "host_unavailable"`` so a consumer's behaviour is unchanged as codes widen."""
    return str(reason or "") in TRANSPORT_FAILURE_REASONS


def classify_transport_exception(exc) -> dict:
    """Map a raised transport exception to ``{"reason": <code>, ...}`` — secret-free, type-only.

    Returns the reason code plus, for an HTTP error, the integer ``http_status`` (nothing else from the response).
    Ordering matters: ``HTTPError`` is a subclass of ``URLError``; ``RemoteDisconnected`` is a subclass of
    ``ConnectionResetError``; ``ConnectionRefusedError`` is a subclass of ``ConnectionError``/``OSError`` — so the
    most specific cases are tested first.
    """
    # 1. Explicit HTTP status (checked before URLError, which it subclasses). Keep ONLY the integer code.
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", None)
        out = {"reason": HOST_HTTP_ERROR}
        if isinstance(code, int):
            out["http_status"] = code
        return out
    # 2. Accepted-then-closed before any HTTP response (cap shutdown_request, reset, truncated/garbled status).
    if isinstance(exc, (http.client.RemoteDisconnected, http.client.BadStatusLine,
                        http.client.IncompleteRead, ConnectionResetError)):
        return {"reason": HOST_CONNECTION_DROPPED_PRE_RESPONSE}
    # 3. URLError wrapping a lower-level cause — unwrap to tell a pre-response drop from a never-reached host.
    if isinstance(exc, urllib.error.URLError):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, (http.client.RemoteDisconnected, ConnectionResetError)):
            return {"reason": HOST_CONNECTION_DROPPED_PRE_RESPONSE}
        return {"reason": HOST_UNREACHABLE}
    # 4. Connection refused / timeout / DNS / other socket errors — the host was not reached usefully.
    if isinstance(exc, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError, OSError)):
        return {"reason": HOST_UNREACHABLE}
    # 5. Anything else — unclassified, but never the misleading "host_unavailable".
    return {"reason": HOST_TRANSPORT_ERROR}
