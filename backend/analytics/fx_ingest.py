"""analytics.fx_ingest — concurrency-safe append-only write of one FX observation revision (F10) via a dedicated
ingestion DB identity (A3).

``select_for_update()`` cannot lock a row that does not yet exist, so two simultaneous FIRST ingestions for the
same ``(source, base, quote, effective_date)`` would both allocate ``revision_number = 1`` and collide. We
serialise writers for that key with a PostgreSQL **advisory transaction lock** (``pg_advisory_xact_lock``), held
to transaction end (commit OR rollback). Under that lock the pre-check + revision numbering are race-free.

A3 — DISTINCT INGESTION IDENTITY: all four operations (advisory lock, the idempotency/revision read, the insert,
and the transaction boundary) run on the SAME ``using`` connection, so they never span two connections. The
ingestion command resolves the dedicated ``fx_ingest`` alias (``resolve_fx_ingest_alias``) and fails CLOSED when
it is not configured — it never silently falls back to the web-runtime ``default`` connection.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Tuple

from django.conf import settings
from django.db import connections, transaction

from analytics import fx_fred as FRED
from analytics.models import DailyFxObservation

FX_INGEST_ALIAS = "fx_ingest"


class FxIngestConfigError(RuntimeError):
    """The dedicated FX ingestion DB alias is absent/incomplete — refuse to ingest (fail-closed)."""


def resolve_fx_ingest_alias() -> str:
    """Return the dedicated FX ingestion DB alias, or fail CLOSED. The alias is configured ONLY from distinct
    ``FX_INGEST_DB_USER`` / ``FX_INGEST_DB_PASSWORD`` (see settings); absent/incomplete configuration means the
    alias is not in ``settings.DATABASES`` and we REFUSE — the ingestion path never falls back to the
    web-runtime ``default`` connection or reuses its credentials."""
    if FX_INGEST_ALIAS in settings.DATABASES:
        return FX_INGEST_ALIAS
    raise FxIngestConfigError(
        "FX ingestion alias 'fx_ingest' is not configured (set distinct FX_INGEST_DB_USER / FX_INGEST_DB_PASSWORD "
        "for a SELECT+INSERT-only ingestion role). Refusing to ingest via the web-runtime connection (fail-closed).")


def append_fx_observation(*, effective_date: date, rate: Decimal, observation_checksum: str, raw_line: str,
                          source_url: str, retrieved_at: datetime, source: str = FRED.SOURCE,
                          base: str = FRED.BASE, quote: str = FRED.QUOTE,
                          using: str = "default") -> Tuple[bool, Optional[int]]:
    """Append one ACCEPTED revision on the ``using`` connection, serialised per key. Returns ``(written,
    revision_number)`` — ``(False, None)`` when the identical observation is already stored (idempotent no-op).
    The advisory lock, the idempotency read, and the insert all run on ``using`` inside one transaction."""
    key = f"{source}|{base}|{quote}|{effective_date.isoformat()}"
    with transaction.atomic(using=using):
        # (1) Serialise concurrent writers for THIS key on THIS connection (released at tx end).
        with connections[using].cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        # (2) Idempotency / revision-allocation read on the SAME connection.
        grp = list(DailyFxObservation.objects.using(using).filter(
            source=source, base_currency=base, quote_currency=quote, effective_date=effective_date
        ).values_list("revision_number", "observation_checksum"))
        if any(cs == observation_checksum for _, cs in grp):
            return (False, None)
        next_rev = (max((rn for rn, _ in grp), default=0)) + 1
        # (3) Insert on the SAME connection.
        obj = DailyFxObservation(
            base_currency=base, quote_currency=quote, effective_date=effective_date, rate=rate,
            source=source, source_series=FRED.SERIES, retrieved_at=retrieved_at,
            observation_checksum=observation_checksum, revision_number=next_rev,
            acceptance_state=DailyFxObservation.STATE_ACCEPTED, source_url=source_url, raw_line=raw_line)
        obj.save(using=using)
        return (True, next_rev)
