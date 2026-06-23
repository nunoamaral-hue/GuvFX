"""Acquisition orchestrator (GFX-PKT-006C).

Validates/fingerprints the request, calls ONLY the injected transport, preserves
exact response bytes, validates the response contract and request match, then lands
or quarantines through the raw store. It never normalises automatically — that is
gated separately on VERIFIED timezone evidence.
"""

from __future__ import annotations

import json

from .agent_client import Transport
from .contracts import (
    ContractError,
    ProhibitedKeyError,
    canonical_json_bytes,
    find_prohibited_key,
    validate_request,
    validate_request_response_match,
    validate_response,
)
from .storage import RawStore


def acquire(request: dict, transport: Transport, store: RawStore, *, code_commit: str,
            acquired_at_utc: str, received_at_utc: str) -> dict:
    """Run one acquisition through the injected transport and raw store.

    Returns a structured result dict (status + logical raw object id + checksums +
    root-relative paths only). Never normalises.
    """
    request_bytes = canonical_json_bytes(request)

    # Request-side credential/account-number guard: security stop, no body persisted.
    bad = find_prohibited_key(request)
    if bad:
        store.security_stop(request, request_bytes, b"", bad)
        raise ProhibitedKeyError(f"prohibited key {bad!r} in request; body not persisted")

    validate_request(request)

    # Call ONLY the injected transport. Exact response bytes are preserved.
    response_bytes = transport.export_rates(request_bytes)
    if not isinstance(response_bytes, (bytes, bytearray)):
        raise ContractError("transport must return raw bytes")
    response_bytes = bytes(response_bytes)

    try:
        response_obj = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return store.quarantine_malformed(
            request, request_bytes, response_bytes, code_commit=code_commit,
            acquired_at_utc=acquired_at_utc, received_at_utc=received_at_utc,
            reason="invalid_json",
        )

    # Response-side credential guard before any persistence of the body.
    bad = find_prohibited_key(response_obj)
    if bad:
        store.security_stop(request, request_bytes, response_bytes, bad)
        raise ProhibitedKeyError(f"prohibited key {bad!r} in response; body not persisted")

    try:
        validate_response(response_obj)
        validate_request_response_match(request, response_obj)
    except ProhibitedKeyError:
        store.security_stop(request, request_bytes, response_bytes, "prohibited")
        raise
    except ContractError as exc:
        return store.quarantine_malformed(
            request, request_bytes, response_bytes, code_commit=code_commit,
            acquired_at_utc=acquired_at_utc, received_at_utc=received_at_utc,
            reason=f"contract_invalid:{type(exc).__name__}",
        )

    return store.land(
        request, response_obj, request_bytes, response_bytes, code_commit=code_commit,
        acquired_at_utc=acquired_at_utc, received_at_utc=received_at_utc,
        timezone_status="NOT_EVALUATED",
    )
