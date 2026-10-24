"""analytics/management/commands/fetch_fx_daily.py — idempotent Fed H.10 ``DEXUSUK`` ingestion/backfill (DARK).

Writes append-only ``DailyFxObservation`` rows from the keyless FRED CSV download. The dashboard/request path
NEVER runs this — it is an out-of-band scheduled/operator job (UNSCHEDULED in production until a separate GO).

Idempotent: re-running the same payload writes nothing (an identical ``observation_checksum`` is skipped); a
genuinely REVISED published value is written as a new revision. On any fetch/parse failure it leaves the store
unchanged and emits a BOUNDED alert — it never logs the whole payload (security.md: redact; no raw dumps).

Source safety (ADR-0058 / PD Phase 3): HTTPS only · response-size + request-time ceilings · column + series
identity validated (wrong-series guard) · ``USD per GBP`` direction asserted · missing/nonnumeric/zero/negative/
implausible values rejected (never written) · raw payload hashed · NO automatic cross-source substitution (Bank
of England is monitoring-only) · a ``--fixture`` path makes the job run data-free for tests/CI.
"""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analytics import fx_fred as FRED
from analytics.fx_ingest import FxIngestConfigError, append_fx_observation, resolve_fx_ingest_alias

# Keyless FRED CSV download for the Fed H.10 DEXUSUK series (no API key). HTTPS only.
DEFAULT_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXUSUK"
_MAX_BYTES = 5 * 1024 * 1024    # response-size ceiling (a decades-long daily CSV is far below this)
_TIMEOUT_S = 15                 # request-time ceiling


class Command(BaseCommand):
    help = "Ingest/backfill Fed H.10 DEXUSUK (USD per GBP) daily rates into the append-only FX store (DARK)."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=DEFAULT_URL, help="FRED keyless CSV URL (HTTPS only).")
        parser.add_argument("--fixture", default=None,
                            help="Read the CSV from a LOCAL file instead of the network (tests / offline backfill).")
        parser.add_argument("--dry-run", action="store_true", help="Parse + report; write nothing.")

    def handle(self, *args, **opts):
        # A3: resolve the DEDICATED ingestion identity BEFORE any work when we will write — fail closed (never
        # fall back to the web-runtime connection). A dry-run writes nothing, so it does not require the alias.
        alias = None
        if not opts["dry_run"]:
            try:
                alias = resolve_fx_ingest_alias()
            except FxIngestConfigError as e:
                raise CommandError(str(e))

        text, source_url = self._read_payload(opts)
        try:
            result = FRED.parse_dexusuk_csv(text)
        except FRED.FxParseError as e:
            # Wrong series / columns / shape — refuse without writing; bounded message, no payload body.
            raise CommandError(f"payload rejected: {e}; store unchanged")

        now = timezone.now()
        new_rows = dup_rows = 0
        if not opts["dry_run"]:
            for obs in result.accepted:
                written, _rev = append_fx_observation(
                    effective_date=obs.effective_date, rate=obs.rate,
                    observation_checksum=obs.observation_checksum, raw_line=obs.raw_line,
                    source_url=source_url, retrieved_at=now, using=alias)
                new_rows += 1 if written else 0
                dup_rows += 0 if written else 1

        summary = {
            "origin": source_url, "series": FRED.SERIES, "direction": FRED.DIRECTION,
            "ingest_alias": alias, "payload_sha256_prefix": result.payload_sha256[:12],
            "rows": result.row_count, "accepted": len(result.accepted),
            "missing": result.missing_count, "rejected": len(result.rejected),
            "rejected_reasons": sorted({r.reason for r in result.rejected}),
            "written_new": new_rows, "duplicate_skipped": dup_rows, "dry_run": bool(opts["dry_run"]),
            "latest_effective_date": (max(o.effective_date for o in result.accepted).isoformat()
                                      if result.accepted else None),
        }
        self.stdout.write(json.dumps(summary))

    # ── payload acquisition ──
    def _read_payload(self, opts):
        if opts["fixture"]:
            with open(opts["fixture"], "r", encoding="utf-8") as fh:
                return fh.read(_MAX_BYTES + 1), f"fixture:{opts['fixture']}"
        url = opts["url"]
        if urlparse(url).scheme != "https":
            raise CommandError("refusing non-HTTPS FRED URL; store unchanged")
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "GuvFX-FX-ingest"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                raw = r.read(_MAX_BYTES + 1)
        except Exception as e:  # noqa: BLE001 — bounded alert; never surface or log the payload body
            raise CommandError(f"fetch failed ({type(e).__name__}); store unchanged")
        if len(raw) > _MAX_BYTES:
            raise CommandError("response exceeds size limit; store unchanged")
        return raw.decode("utf-8", "ignore"), url
