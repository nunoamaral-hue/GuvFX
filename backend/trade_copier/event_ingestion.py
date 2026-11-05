"""trade_copier.event_ingestion — the SIGNED, account-scoped fast-path event-ingestion endpoint (ADR-0074).

The fast half of the hybrid detector. A per-account bridge emits normalised position-state revisions; this
endpoint VERIFIES the emission (HMAC over a canonical signed envelope that binds the account scope), enforces a
replay window + single-use nonce + monotone-revision idempotency, and PERSISTS the accepted revisions as
immutable evidence (``CopierIngestedRevision``). It does nothing else:

* It creates NO ``ExecutionJob`` and places/modifies/closes NO order.
* It writes NO authoritative ``MasterEvent`` into the poll ledger — the reliability (poll) path remains the
  sole producer of the lifecycle's authoritative ordering (ADR-0071). Unifying the event path into the
  authoritative sequence is a separately-gated next step (it mutates shared ordering).
* It fails CLOSED: when ``COPIER_EVENT_INGESTION_ENABLED`` is off, or the signing secret is unset/short, or a
  signature/identity/replay check fails, nothing is persisted.

The signing construction mirrors the audited ``hosted_workspace.host_protocol`` idiom (canonical-JSON signed
field set, HMAC-SHA256, constant-time compare, caller-supplied durable single-use nonce burn, bounded skew +
expiry). Secrets, signatures and full payloads are NEVER logged. The HMAC key is this endpoint's OWN dedicated
secret (``COPIER_INGEST_HMAC_KEY``) resolved via ``core.credentials.resolve_secret`` — never the conflated
``GUVFX_AGENT_TOKEN`` (security.md RULE 3 / 6).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────── protocol constants ───────────────────────────────
INGEST_PROTOCOL_VERSION = "copier-ingest-1"
INGEST_OPERATION = "copier_event_ingest"
DEFAULT_KEY_ID = "copier-ingest"
DEFAULT_MAX_SKEW_SECONDS = 30
MAX_EXPIRY_WINDOW_SECONDS = 600          # a signed request may not be valid for more than 10 minutes
MAX_BODY_BYTES = 262_144                 # 256 KiB hard cap (well under Django's 2.5 MB default)
MAX_REVISIONS_PER_BATCH = 500
MAX_NONCE_LEN = 80                       # matches CopierIngestNonce.nonce varchar(80) — bound before the insert

_SIGNED_FIELDS = (
    "protocol_version", "account_id", "operation", "key_id",
    "timestamp", "expiry", "nonce", "correlation_id", "payload_digest",
)
_MIN_SECRET_LEN = 32


class IngestError(Exception):
    """A structurally-sanitised ingestion rejection. ``reason`` is a stable code (never a secret)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ─────────────────────────────── pure signing primitives (no Django) ───────────────────────────────

