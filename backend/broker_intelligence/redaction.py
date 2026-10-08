"""Broker Intelligence — redaction of sensitive financial-authorization material from projections/logs.

Broker emails can carry CONFIRMATION URLs / authorization tokens (e.g. a "confirm your withdrawal" link) that are
sensitive: following one could MOVE FUNDS (Sponsor packet 2026-10-08 §7). Policy:
  * The raw message is kept verbatim in the immutable EvidenceStore (WP2) — evidence is never mutated.
  * Parsers NEVER extract a URL/token into a ``BrokerEvent`` field, and NEVER follow a link.
  * Anything that could surface in a log line or a member-facing projection is passed through ``redact`` first.

This module holds NO authority: it cannot click, fetch, or store a link; it only removes sensitive substrings from
text destined for logs/UI. Deterministic, pure, no I/O.
"""
from __future__ import annotations

import re

# Links: an explicit http(s):// URL, OR a scheme-less "domain.tld" followed by a path '/', a query '?', or a
# ':port' (TradersWay confirmation links can be scheme-less in text). Masks the whole run so no clickable
# authorization target survives in a log/projection.
_URL = re.compile(r"(?:https?://|(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?[/?])\S*", re.IGNORECASE)
# BEST-EFFORT token masking for LOG LINES ONLY (defence in depth). A contiguous run (base64-standard alphabet, no
# hyphen — so UUIDs/correlation-ids split rather than mask whole) that is long AND mixes a letter and a digit. This
# excludes what must not be over-masked here: UPPER_SNAKE enum values (no digit) and pure-numeric references (no
# letter). NOTE: the PRIMARY guarantee is that the parser never EMITS a URL/token into a stored BrokerEvent field
# (structured fields are rendered from DB columns, never via redact()); this helper only sanitises free text in logs,
# where bounded over-masking of a long opaque identifier is acceptable.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_])(?=[A-Za-z0-9+/=_]*[A-Za-z])(?=[A-Za-z0-9+/=_]*\d)[A-Za-z0-9+/=_]{20,}")
_REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Return ``text`` with URLs and long opaque tokens masked. Safe for logs and ordinary projections. Never
    raises on non-str (coerces)."""
    s = "" if text is None else str(text)
    s = _URL.sub(_REDACTED, s)
    s = _TOKEN.sub(_REDACTED, s)
    return s


def contains_sensitive(text: str) -> bool:
    """True if ``text`` contains a URL or a long opaque token — used by tests/guards to assert that an emitted
    field never carries authorization material."""
    s = "" if text is None else str(text)
    return bool(_URL.search(s) or _TOKEN.search(s))
