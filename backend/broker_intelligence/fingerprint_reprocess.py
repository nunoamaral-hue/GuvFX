"""ADR-0099 (Lane A) — bounded, owner-scoped, version-pinned reprocessing of ALREADY-RETAINED evidence.

When a broker is newly certified, its RETAINED Tier-1 evidence (``EvidenceBlob`` — raw bytes already stored under
the existing retention gate) can be re-run through the detector so a withdrawal that was previously REVIEW-only is
recognised. This path is deliberately narrow and fail-closed:

  * ONLY evidence already retained is reprocessed — it NEVER reconstructs a historical raw email, NEVER broadens
    Gmail access, and NEVER moves a mailbox cursor (there is no mailbox call here at all).
  * OWNER-SCOPED and bounded (a caller-supplied limit); VERSION-PINNED to one certified fingerprint.
  * DRY-RUN first (``plan_reprocess`` mutates nothing); apply is IDEMPOTENT and DUPLICATE-SAFE — an item that
    already produced an owner-scoped event for the same evidence is skipped, so an existing withdrawal is never
    duplicated.
  * A CONFLICT (an existing event for the same evidence that disagrees with the reprocessed classification) STOPS
    that item for review — it is never silently overwritten.
  * Evidence that no longer resolves to a withdrawal under the pinned fingerprint stays N/A (unresolved).

The plan is REDACTED: per-item structural outcomes only (12-char evidence hash, enums, action) — never raw content.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from django.core.exceptions import ValidationError

from . import generic_detector as gdx

# ---- reprocessing actions (structural; never raw text) ----------------------------------------------
A_SKIP_IDEMPOTENT = "SKIP_ALREADY_EVENTED"      # an owner-scoped event already exists for this evidence
A_WOULD_CREATE = "WOULD_CREATE_EVENT"           # certified recognition; no prior event => a new event would be created
A_CONFLICT = "CONFLICT_STOP_FOR_REVIEW"         # prior event disagrees with the reprocessed classification
A_REVIEW_ONLY = "REVIEW_ONLY"                   # credible but not auto-acceptable under the pinned fingerprint
A_QUARANTINE = "QUARANTINE"                     # auth/ambiguity/contradiction/unsafe
A_NOT_WITHDRAWAL = "NOT_A_WITHDRAWAL"           # stays N/A (unresolved) — no reconstruction, no guess


def _pinned_fingerprint(fingerprints: Sequence, name: str, version: int):
    for fp in fingerprints:
        fn = fp.get("name") if isinstance(fp, dict) else getattr(fp, "name", "")
        fv = fp.get("version") if isinstance(fp, dict) else getattr(fp, "version", 0)
        if fn == name and int(fv or 0) == int(version):
            return fp
    return None


def classify_item(gd, existing_event) -> str:
    """Decide the reprocessing action for ONE evidence item from its detection + any existing owner-scoped event."""
    if existing_event is not None:
        prior_cat = getattr(existing_event, "transaction_category", "")
        prior_evt = getattr(existing_event, "event_type", "")
        if gd.disposition == gdx.GENERIC_HIGH_CONFIDENCE and gd.parsed_event is not None:
            new_cat = gd.parsed_event.transaction_category
            new_evt = gd.parsed_event.event_type
            if (new_cat, new_evt) != (prior_cat, prior_evt):
                return A_CONFLICT
        return A_SKIP_IDEMPOTENT
    if gd.disposition == gdx.GENERIC_HIGH_CONFIDENCE and gd.parsed_event is not None:
        return A_WOULD_CREATE
    if gd.disposition == gdx.REVIEW_REQUIRED:
        return A_REVIEW_ONLY
    if gd.disposition == gdx.QUARANTINED:
        return A_QUARANTINE
    return A_NOT_WITHDRAWAL


def plan_reprocess(*, owner_id: int, items: List[dict], fingerprints: Sequence, fingerprint_name: str,
                   fingerprint_version: int, limit: int = 200) -> dict:
    """DRY-RUN plan (mutates nothing). ``items`` are already-retained evidence, each a dict with: subject, body,
    from_domain, auth_verdict, html (optional), blob_sha256, existing_event (object|None). VERSION-PINNED and bounded
    by ``limit``. Returns a redacted plan. Raises if the pinned fingerprint is not among the active+certified set."""
    if not owner_id:
        raise ValidationError("reprocess is owner-scoped; an owner is required")
    pinned = _pinned_fingerprint(fingerprints, fingerprint_name, fingerprint_version)
    if pinned is None or not gdx._fp_eligible(pinned):
        raise ValidationError("pinned fingerprint is not active+certified")
    bounded = list(items)[: max(0, int(limit))]
    results = []
    for it in bounded:
        gd = gdx.detect(
            subject=str(it.get("subject", "")), body=str(it.get("body", "")),
            from_domain=str(it.get("from_domain", "")), auth_verdict=str(it.get("auth_verdict", "pass")),
            sender_allowlisted=bool(it.get("allowlisted", False)), html_text=str(it.get("html", "")),
            fingerprints=[pinned])
        action = classify_item(gd, it.get("existing_event"))
        results.append({
            "evidence": str(it.get("blob_sha256", ""))[:12], "action": action,
            "disposition": gd.disposition, "proposed_state": gd.proposed_state,
            "transaction_category": (gd.parsed_event.transaction_category if gd.parsed_event is not None else ""),
            "certified_match": gdx.RC_CERTIFIED_FINGERPRINT in gd.reason_codes,
        })
    counts: dict = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {
        "owner_id": owner_id, "fingerprint": f"{fingerprint_name}:{fingerprint_version}",
        "considered": len(bounded), "bounded_limit": limit, "truncated": len(items) > len(bounded),
        "counts": counts, "results": results,
    }