def canonical_bytes(obj) -> bytes:
    """Deterministic canonical JSON encoding used for both signing and the payload digest."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_digest(payload) -> str:
    """SHA-256 hex of the canonical payload (binds the carried body to the signature)."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _expected_signature(secret: str, signed: dict) -> str:
    body = canonical_bytes({k: signed[k] for k in _SIGNED_FIELDS})
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def sign_ingest_request(*, account_id: int, secret: str, nonce: str, now: float,
                        payload: dict, key_id: str = DEFAULT_KEY_ID, correlation_id: str = "",
                        ttl_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Build a signed ingestion envelope (used by tests and a future bridge emitter; NEVER by production
    code here). Returns ``{"signed": {...}, "signature": hex, "payload": payload}``."""
    signed = {
        "protocol_version": INGEST_PROTOCOL_VERSION,
        "account_id": int(account_id),
        "operation": INGEST_OPERATION,
        "key_id": str(key_id),
        "timestamp": float(now),
        "expiry": float(now) + float(ttl_seconds),
        "nonce": str(nonce),
        "correlation_id": str(correlation_id or ""),
        "payload_digest": payload_digest(payload),
    }
    return {"signed": signed, "signature": _expected_signature(secret, signed), "payload": payload}


def verify_ingest_request(envelope: dict, *, keyring: dict, now: float,
                          nonce_burn: Callable[[str, float], bool],
                          max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Verify a signed ingestion envelope. PURE except for the injected ``now`` and ``nonce_burn``.

    Raises ``IngestError(reason)`` on any failure; returns the validated ``signed`` dict + ``payload`` on
    success. The nonce is burned (single-use) ONLY after the signature verifies, so an unauthenticated request
    can never consume a nonce."""
    if not isinstance(envelope, dict):
        raise IngestError("malformed_envelope")
    signed = envelope.get("signed")
    signature = envelope.get("signature")
    payload = envelope.get("payload")
    if not isinstance(signed, dict) or not isinstance(signature, str) or payload is None:
        raise IngestError("malformed_envelope")
    for field in _SIGNED_FIELDS:
        if field not in signed:
            raise IngestError("missing_signed_field")
    if signed.get("protocol_version") != INGEST_PROTOCOL_VERSION:
        raise IngestError("bad_protocol_version")
    if signed.get("operation") != INGEST_OPERATION:
        raise IngestError("bad_operation")
    key_id = str(signed.get("key_id") or "")
    secret = keyring.get(key_id)
    if not secret:
        raise IngestError("unknown_key_id")

    expected = _expected_signature(secret, signed)
    if not hmac.compare_digest(expected, signature):
        raise IngestError("bad_signature")

    # Timestamp / expiry window (only AFTER the signature is proven — never trust an unsigned clock claim).
    try:
        ts = float(signed["timestamp"])
        expiry = float(signed["expiry"])
    except (TypeError, ValueError):
        raise IngestError("bad_timestamp")
    if abs(float(now) - ts) > float(max_skew_seconds):
        raise IngestError("timestamp_skew")
    if float(now) > expiry:
        raise IngestError("request_expired")
    if expiry - ts > float(MAX_EXPIRY_WINDOW_SECONDS):
        raise IngestError("expiry_too_far")

    # Payload integrity.
    if not hmac.compare_digest(payload_digest(payload), str(signed.get("payload_digest") or "")):
        raise IngestError("payload_digest_mismatch")

    # Single-use replay burn (durable, atomic) — last so a replay of a valid request is still rejected.
    nonce = str(signed.get("nonce") or "")
    if not nonce:
        raise IngestError("missing_signed_field")
    if len(nonce) > MAX_NONCE_LEN:
        raise IngestError("nonce_too_long")   # bounded before the DB insert (sanitised 400, never a 500)
    if not nonce_burn(nonce, expiry):
        raise IngestError("nonce_replayed")

    return {"signed": signed, "payload": payload}


# ─────────────────────────────── Django-aware helpers (keyring, nonce, service) ───────────────────────────────

def build_keyring() -> dict:
    """Resolve the endpoint's dedicated HMAC key (fail closed). Its OWN secret — never ``GUVFX_AGENT_TOKEN``."""
    from core.credentials import resolve_secret  # local import keeps the pure half import-light
    secret = resolve_secret(
        "COPIER_INGEST_HMAC_KEY", aliases=("COPIER_INGEST_HMAC_SECRET",),
        purpose="copier signed event ingestion", min_length=_MIN_SECRET_LEN)
    return {DEFAULT_KEY_ID: secret}


def make_nonce_burn() -> Callable[[str, float], bool]:
    """A durable single-use nonce burn backed by ``CopierIngestNonce`` (unique insert = first use)."""
    from datetime import datetime, timezone as _tz
    from django.db import IntegrityError, transaction
    from trade_copier.models import CopierIngestNonce

    def _burn(nonce: str, expiry_epoch: float) -> bool:
        expires_at = datetime.fromtimestamp(float(expiry_epoch), tz=_tz.utc)
        try:
            with transaction.atomic():
                CopierIngestNonce.objects.create(nonce=nonce, expires_at=expires_at)
            return True
        except IntegrityError:
            return False
    return _burn


@dataclass(frozen=True)
class IngestOutcome:
    accepted: int
    duplicates: int
    rejected: tuple[tuple[int, int, str], ...]
    changes: tuple[str, ...]             # sanitised named change types detected (evidence)
    identity_ok: bool


def _revisions_from_payload(payload: dict):
    """Build ``PositionRevision`` objects from the validated payload (pure; no persistence)."""
    from datetime import datetime
    from django.utils.dateparse import parse_datetime
    from trade_copier.revisions import PositionRevision

    def _dt(v):
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v
        return parse_datetime(str(v))

    def _f(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rows = payload.get("revisions")
    if not isinstance(rows, list):
        raise IngestError("malformed_payload")
    if not rows:
        raise IngestError("empty_batch")
    if len(rows) > MAX_REVISIONS_PER_BATCH:
        raise IngestError("too_many_revisions")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise IngestError("malformed_payload")
        out.append(PositionRevision(
            account_id=int(r.get("account_id") or 0),
            ticket=int(r.get("ticket") or 0),
            revision=int(r.get("revision") or 0),
            observed_time=_dt(r.get("observed_time")),
            broker_event_time=_dt(r.get("broker_event_time")),
            closed=bool(r.get("closed", False)),
            symbol=str(r.get("symbol") or ""),
            side=str(r.get("side") or ""),
            volume=float(r.get("volume") or 0.0),
            sl=_f(r.get("sl")),
            tp=_f(r.get("tp")),
            event_id=str(r.get("event_id") or ""),
            price_open=_f(r.get("price_open")),
            open_time=_dt(r.get("open_time")),
            original_volume=_f(r.get("original_volume")),
            close_reason=str(r.get("close_reason") or ""),
            broker_login=str(r.get("broker_login") or ""),
            broker_server=str(r.get("broker_server") or ""),
        ))
    return out


def ingest_revisions(*, account_id: int, payload: dict, body_digest: str, correlation_id: str) -> IngestOutcome:
    """GATED persistence of a verified emission batch. Assumes the caller has verified the signature and the
    gate (``copier_event_ingestion_enabled``). Persists immutable ``CopierIngestedRevision`` rows idempotently,
    validates account scope + broker identity, and returns a sanitised outcome. Creates NO order, NO job, NO
    authoritative MasterEvent."""
    from django.db import IntegrityError, transaction
    from trade_copier.models import CopierIngestedRevision, CopierMaster
    from core import observability

    master = (CopierMaster.objects.select_related("trading_account", "trading_account__broker_server")
              .filter(trading_account_id=account_id).first())
    if master is None:
        raise IngestError("account_not_master")

    # Identity scope: every revision must claim the signed account; broker login/server (when present) must
    # match the account's canonical identity (never trust the emission's identity alone).
    expected_login, expected_server = _expected_identity(master.trading_account)
    revisions = _revisions_from_payload(payload)

    accepted = 0
    duplicates = 0
    rejected: list[tuple[int, int, str]] = []
    kept: list = []
    identity_ok = True
    for rev in revisions:
        if rev.account_id != int(account_id):
            rejected.append((rev.ticket, rev.revision, "account_scope_violation"))
            identity_ok = False
            continue
        ok, why = rev.structurally_valid()
        if not ok:
            rejected.append((rev.ticket, rev.revision, why))
            continue
        if not _identity_matches(rev, expected_login, expected_server):
            rejected.append((rev.ticket, rev.revision, "identity_mismatch"))
            identity_ok = False
            continue
        kept.append(rev)

    for rev in kept:
        try:
            with transaction.atomic():
                CopierIngestedRevision.objects.create(
                    master=master, account_id=int(account_id), ticket=rev.ticket, revision=rev.revision,
                    source="EVENT", event_id=rev.event_id[:64], payload_digest=(body_digest or "")[:64],
                    closed=rev.closed, symbol=rev.symbol[:64], side=rev.side[:8],
                    volume=_dec(rev.volume), original_volume=_dec_or_none(rev.original_volume),
                    sl=_dec_or_none(rev.sl), tp=_dec_or_none(rev.tp), price_open=_dec_or_none(rev.price_open),
                    open_time=rev.open_time, close_reason=rev.close_reason[:32],
                    broker_login=rev.broker_login[:64], broker_server=rev.broker_server[:128],
                    broker_event_time=rev.broker_event_time, observed_time=rev.observed_time,
                    correlation_id=correlation_id[:40])
            accepted += 1
        except IntegrityError:
            duplicates += 1   # a re-sent (master, ticket, revision) — idempotent, not an error

    changes = _classify_batch(master, kept)
    observability.log_stage("copier_event_ingest", correlation_id, account_id=int(account_id),
                            accepted=accepted, duplicates=duplicates, rejected=len(rejected),
                            changes=len(changes), identity_ok=identity_ok)
    return IngestOutcome(accepted=accepted, duplicates=duplicates, rejected=tuple(rejected),
                         changes=tuple(changes), identity_ok=identity_ok)


def _classify_batch(master, kept) -> list[str]:
    """Name the change each accepted non-stale revision represents vs the prior persisted state for its ticket
    (Phase-5 detection as evidence). Read-only; returns sanitised change-type names only."""
    from trade_copier.models import CopierIngestedRevision
    from trade_copier import change_types
    from trade_copier.diff import ObservedPosition
    names: list[str] = []
    for rev in kept:
        prior = (CopierIngestedRevision.objects
                 .filter(master=master, ticket=rev.ticket, revision__lt=rev.revision)
                 .order_by("-revision").first())
        prev_positions = []
        if prior is not None and not prior.closed:
            prev_positions = [ObservedPosition(
                ticket=prior.ticket, symbol=prior.symbol, side=prior.side, volume=float(prior.volume),
                sl=(float(prior.sl) if prior.sl is not None else None),
                tp=(float(prior.tp) if prior.tp is not None else None))]
        new_positions = [] if rev.closed else [rev.to_observed_position()]
        for ch in change_types.classify_position_changes(prev_positions, new_positions):
            names.append(ch.change_type)
    return names


def _expected_identity(trading_account):
    try:
        from execution.snapshot_transport import expected_identity
        return expected_identity(trading_account)
    except Exception:  # noqa: BLE001 — identity helper must never leak; fail closed to "no expectation"
        return (None, None)


def _identity_matches(rev, expected_login, expected_server) -> bool:
    """Fail-closed broker-identity cross-check. When the emission carries no login we accept (the account
    scope is already bound by the signature + the account_id check); when it DOES carry one it must match."""
    if not rev.broker_login:
        return True
    if expected_login is None:
        return False
    if str(rev.broker_login).strip() != str(expected_login).strip():
        return False
    if rev.broker_server and expected_server and \
            str(rev.broker_server).strip().casefold() != str(expected_server).strip().casefold():
        return False
    return True


def prune_ingest_nonces(*, now=None, dry_run: bool = True, margin_seconds: int = 0) -> dict:
    """Bounded, safe retention for ``CopierIngestNonce`` (ADR-0074 Phase-6). Dry-run by default.

    Deletes ONLY nonces whose validity window has fully elapsed (``expires_at < now - margin``), so replay
    protection inside the active window is NEVER weakened — an in-window nonce is always retained, so a replay
    of a still-valid signed request is still rejected. A non-negative ``margin_seconds`` only makes pruning MORE
    conservative (it keeps recently-expired nonces a little longer). The counts are secret-free: a nonce value is
    never read or logged, only tallied. Unscheduled: run on demand. Returns ``{eligible, pruned, retained,
    dry_run}``."""
    from datetime import timedelta
    from django.utils import timezone as _tz
    from trade_copier.models import CopierIngestNonce

    now = now or _tz.now()
    margin = max(0, int(margin_seconds))
    cutoff = now - timedelta(seconds=margin)
    expired = CopierIngestNonce.objects.filter(expires_at__lt=cutoff)
    eligible = expired.count()
    total = CopierIngestNonce.objects.count()
    pruned = 0
    if not dry_run and eligible:
        pruned = expired.delete()[0]
    return {"eligible": eligible, "pruned": pruned, "retained": total - pruned, "dry_run": bool(dry_run)}


def _dec(v):
    from decimal import Decimal
    return Decimal(str(v or 0.0))


def _dec_or_none(v):
    from decimal import Decimal
    return None if v is None else Decimal(str(v))
