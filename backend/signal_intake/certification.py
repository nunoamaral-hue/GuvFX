"""
GFX-PKT-WAYOND-PARSER-CERTIFICATION — replay + certification engine (repo-only).

Replays a corpus of REAL Wayond messages through the wayond_v1 parser + the
dispatcher's content precedence and certifies each message against its human-
labelled ``expected_type``. Pure: no Telegram, no DB, no network, no order.

``wayond_corpus.json`` is the permanent regression suite — every real message Nuno
supplies becomes a fixed entry. The corpus defines reality; this module never
invents message formats, it only reports what the real parser does with real text.

Taxonomy (packet vocabulary):
    ENTRY_SIGNAL  a new tradeable BUY/SELL (order + stop-loss)  -> intake for approval
    UPDATE        TP-hit / move-SL follow-up                    -> recorded, not traded
    WARNING       news / NFP caution                            -> safely quarantined
    CHATTER       general discussion                            -> safely quarantined
    STALE         outside the acquisition freshness window      -> dropped
    QUARANTINED   edited / media / empty / malformed            -> dropped
    UNKNOWN       format the parser does not recognise          -> dropped (fail-closed)

Safety invariant certified here: ONLY an ENTRY_SIGNAL may become tradeable; every
other type must NOT (fail-closed). A missed ENTRY_SIGNAL and a non-signal classified
as ENTRY_SIGNAL are the only UNSAFE verdicts.
"""

from __future__ import annotations

import json
from pathlib import Path

from intelligence.telegram_source import Kind, parse_message

CORPUS_PATH = Path(__file__).with_name("wayond_corpus.json")

TAXONOMY = (
    "ENTRY_SIGNAL", "UPDATE", "WARNING", "CHATTER", "STALE", "QUARANTINED", "UNKNOWN",
)

# The content parser can positively DETECT only these two; WARNING/CHATTER are
# semantic labels the parser (correctly, safely) treats as UNKNOWN -> quarantine
# until a real message proves a distinction is needed.
_TRADEABLE = "ENTRY_SIGNAL"


def classify(text, *, is_edit=False, media=False, stale=False):
    """Classify one message to a taxonomy label, mirroring the dispatcher's content
    precedence (signal_intake.acquisition._classify) exactly. Pure — no DB.

    Precedence: stale > media > edited > empty > parser(SIGNAL/UPDATE/UNKNOWN). A
    drift test asserts this stays in lock-step with the real dispatcher.
    """
    if stale:
        return "STALE"
    if media:
        return "QUARANTINED"
    if is_edit:
        return "QUARANTINED"
    if not (text or "").strip():
        return "QUARANTINED"
    parsed = parse_message(text)
    if parsed.kind == Kind.SIGNAL and parsed.is_tradeable_shape():
        return "ENTRY_SIGNAL"
    if parsed.kind == Kind.UPDATE:
        return "UPDATE"
    return "UNKNOWN"


def _verdict(expected, observed):
    """(verdict, safety) for expected-vs-observed. UNSAFE only when a real entry
    signal is missed, or a non-entry message is classified tradeable."""
    if expected == "ENTRY_SIGNAL":
        return ("PASS", "SAFE") if observed == "ENTRY_SIGNAL" else ("FAIL", "UNSAFE")
    # From here, the message is NOT meant to trade — a tradeable classification is a
    # false positive (the single worst outcome).
    if observed == "ENTRY_SIGNAL":
        return "FAIL", "UNSAFE"
    if expected == "UPDATE":
        return ("PASS", "SAFE") if observed == "UPDATE" else ("DEGRADED", "SAFE")
    if expected == "STALE":
        return ("PASS", "SAFE") if observed == "STALE" else ("DEGRADED", "SAFE")
    if expected == "QUARANTINED":
        return ("PASS", "SAFE") if observed in ("QUARANTINED", "UNKNOWN") else ("DEGRADED", "SAFE")
    if expected in ("WARNING", "CHATTER", "UNKNOWN"):
        # Safe as long as it is quarantined/unknown (not traded). Being read as an
        # UPDATE is a benign misclassification (recorded, never traded).
        if observed in ("UNKNOWN", "QUARANTINED"):
            return "PASS", "SAFE"
        return "DEGRADED", "SAFE"
    return "DEGRADED", "SAFE"


def certify_entry(entry):
    """Certify one corpus entry -> result dict."""
    expected = entry.get("expected_type", "")
    if expected not in TAXONOMY:
        raise ValueError(f"entry {entry.get('id')!r}: bad expected_type {expected!r}")
    meta = entry.get("meta") or {}
    observed = classify(
        entry.get("text", ""),
        is_edit=bool(meta.get("is_edit")),
        media=bool(meta.get("media")),
        stale=bool(meta.get("stale")),
    )
    verdict, safety = _verdict(expected, observed)
    return {
        "id": entry.get("id", ""),
        "source": entry.get("source", ""),
        "expected": expected,
        "observed": observed,
        "verdict": verdict,
        "safety": safety,
        "text": (entry.get("text", "") or "").replace("\n", " ⏎ "),
    }


def load_corpus(path=None):
    """Load and validate the corpus. Raises ValueError on a bad expected_type."""
    path = Path(path or CORPUS_PATH)
    data = json.loads(path.read_text())
    entries = data.get("messages", data) if isinstance(data, dict) else data
    ids = set()
    for e in entries:
        et = e.get("expected_type")
        if et not in TAXONOMY:
            raise ValueError(f"corpus entry {e.get('id')!r}: bad expected_type {et!r}")
        eid = e.get("id")
        if not eid or eid in ids:
            raise ValueError(f"corpus entry has missing/duplicate id: {eid!r}")
        ids.add(eid)
    return entries


def build_report(entries=None):
    """Run the corpus and return {results, summary}."""
    entries = entries if entries is not None else load_corpus()
    results = [certify_entry(e) for e in entries]

    by_observed, by_expected, verdicts = {}, {}, {"PASS": 0, "DEGRADED": 0, "FAIL": 0}
    for r in results:
        by_observed[r["observed"]] = by_observed.get(r["observed"], 0) + 1
        by_expected[r["expected"]] = by_expected.get(r["expected"], 0) + 1
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    unsafe = [r["id"] for r in results if r["safety"] == "UNSAFE"]
    degraded = [r["id"] for r in results if r["verdict"] == "DEGRADED"]
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "by_observed": by_observed,
            "by_expected": by_expected,
            "verdicts": verdicts,
            "unsafe": unsafe,
            "degraded": degraded,
            "certified": not unsafe and verdicts["FAIL"] == 0,
        },
    }
