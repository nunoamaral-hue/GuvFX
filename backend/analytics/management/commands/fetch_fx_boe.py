"""analytics/management/commands/fetch_fx_boe.py — idempotent Bank of England XUDLUSS ingestion/backfill (DARK).

Writes append-only ``DailyFxObservation`` rows (``source=BOE``) from the keyless BoE IADB CSV. Mirrors
``fetch_fx_daily`` (Fed H.10) exactly: the dashboard/request path NEVER runs this; it is an out-of-band
scheduled/operator job, UNSCHEDULED in production until a separate Sponsor GO. The request path reads the DB only.

Idempotent (identical ``observation_checksum`` skipped; a revised value is a new revision). On any fetch/parse
failure it leaves the store unchanged and emits a BOUNDED message (never the raw payload). Writes go through the
dedicated ``fx_ingest`` identity (fail-closed) — never the web-runtime connection. Source safety: HTTPS only,
size + time ceilings, column + series identity validated (wrong-series guard), USD-per-GBP direction intrinsic,
bad values rejected; a ``--fixture`` path makes it run data-free for tests / offline (no-egress) backfill.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.request
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analytics import fx_boe as BOE
from analytics.fx_ingest import FxIngestConfigError, append_fx_observation, resolve_fx_ingest_alias

# Keyless BoE Statistical Interactive Database (IADB) CSV for the XUDLUSS daily spot series (no API key). HTTPS.
_BASE = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
_MAX_BYTES = 5 * 1024 * 1024
_TIMEOUT_S = 15


def _default_url(days: int = 30) -> str:
    today = timezone.now().date()
    frm = today - _dt.timedelta(days=days)
    def fmt(d): return d.strftime("%d/%b/%Y")
    return (f"{_BASE}?csv.x=yes&Datefrom={fmt(frm)}&Dateto={fmt(today)}"
            f"&SeriesCodes={BOE.SERIES}&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")


class Command(BaseCommand):
    help = "Ingest/backfill Bank of England XUDLUSS (USD per GBP) daily rates into the append-only FX store (DARK)."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=None, help="BoE IADB keyless CSV URL (HTTPS only). Default: last 30 days.")
        parser.add_argument("--fixture", default=None,
                            help="Read the CSV from a LOCAL file instead of the network (tests / offline backfill).")
        parser.add_argument("--dry-run", action="store_true", help="Parse + report; write nothing.")

    def _read_payload(self, opts):
        if opts["fixture"]:
            with open(opts["fixture"], encoding="utf-8") as fh:
                return fh.read(), f"fixture:{opts['fixture']}"
        url = opts["url"] or _default_url()
        if urlparse(url).scheme != "https":
            raise CommandError("refusing non-HTTPS URL")
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "guvfx-fx-ingest"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = resp.read(_MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 — bounded, no payload echo
            raise CommandError(f"fetch failed ({type(exc).__name__}); store unchanged")
        if len(data) > _MAX_BYTES:
            raise CommandError("payload exceeds size ceiling; store unchanged")
        return data.decode("utf-8", "replace"), url

    def handle(self, *args, **opts):
        alias = None
        if not opts["dry_run"]:
            try:
                alias = resolve_fx_ingest_alias()
            except FxIngestConfigError as e:
                raise CommandError(str(e))
        text, source_url = self._read_payload(opts)
        try:
            result = BOE.parse_xudluss_csv(text)
        except BOE.FxParseError as e:
            raise CommandError(f"payload rejected: {e}; store unchanged")
        now = timezone.now()
        new_rows = dup_rows = 0
        if not opts["dry_run"]:
            for obs in result.accepted:
                written, _rev = append_fx_observation(
                    effective_date=obs.effective_date, rate=obs.rate,
                    observation_checksum=obs.observation_checksum, raw_line=obs.raw_line,
                    source_url=source_url, retrieved_at=now,
                    source=BOE.SOURCE, base=BOE.BASE, quote=BOE.QUOTE, source_series=BOE.SERIES, using=alias)
                new_rows += 1 if written else 0
                dup_rows += 0 if written else 1
        latest = max((o.effective_date for o in result.accepted), default=None)
        summary = {
            "origin": source_url, "series": BOE.SERIES, "direction": BOE.DIRECTION, "ingest_alias": alias,
            "payload_sha256_prefix": result.payload_sha256[:12],
            "rows": result.row_count, "accepted": len(result.accepted), "missing": result.missing_count,
            "rejected": len(result.rejected), "rejected_reasons": [r.reason for r in result.rejected[:10]],
            "written_new": new_rows, "duplicate_skipped": dup_rows, "dry_run": bool(opts["dry_run"]),
            "latest_effective_date": latest.isoformat() if latest else None,
        }
        self.stdout.write(json.dumps(summary))
