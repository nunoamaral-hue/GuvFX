"""broker_public — central PUBLIC disclosure-control layer (Stream C / ADR-0080).

Fail-closed primitives that stand between member-scoped ``Withdrawal`` evidence and any PUBLIC broker-withdrawal
statistic. Every function here is pure/deterministic, emits NO member-identifiable datum, and defaults to the most
conservative (least-disclosing) outcome. It CENTRALISES the disclosure decisions Stream C depends on so every
attack-class control lives in one reviewable place:

* text safety (malicious broker names / injected free text): redact URLs+tokens, strip control chars, HTML-escape,
  length-cap — and a URL allow-list (http/https only; ``javascript:`` / ``data:`` never pass);
* exact-timestamp coarsening — no sub-day precision on any public timestamp (defeats fine differencing);
* a stage-interval chokepoint — an impossible/negative interval collapses to ``None``, mirroring
  ``Withdrawal.duration_seconds()`` so a clock-skewed pair is never a fabricated 0 or a negative;
* cohort eligibility — k-anonymity on BOTH distinct contributors AND observation count, returning an explicit
  ``Reason`` code (SUPPRESSED / PARTIAL / UNAVAILABLE), never zero or fabricated data;
* single-account dominance — re-uses the winsorise water-filling (no one member can move a public figure);
* small-bucket k-anonymity suppression — re-uses the complementary/differencing suppression;
* a COMPLETE lifecycle vocabulary covering ALL eleven ``Withdrawal.Status`` values (no silent 'unresolved' collapse).

It has no authority (no fetch/store/execute), reads nothing, and is never a financial action. The winsorise and
small-bucket helpers are the SAME implementations the pre-existing WITHDRAWAL_PROCESSING aggregate uses — re-exported
(not reimplemented) here so the two public surfaces share one audited privacy core.
"""
from __future__ import annotations

from django.utils.html import escape

from broker_intelligence.models import Withdrawal
from broker_intelligence.redaction import redact

# Re-export the audited privacy primitives from the existing aggregation engine so the central layer references ONE
# implementation (the WITHDRAWAL_PROCESSING aggregate and the Stream C aggregate share the same winsorise + k-anon).
from .aggregation import _suppress_small_buckets as suppress_small_buckets  # noqa: F401  (public re-export)
from .aggregation import _winsorise_counts as winsorise_counts  # noqa: F401  (public re-export)

MAX_PUBLIC_TEXT = 240


class State:
    """Disclosure outcome for a public statistic (richer than the 2-value BrokerMetricAggregate.State). PARTIAL marks
    a sub-metric withheld while the cohort as a whole publishes; SUPPRESSED marks a cohort withheld for k-anonymity;
    UNAVAILABLE marks no eligible evidence (not certified, none genuine, or ambiguous identity)."""

    PUBLISHED = "PUBLISHED"
    PARTIAL = "PARTIAL"
    SUPPRESSED = "SUPPRESSED"
    UNAVAILABLE = "UNAVAILABLE"


class Reason:
    """Stable, non-identifying reason codes. They explain WHY a figure is withheld without revealing any count."""

    OK = "OK"
    NOT_CERTIFIED = "NOT_CERTIFIED"                      # broker absent from CERTIFIED_BROKERS (fail-closed default)
    NO_ELIGIBLE_EVIDENCE = "NO_ELIGIBLE_EVIDENCE"        # certified but no genuine, account-resolved, consenting rows
    AMBIGUOUS_BROKER_IDENTITY = "AMBIGUOUS_BROKER_IDENTITY"   # no authoritative source_slug to resolve the broker
    BELOW_MIN_CONTRIBUTORS = "BELOW_MIN_CONTRIBUTORS"   # fewer than min distinct accounts
    BELOW_MIN_SAMPLES = "BELOW_MIN_SAMPLES"             # fewer than min observations
    SINGLE_ACCOUNT_DOMINANCE = "SINGLE_ACCOUNT_DOMINANCE"    # winsorise collapsed the cohort below threshold
    BELOW_MIN_COMPLETED = "BELOW_MIN_COMPLETED"         # a speed/stage distribution below the completed floor
    STAGE_ANCHORS_MISSING = "STAGE_ANCHORS_MISSING"     # a stage interval has too few both-anchor pairs


