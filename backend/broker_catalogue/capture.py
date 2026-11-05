"""broker_catalogue.capture — governed NEEDS_CAPTURE candidate workflow (Stream A / ADR-0078).

Scaffolding ONLY. A broker with no captured ``servers.dat`` (e.g. Dominion Markets) enters the catalogue
pipeline through a governed NEEDS_CAPTURE placeholder. This module can ONLY create/refresh such a placeholder
and can NEVER create an artefact, an approval, a SHA, a catalogue version, or any host/production state. Writing
is DARK (gated by ``capture_workflow_enabled``); by default the register path is a dry-run that reports what it
WOULD record and writes nothing. Promotion out of NEEDS_CAPTURE (to CAPTURED) is a separate, authorised capture
step that this module deliberately does not perform (the STOP boundary).
"""
from __future__ import annotations

from .flags import capture_workflow_enabled
from .models import CatalogueCaptureCandidate


def register_needs_capture(*, broker_id: str, display_name: str, server_identity_hint: str = "",
                           reason: str = "", requested_by=None, commit: bool = False) -> dict:
    """Create or refresh a NEEDS_CAPTURE candidate. Returns a result dict.

    - Writes to the DB ONLY when ``commit=True`` AND ``capture_workflow_enabled()`` is armed; otherwise it is a
      pure dry-run (``written=False``) that reports the intended placeholder and touches nothing.
    - On an existing candidate, only the descriptive fields are refreshed; ``capture_status`` is NEVER regressed
      (a candidate already advanced to CAPTURED/SUPERSEDED by a separate authorised step is left untouched).
    - It never creates an artefact/approval/SHA/version — it is a governance placeholder only.
    """
    broker_id = str(broker_id or "").strip()
    display_name = str(display_name or "").strip() or broker_id
    result = {
        "broker_id": broker_id, "display_name": display_name,
        "server_identity_hint": str(server_identity_hint or "").strip(),
        "capture_status": CatalogueCaptureCandidate.Status.NEEDS_CAPTURE,
        "flag_armed": capture_workflow_enabled(), "commit_requested": bool(commit),
        "would_write": False, "written": False, "created": False,
        "note": "scaffolding only — never creates an artefact/approval/SHA/version",
    }
    if not broker_id:
        raise ValueError("broker_id is required")
    if not (commit and capture_workflow_enabled()):
        # Dry-run: report intent, write nothing. (If commit was requested but the flag is OFF, say so.)
        result["would_write"] = True
        return result

    obj = CatalogueCaptureCandidate.objects.filter(broker_id=broker_id).first()
    if obj is None:
        obj = CatalogueCaptureCandidate.objects.create(
            broker_id=broker_id, display_name=display_name,
            server_identity_hint=result["server_identity_hint"], reason=str(reason or "")[:200],
            requested_by=requested_by)
        result["created"] = True
    else:
        # Refresh descriptive fields only; never regress capture_status.
        obj.display_name = display_name
        obj.server_identity_hint = result["server_identity_hint"]
        obj.reason = str(reason or "")[:200]
        if requested_by is not None:
            obj.requested_by = requested_by
        obj.save(update_fields=["display_name", "server_identity_hint", "reason", "requested_by", "updated_at"])
    result["written"] = True
    result["capture_status"] = obj.capture_status
    return result
