"""analytics.fx_ingest — concurrency-safe append-only write of one FX observation revision (F10).

``select_for_update()`` cannot lock a row that does not yet exist, so two simultaneous FIRST ingestions for the
same ``(source, base, quote, effective_date)`` would both allocate ``revision_number = 1`` and collide. We
serialise writers for that key with a PostgreSQL **advisory transaction lock** (``pg_advisory_xact_lock``), held
to transaction end (commit OR rollback). Under that lock the pre-check + revision numbering are race-free:

* identical concurrent ingestion ⇒ exactly one stored observation (the later writer sees the checksum and skips);
* two genuine corrections ⇒ distinct, sequential revisions;
* no duplicate revision number, no lost correction, no unhandled IntegrityError to the caller;
* a rollback during allocation releases the lock and leaves no partial row.

The unique constraints remain the database backstop; serialisation means they are never actually hit for a
same-key race.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Tuple

from django.db import connection, transaction

from analytics import fx_fred as FRED
from analytics.models import DailyFxObservation


def append_fx_observation(*, effective_date: date, rate: Decimal, observation_checksum: str, raw_line: str,
                          source_url: str, retrieved_at: datetime, source: str = FRED.SOURCE,
                          base: str = FRED.BASE, quote: str = FRED.QUOTE) -> Tuple[bool, Optional[int]]:
    """Append one ACCEPTED revision, serialised per key. Returns ``(written, revision_number)`` — ``(False, None)``
    when the identical observation is already stored (idempotent no-op)."""
    key = f"{source}|{base}|{quote}|{effective_date.isoformat()}"
    with transaction.atomic():
        # Serialise concurrent writers for THIS key (released at tx end). hashtextextended -> bigint so distinct
        # dates take distinct lock keys and never block one another.
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        grp = list(DailyFxObservation.objects.filter(
            source=source, base_currency=base, quote_currency=quote, effective_date=effective_date
        ).values_list("revision_number", "observation_checksum"))
        if any(cs == observation_checksum for _, cs in grp):
            return (False, None)                                    # identical observation already stored
        next_rev = (max((rn for rn, _ in grp), default=0)) + 1
        DailyFxObservation.objects.create(
            base_currency=base, quote_currency=quote, effective_date=effective_date, rate=rate,
            source=source, source_series=FRED.SERIES, retrieved_at=retrieved_at,
            observation_checksum=observation_checksum, revision_number=next_rev,
            acceptance_state=DailyFxObservation.STATE_ACCEPTED, source_url=source_url, raw_line=raw_line)
        return (True, next_rev)
