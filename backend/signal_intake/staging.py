"""
GFX-PKT-WAYOND-CORPUS-SEED-READY — real-message intake staging (repo-only, pure).

Turns a raw paste of REAL Wayond messages into a reviewable *draft* — it splits the
paste, runs each message through the certification classifier to PROPOSE a type, and
flags anything that needs Nuno's eyes. It NEVER fabricates messages and NEVER writes
to the permanent corpus: only ``promote()`` appends, and only entries Nuno has
CONFIRMED. The proposed label is the parser's *observed* result (a review aid) — the
ground-truth ``expected_type`` is Nuno's to confirm, so a real signal the parser
misses shows up as a mismatch (a parser gap), not a silent pass.

Paste format (see docs/WAYOND_CERTIFICATION.md):
    one message per block, blocks separated by a line that is exactly ``---``.
    Optional leading @directives per block:
        @type: ENTRY_SIGNAL   # declare the ground-truth type (marks it CONFIRMED)
        @edit                 # message was edited     (meta.is_edit)
        @media                # screenshot/image       (meta.media)
        @reply                # a reply update         (meta.is_reply)
        @stale                # arrived outside window (meta.stale)
        @id: my-slug          # optional explicit id
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .certification import CORPUS_PATH, TAXONOMY, _verdict, classify

_DELIM_RE = re.compile(r"^\s*---+\s*$", re.M)
_SIGNAL_HINT = re.compile(r"\b(BUY|SELL)\b|Stop\s*Loss|\bTP\s*\d", re.I)
_DIRECTIVE_RE = re.compile(r"^@(\w+)\s*:?\s*(.*)$")
_FLAGS = {"edit": "is_edit", "media": "media", "reply": "is_reply", "stale": "stale"}


def _extract_directives(block):
    """Split a block's leading @directive lines from its message body."""
    meta, declared, cid = {}, None, None
    lines = block.split("\n")
    i = 0
    while i < len(lines):
        m = _DIRECTIVE_RE.match(lines[i].strip())
        if not m:
            break
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "type":
            declared = val.upper()
        elif key == "id":
            cid = val
        elif key in _FLAGS:
            meta[_FLAGS[key]] = True
        i += 1
    body = "\n".join(lines[i:]).strip()
    return declared, cid, meta, body


def _slug(text, n=30):
    s = re.sub(r"[^a-z0-9]+", "-", (text.split("\n")[0] or "").lower()).strip("-")
    return s[:n] or "msg"


def parse_paste(text):
    """Split a raw paste into message dicts. Never fabricates — only what was pasted.
    Returns [{text, declared_type, id, meta}] for each non-empty block."""
    entries = []
    for block in _DELIM_RE.split(text or ""):
        declared, cid, meta, body = _extract_directives(block)
        if not body.strip():
            continue
        entries.append({"text": body, "declared_type": declared, "id": cid, "meta": meta})
    return entries


def stage_entries(text):
    """Parse + classify a paste into reviewable staging entries."""
    staged = []
    for i, e in enumerate(parse_paste(text), 1):
        meta = e["meta"]
        observed = classify(e["text"], is_edit=meta.get("is_edit", False),
                            media=meta.get("media", False), stale=meta.get("stale", False))
        declared = e["declared_type"]
        confirmed = declared is not None
        if confirmed and declared not in TAXONOMY:
            confirmed, declared = False, None  # bad @type -> treat as un-declared
        expected = declared or observed  # proposal only when un-declared
        if confirmed:
            verdict, safety = _verdict(expected, observed)
        else:
            verdict, safety = "PROPOSED", "REVIEW"
        needs_review = (
            not confirmed
            or observed == "ENTRY_SIGNAL"                       # always double-check a trade
            or bool(_SIGNAL_HINT.search(e["text"]) and observed != "ENTRY_SIGNAL")  # missed?
        )
        staged.append({
            "id": e["id"] or f"pasted-{i:02d}-{_slug(e['text'])}",
            "text": e["text"],
            "meta": meta,
            "observed": observed,
            "expected_type": expected,
            "confirmed": confirmed,
            "verdict": verdict,
            "safety": safety,
            "needs_review": needs_review,
        })
    return staged


def _unique_id(base, taken):
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def promote(staged, corpus_path=None, *, source="pasted-by-nuno"):
    """Append CONFIRMED staging entries to the permanent corpus. Skips unconfirmed
    entries, duplicate text, and bad expected_types. Returns {added, skipped}."""
    corpus_path = Path(corpus_path or CORPUS_PATH)
    data = json.loads(corpus_path.read_text())
    messages = data["messages"]
    texts = {m["text"] for m in messages}
    ids = {m["id"] for m in messages}
    added, skipped = [], []
    for e in staged:
        if not e.get("confirmed"):
            skipped.append({"id": e.get("id"), "reason": "unconfirmed"})
            continue
        if e.get("expected_type") not in TAXONOMY:
            skipped.append({"id": e.get("id"), "reason": "bad_expected_type"})
            continue
        if e["text"] in texts:
            skipped.append({"id": e.get("id"), "reason": "duplicate_text"})
            continue
        eid = _unique_id(e.get("id") or f"pasted-{_slug(e['text'])}", ids)
        messages.append({
            "id": eid,
            "source": e.get("source", source),
            "text": e["text"],
            "expected_type": e["expected_type"],
            "meta": {k: bool(e.get("meta", {}).get(k)) for k in
                     ("is_edit", "media", "is_reply", "stale")},
            "notes": e.get("notes", ""),
        })
        ids.add(eid)
        texts.add(e["text"])
        added.append(eid)
    corpus_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {"added": added, "skipped": skipped}