# COMPLETE public lifecycle vocabulary: EVERY Withdrawal.Status maps to an explicit public bucket. (The pre-existing
# WITHDRAWAL_PROCESSING map in aggregation.py deliberately collapses REJECTED/CANCELLED to 'terminal_not_completed'
# and does not map the Stream B states; Stream C keeps terminals DISTINCT and maps all four Stream B states so no
# status silently falls through to 'unresolved'.) 'broker_reported_completed' is NEVER 'funds received'.
PUBLIC_LIFECYCLE = {
    Withdrawal.Status.REQUESTED: "requested",
    Withdrawal.Status.PENDING: "requested",
    Withdrawal.Status.CONFIRMATION_REQUIRED: "confirmation_required",
    Withdrawal.Status.CONFIRMED: "confirmed",
    Withdrawal.Status.PROCESSING: "processing",
    Withdrawal.Status.COMPLETED: "broker_reported_completed",
    Withdrawal.Status.REJECTED: "terminal_rejected",
    Withdrawal.Status.CANCELLED: "terminal_cancelled",
    Withdrawal.Status.EXPIRED: "terminal_expired",
    Withdrawal.Status.FAILED: "terminal_failed",
    Withdrawal.Status.UNRESOLVED: "unresolved",
}

# Build-time completeness guard: a future Withdrawal.Status added without a public mapping fails import here rather
# than silently collapsing to 'unresolved' in a live public statistic (the reconciliation gotcha this layer closes).
assert set(PUBLIC_LIFECYCLE) == set(Withdrawal.Status), (
    "PUBLIC_LIFECYCLE must map every Withdrawal.Status value (an unmapped status would leak as 'unresolved')"
)


def public_lifecycle_bucket(status) -> str:
    """The public lifecycle bucket for a durable status (defensive default 'unresolved', though the import-time
    assert guarantees every status is mapped)."""
    return PUBLIC_LIFECYCLE.get(status, "unresolved")


def safe_public_text(text, *, max_len: int = MAX_PUBLIC_TEXT) -> str:
    """Neutralise an editorial/free-text field for public emission: drop control characters, collapse whitespace,
    redact any URL or long opaque token (via the ingestion-side redactor), length-cap, then HTML-escape so injected
    markup/script cannot reach a browser. A clean name (e.g. 'Pepperstone') passes through unchanged. Fail-closed:
    empty/None -> ''."""
    if not text:
        return ""
    s = str(text)
    s = "".join(ch for ch in s if ch == " " or (ch.isprintable() and ch not in "\t\r\n\f\v"))
    s = " ".join(s.split())
    s = redact(s)                       # mask any URL / long opaque token that has no place in a broker name/summary
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return escape(s)        # Django escape (&<>"') neutralises markup AFTER capping so an entity is never split


def safe_public_url(url, *, max_len: int = MAX_PUBLIC_TEXT) -> str:
    """Emit a public website URL only if it is an http/https absolute URL; anything else (``javascript:``, ``data:``,
    scheme-less, empty) fails closed to ''. The accepted URL is length-capped and HTML-escaped."""
    if not url:
        return ""
    s = " ".join(str(url).split())
    low = s.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return ""
    if len(s) > max_len:
        s = s[:max_len]
    return escape(s)


def coarsen_to_date(dt):
    """Coarsen a datetime to its calendar date (no sub-day precision), or None. Every public timestamp is emitted at
    date granularity so no member's exact transaction instant is exposed and two snapshots cannot be finely
    differenced."""
    if dt is None:
        return None
    return dt.date()


def stage_seconds(start, end):
    """The single Stream C stage-interval chokepoint: whole seconds end-minus-start, or None if EITHER anchor is
    missing OR the interval is impossible (end before start). Mirrors ``Withdrawal.duration_seconds()`` exactly so an
    out-of-order / clock-skewed pair is treated as a missing anchor — never a fabricated 0 and never a negative."""
    if start and end:
        secs = int((end - start).total_seconds())
        return secs if secs >= 0 else None
    return None


def cohort_state(contributor_count: int, sample_size: int, *, min_contributors: int, min_samples: int):
    """Fail-closed k-anonymity gate on a cohort. Returns ``(state, reason)``: UNAVAILABLE/NO_ELIGIBLE_EVIDENCE when
    empty, SUPPRESSED/BELOW_MIN_CONTRIBUTORS or BELOW_MIN_SAMPLES below threshold, else PUBLISHED/OK. Never returns a
    publishable state for a cohort that cannot satisfy BOTH the distinct-contributor and the observation floor."""
    if contributor_count <= 0 or sample_size <= 0:
        return State.UNAVAILABLE, Reason.NO_ELIGIBLE_EVIDENCE
    if contributor_count < min_contributors:
        return State.SUPPRESSED, Reason.BELOW_MIN_CONTRIBUTORS
    if sample_size < min_samples:
        return State.SUPPRESSED, Reason.BELOW_MIN_SAMPLES
    return State.PUBLISHED, Reason.OK
