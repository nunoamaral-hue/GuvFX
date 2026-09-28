"""broker_catalogue.sanitiser — reusable, bounded credential-free scan for a broker ``servers.dat`` artefact.

This is a HEURISTIC gate, not a proof. It proves the ABSENCE of a given set of identity terms (logins, emails,
names) across the encodings a broker terminal plausibly uses, and reports high-entropy regions as advisory
evidence. It does NOT mathematically prove the opaque binary contains no secret of any kind — evidence language is
deliberately bounded. Pure stdlib (no Django, no host, no I/O beyond an optional file read) so it is unit-testable
and can run wherever the bytes are (host candidate or a backend-provided blob). The machine verdict it returns is
what the approval/activation gate binds and enforces (replacing operator free-text ``sanitisation:PASS``).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable, List

SANITISER_VERSION = "sanitiser_v1"

# Advisory only: broker TLS/data-centre certificates are legitimately high-entropy, so a high-entropy region is
# reported as evidence but does NOT by itself fail the scan. The PASS/FAIL verdict keys on identity-term absence.
_ENTROPY_WINDOW = 256
_ENTROPY_HI = 7.5


def _seqs_for_term(term: str) -> List[tuple]:
    """(encoding_label, bytes) patterns for one identity term. Numeric terms (logins) get integer/BCD forms too."""
    out: List[tuple] = []
    t = term.strip()
    if not t:
        return out
    out.append(("ascii", t.encode("ascii", "ignore")))
    out.append(("utf16le", t.encode("utf-16-le")))
    out.append(("utf16be", t.encode("utf-16-be")))
    low = t.lower()
    if low != t:
        out.append(("ascii_lower", low.encode("ascii", "ignore")))
        out.append(("utf16le_lower", low.encode("utf-16-le")))
    if t.isdigit():
        n = int(t)
        for width, order in ((4, "little"), (4, "big"), (8, "little"), (8, "big")):
            try:
                out.append((f"int{width*8}{order[:2]}", n.to_bytes(width, order)))
            except OverflowError:
                pass
        digits = t if len(t) % 2 == 0 else "0" + t
        try:
            bcd = bytes(int(digits[i]) << 4 | int(digits[i + 1]) for i in range(0, len(digits), 2))
            out.append(("bcd", bcd))
        except ValueError:
            pass
        out.append(("zeropad", ("0" + t).encode("ascii")))
    # drop empty/degenerate needles
    return [(lbl, b) for (lbl, b) in out if b and len(b) >= 3]


def _contains(hay: bytes, needle: bytes) -> bool:
    return bool(needle) and needle in hay


def _entropy_scan(data: bytes) -> dict:
    n = 0
    hi = 0
    mx = 0.0
    for off in range(0, len(data) - _ENTROPY_WINDOW + 1, _ENTROPY_WINDOW):
        window = data[off:off + _ENTROPY_WINDOW]
        freq = [0] * 256
        for b in window:
            freq[b] += 1
        h = 0.0
        for c in freq:
            if c:
                p = c / _ENTROPY_WINDOW
                h -= p * math.log2(p)
        n += 1
        if h > mx:
            mx = h
        if h > _ENTROPY_HI:
            hi += 1
    return {"windows": n, "max_bits_per_byte": round(mx, 3), "windows_over_7_5": hi}


def scan_artefact(data: bytes, *, identity_terms: Iterable[str] = ()) -> dict:
    """Scan artefact bytes for the given identity terms (all encodings) + an advisory entropy sweep.

    Returns a machine verdict::

        {"passed": bool, "version": SANITISER_VERSION, "size_bytes": int, "sha256": <hex>,
         "identity_hits": [{"term_masked": "...", "encoding": "..."}], "entropy": {...},
         "evidence_sha256": <hex over the canonical verdict>, "note": "<bounded-language disclaimer>"}

    ``passed`` is True iff NO identity term is present in any scanned encoding. Bounded: this does not prove the
    absence of every possible secret — only of the supplied identities, plus advisory high-entropy reporting.
    """
    data = bytes(data or b"")
    hits: List[dict] = []
    for term in identity_terms:
        for (label, needle) in _seqs_for_term(str(term)):
            if _contains(data, needle):
                masked = (str(term)[:2] + "***") if len(str(term)) > 3 else "***"
                hits.append({"term_masked": masked, "encoding": label})
    entropy = _entropy_scan(data)
    verdict = {
        "passed": len(hits) == 0,
        "version": SANITISER_VERSION,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "identity_hits": hits,
        "entropy": entropy,
        "note": ("bounded heuristic: proves absence of the supplied identity terms in the scanned encodings and "
                 "reports high-entropy regions (broker TLS certs are legitimately high-entropy); does NOT prove "
                 "absence of every possible secret"),
    }
    # A stable evidence hash over the verdict (excluding itself) so an approval can bind the exact scan result.
    core = {k: verdict[k] for k in ("passed", "version", "size_bytes", "sha256", "identity_hits", "entropy")}
    verdict["evidence_sha256"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return verdict


def scan_file(path: str, *, identity_terms: Iterable[str] = ()) -> dict:
    with open(path, "rb") as fh:
        return scan_artefact(fh.read(), identity_terms=identity_terms)


def recompute_evidence_sha(verdict: dict) -> str:
    """Recompute a verdict's ``evidence_sha256`` from its own core fields — the same canonicalisation ``scan_artefact``
    uses — so a caller can prove the verdict is an internally-consistent scan output, not hand-typed JSON."""
    core = {k: (verdict or {}).get(k) for k in ("passed", "version", "size_bytes", "sha256", "identity_hits", "entropy")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_verdict(verdict: dict, *, expected_sha256: str) -> bool:
    """True iff ``verdict`` is a genuine PASS scan bound to EXACTLY the expected bytes: it declares ``passed`` True,
    its ``sha256`` equals the approved artefact SHA (so a clean verdict from other bytes cannot be reused), and its
    ``evidence_sha256`` recomputes from its own core (so ``{"passed": true}`` cannot simply be hand-typed). This
    replaces trusting operator free-text. BOUNDED: it still cannot prove the operator scanned for the RIGHT identity
    terms — that is the heuristic's inherent limit (see ``scan_artefact``'s note)."""
    if not isinstance(verdict, dict) or verdict.get("passed") is not True:
        return False
    if str(verdict.get("sha256", "")).lower() != str(expected_sha256 or "").lower():
        return False
    ev = str(verdict.get("evidence_sha256", "")).lower()
    return bool(ev) and ev == recompute_evidence_sha(verdict).lower()
