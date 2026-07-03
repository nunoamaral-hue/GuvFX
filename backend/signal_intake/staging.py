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
_FLAGS = {"edit": "is_edit", "media": "media", "reply": "is_reply", "stale": "stale"}
# Strict, KNOWN-key-only directive lines (one per line). ANY other line — including a
# real "@mention", a bare "@foo", or two directives on one line — is treated as message
# BODY and never consumed, so real content is never silently eaten.
_TYPE_RE = re.compile(r"^@type\s*:\s*(\S.*?)\s*$", re.I)
_ID_RE = re.compile(r"^@id\s*:\s*(\S.*?)\s*$", re.I)
_FLAG_RE = re.compile(r"^@(edit|media|reply|stale)\s*$", re.I)


def _extract_directives(block):
    """Split a block's leading @directive lines from its body. Directives must be
    KNOWN keys, one per line; leading blank lines are skipped; the first line that is
    not a recognised directive begins the body (so an @mention, a bare @word, or a
    '@edit @type: X' mix stays verbatim in the body rather than being consumed)."""
    meta, declared, cid = {}, None, None
    lines = block.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue  # skip blank lines among/before leading directives
        mt, mi, mf = _TYPE_RE.match(s), _ID_RE.match(s), _FLAG_RE.match(s)
        if mt:
            declared = mt.group(1).strip().upper()
        elif mi:
            cid = mi.group(1).strip()
        elif mf:
            meta[_FLAGS[mf.group(1).lower()]] = True
        else:
            break  # first real body line — everything from here is verbatim body
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
    """Append CONFIRMED staging entries to the permanent corpus. Skips (with a reason)
    entries that are unconfirmed, missing text, a bad expected_type, or a duplicate.
    Returns {added, skipped, unsafe} — ``unsafe`` lists added entries whose certified
    type the parser does NOT currently produce (a real parser gap; the entry is still
    added because it is a real message, and certification will now fail until fixed).
    Raises ValueError on a malformed corpus (never partially writes)."""
    corpus_path = Path(corpus_path or CORPUS_PATH)
    data = json.loads(corpus_path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("corpus must be a JSON object with a 'messages' list")
    messages = data["messages"]
    texts = {m.get("text") for m in messages}
    ids = {m.get("id") for m in messages}
    added, skipped, unsafe = [], [], []
    for e in staged:
        text = e.get("text")
        if not text or not str(text).strip():
            skipped.append({"id": e.get("id"), "reason": "missing_text"})
            continue
        if not e.get("confirmed"):
            skipped.append({"id": e.get("id"), "reason": "unconfirmed"})
            continue
        expected = e.get("expected_type")
        if expected not in TAXONOMY:
            skipped.append({"id": e.get("id"), "reason": "bad_expected_type"})
            continue
        if text in texts:
            skipped.append({"id": e.get("id"), "reason": "duplicate_text"})
            continue
        meta = e.get("meta") or {}
        eid = _unique_id(e.get("id") or f"pasted-{_slug(text)}", ids)
        messages.append({
            "id": eid,
            "source": e.get("source", source),
            "text": text,
            "expected_type": expected,
            "meta": {k: bool(meta.get(k)) for k in ("is_edit", "media", "is_reply", "stale")},
            "notes": e.get("notes", ""),
        })
        ids.add(eid)
        texts.add(text)
        added.append(eid)
        # Recompute the real verdict; surface (but still add) a parser gap.
        observed = classify(text, is_edit=meta.get("is_edit", False),
                            media=meta.get("media", False), stale=meta.get("stale", False))
        if _verdict(expected, observed)[1] == "UNSAFE":
            unsafe.append({"id": eid, "expected": expected, "observed": observed})
    corpus_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {"added": added, "skipped": skipped, "unsafe": unsafe}
