"""WAYOND Trader Match (WTM) broker-metrics PREVIEW — SIMULATED illustration only. DARK.

Produces a clearly-labelled SIMULATED broker-performance scorecard for the investor demo, illustrating what
multi-sample WAYOND Trader Match withdrawal-processing analytics would look like once enough verified, consented
samples exist. It NEVER reads or writes ``Withdrawal`` / ``BrokerEvent`` / ``PublicBroker`` / member data, NEVER
touches ``CERTIFIED_BROKERS``, and NEVER publishes a real rating — every figure here is synthetic and flagged
``simulated: True``. The AUTHENTIC public state is reported truthfully as ``INSUFFICIENT_VERIFIED_DATA`` (nothing is
certified or published). Pure + deterministic (no DB, no I/O, no randomness).
"""
from __future__ import annotations

import statistics

# Deterministic synthetic request→completion processing-time samples (minutes), per broker — a plausible
# distribution used purely to ILLUSTRATE the analytics. These are not observations of any real withdrawal.
_SAMPLES = {
    "tradersway": ("TradersWay",
                   [12, 18, 22, 28, 31, 33, 33, 35, 38, 40, 44, 47, 52, 58, 61, 70, 77, 88, 95, 120, 150, 240]),
    "is6-technologies": ("IS6 Technologies",
                         [8, 11, 15, 19, 24, 26, 29, 33, 37, 41, 48, 55, 63, 72, 90, 110, 140]),
}
_HIST_BUCKETS = [(0, 30, "0–30m"), (30, 60, "30–60m"), (60, 120, "1–2h"), (120, 10 ** 9, "2h+")]
_LIFECYCLE_PCT = {"tradersway": {"completed": 86, "pending": 9, "rejected": 5},
                  "is6-technologies": {"completed": 91, "pending": 6, "rejected": 3}}
_CONTRIBUTORS = {"tradersway": 11, "is6-technologies": 8}


def _scorecard(slug: str) -> dict:
    name, mins = _SAMPLES[slug]
    srt = sorted(mins)
    p90 = srt[min(len(srt) - 1, int(round(0.9 * (len(srt) - 1))))]
    hist = [{"bucket": lbl, "count": sum(1 for m in mins if lo <= m < hi)} for lo, hi, lbl in _HIST_BUCKETS]
    return {
        "slug": slug,
        "display_name": name,
        "simulated": True,
        "sample_size": len(mins),
        "contributor_count": _CONTRIBUTORS[slug],
        "methodology_version": 1,
        "withdrawal_processing": {
            "median_minutes": int(statistics.median(mins)),
            "avg_minutes": int(round(statistics.mean(mins))),
            "p90_minutes": int(p90),
            "min_minutes": min(mins),
            "max_minutes": max(mins),
            "histogram": hist,
            "lifecycle_pct": _LIFECYCLE_PCT[slug],
        },
    }


def wtm_preview_payload() -> dict:
    """The full WTM preview payload. Everything under ``simulated_brokers`` is synthetic (``simulated: True``); the
    AUTHENTIC real-directory state is reported as INSUFFICIENT_VERIFIED_DATA. No member data, no real aggregate."""
    return {
        "preview": True,
        "not_a_deployed_integration": True,
        "title": "WAYOND Trader Match — Broker Performance (Preview)",
        "authentic_state": "INSUFFICIENT_VERIFIED_DATA",
        "authentic_note": (
            "No broker yet has enough verified, consented samples to publish a real WAYOND Trader Match scorecard, "
            "so the live public directory truthfully shows INSUFFICIENT_VERIFIED_DATA. The scorecards below are a "
            "SIMULATED illustration of the multi-sample analytics this surface will show once the data exists."),
        "simulated_brokers": [_scorecard(s) for s in _SAMPLES],
        "methodology": (
            "Median broker-reported request→completion interval across de-duplicated, per-contributor winsorised, "
            "k-anonymised samples from members who opted in; minimum sample-size and contributor thresholds must be "
            "met before any figure is shown."),
        "privacy": (
            "Aggregate-only. Never a member identity, email, account number, withdrawal reference, individual "
            "transaction, raw notification or evidence pointer. k-anonymity + minority-bucket suppression + "
            "per-contributor winsorisation."),
        "source": "GuvFX authenticated broker-email evidence (request + completion notifications; DKIM/SPF verified).",
        "flow": [
            "GuvFX evidence — authenticated broker emails (DKIM/SPF, content-hash immutable)",
            "Correlated withdrawal observations — per member, consented",
            "Privacy-safe aggregate — k-anonymity, winsorised, minimum samples",
            "WAYOND Trader Match — broker performance page",
        ],
    }
