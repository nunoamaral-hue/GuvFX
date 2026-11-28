"""ADR-0102 (Lane C) — governed, hash-PINNED catalogue-delta workflow.

The testable core behind the ``catalogue_delta`` command. It wraps the existing immutable, versioned catalogue
(``CatalogueVersion``/``CatalogueArtefact``/``approvals.ArtefactApproval``) with the governance layer it lacks:

    propose → validate (dry-run) → approve → stage → activate   (or reject / rollback)

Every mutation is explicit, audited (append-only ``CatalogueDeltaAudit``) and fail-closed. APPROVAL is SEPARATE
from ACTIVATION. Activation re-checks the EXPECTED base manifest (optimistic concurrency / CAS) so a stale base
fails closed, and delegates the actual version flip to the proven ``activate_catalogue_version`` gates (approval /
sanitiser / ownership / duplicate / carried-SHA) — this module NEVER re-implements those and NEVER contacts a host
or writes a ``servers.dat`` (the host seams stay fail-closed/DARK; no ``--attest-host``).

PRIVACY: stores/returns server/broker IDENTITY, SHA-256 integrity hashes, enums and counts only — never servers.dat
bytes, never a credential/account number.
"""
from __future__ import annotations

import re
from io import StringIO
from typing import List, Optional

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from . import service as svc
from .models import CatalogueArtefact, CatalogueDelta, CatalogueDeltaAudit, CatalogueVersion

ST = CatalogueDelta.State
ACT = CatalogueDeltaAudit.Action
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# A host-relative artefact path must be exactly 'versions/<label>/<broker>/servers.dat' — this rejects '..',
# absolute paths and substitution at PROPOSE time (defence-in-depth; the host seam also confines its own path).
_RELPATH_RE = re.compile(r"^versions/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/servers\.dat$")
# Bidi-override + zero-width/BOM format characters: harmless to routing (exact-match) but can spoof a report/audit
# or a terminal rendering, so a server name may not contain them.
_BIDI_ZERO_WIDTH = frozenset({0x200b, 0x200c, 0x200d, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e,
                              0x2066, 0x2067, 0x2068, 0x2069, 0xfeff})
_MAX_SERVER_NAME = 160


def _unsafe_name_char(ch: str) -> bool:
    """A character disallowed in a server identity: any C0/C1 control, DEL, or a bidi-override/zero-width char."""
    o = ord(ch)
    return o < 32 or o == 127 or 0x80 <= o <= 0x9f or o in _BIDI_ZERO_WIDTH


# ---- helpers ----------------------------------------------------------------------------------------

def _audit(delta: CatalogueDelta, action: str, *, actor: str, reason: str = "", ticket: str = "") -> None:
    CatalogueDeltaAudit.objects.create(
        delta=delta, action=action, actor=(actor or "")[:128], reason=(reason or "")[:256],
        ticket=(ticket or "")[:64], state_after=delta.state, base_manifest_sha256=delta.base_manifest_sha256,
        delta_sha256=delta.delta_sha256,
        snapshot={"state": delta.state, "proposed": delta.proposed_version.label,
                  "base": (delta.base_version.label if delta.base_version else None)})


def _identity(a: CatalogueArtefact) -> dict:
    return {"broker_id": a.broker_id, "display_name": a.display_name,
            "servers": sorted(str(s).strip() for s in (a.servers or []) if str(s).strip()),
            "sha256": svc.norm_sha(a.sha256)}


def _artefact_map(version: Optional[CatalogueVersion]) -> dict:
    if version is None:
        return {}
    return {a.broker_id: a for a in CatalogueArtefact.objects.filter(version=version)}


def compute_diff(base_version: Optional[CatalogueVersion], proposed_version: CatalogueVersion):
    """Privacy-safe server-identity diff between two versions' artefact sets. Returns (added, removed, changed)."""
    base, prop = _artefact_map(base_version), _artefact_map(proposed_version)
    added = [_identity(prop[b]) for b in sorted(prop) if b not in base]
    removed = [_identity(base[b]) for b in sorted(base) if b not in prop]
    changed = []
    for b in sorted(prop):
        if b in base:
            ba, pa = base[b], prop[b]
            bs = sorted(str(s).strip() for s in (ba.servers or []) if str(s).strip())
            ps = sorted(str(s).strip() for s in (pa.servers or []) if str(s).strip())
            if bs != ps or svc.norm_sha(ba.sha256) != svc.norm_sha(pa.sha256):
                changed.append({"broker_id": b, "display_name": pa.display_name, "from_servers": bs,
                                "to_servers": ps, "from_sha": svc.norm_sha(ba.sha256), "to_sha": svc.norm_sha(pa.sha256)})
    return added, removed, changed


def structural_problems(proposed_version: CatalogueVersion) -> List[str]:
    """Structural validation of the proposed version's declared server IDENTITIES + artefact metadata. (The
    servers.dat BYTES are opaque MT5 format — not parsed here; their integrity is bound by SHA + the base
    subsystem's sanitiser verdict + immutability.) Reuses the base subsystem's ownership/duplicate checks.
    Returns problems (empty == structurally valid)."""
    problems: List[str] = []
    arts = list(CatalogueArtefact.objects.filter(version=proposed_version))
    if not arts:
        problems.append("proposed version has no artefacts")
    dups = svc.duplicate_server_names(proposed_version)
    if dups:
        problems.append(f"ambiguous: duplicate server names across artefacts: {dups}")
    seen = set()
    for a in arts:
        if a.broker_id in seen:
            problems.append(f"duplicate broker_id: {a.broker_id}")
        seen.add(a.broker_id)
        problems.extend(svc.server_ownership_problems(a.broker_id, a.servers))
        names = [str(s).strip() for s in (a.servers or [])]
        if not any(names):
            problems.append(f"{a.broker_id}: no server names declared")
        for nm in names:
            if not nm:
                problems.append(f"{a.broker_id}: empty server name")
            elif any(_unsafe_name_char(ch) for ch in nm):
                problems.append(f"{a.broker_id}: server name contains control / bidi / zero-width characters")
            elif len(nm) > _MAX_SERVER_NAME:
                problems.append(f"{a.broker_id}: server name exceeds {_MAX_SERVER_NAME} chars")
        if (a.sanitisation_result or "").upper() != "PASS":
            problems.append(f"{a.broker_id}: sanitisation verdict is not PASS ({a.sanitisation_result or 'MISSING'})")
        if not _SHA_RE.fullmatch(svc.norm_sha(a.sha256)):
            problems.append(f"{a.broker_id}: missing/invalid artefact sha256")
        if not _RELPATH_RE.fullmatch(a.host_relpath or ""):
            problems.append(f"{a.broker_id}: host_relpath is not a safe 'versions/<label>/<broker>/servers.dat' path")
    return problems


def _assert_proposed_unchanged(delta: CatalogueDelta) -> None:
    """Re-PIN the proposed side: the proposed version's live content must still match the manifest recorded at
    propose (the reviewer approved THAT content), and still be structurally valid. Fail closed — a DRAFT version is
    mutable, so without this an operator could add/remove a broker after approval and ship an unreviewed change set."""
    if delta.proposed_version.status != CatalogueVersion.Status.DRAFT:
        raise ValidationError(f"proposed version is no longer DRAFT ({delta.proposed_version.status})")
    live = svc.compute_manifest_sha(delta.proposed_version, algo=svc.MANIFEST_ALGO)
    if live != delta.proposed_manifest_sha256:
        raise ValidationError("stale proposal: the proposed version's content changed since this delta was proposed")
    probs = structural_problems(delta.proposed_version)
    if probs:
        raise ValidationError(f"proposed version is no longer structurally valid: {probs}")


def _active_locked() -> Optional[CatalogueVersion]:
    """The current ACTIVE version, ROW-LOCKED (select_for_update) — use inside a transaction so the active pointer
    cannot change between the CAS check and a delegated flip (closes the TOCTOU vs a concurrent direct command)."""
    return CatalogueVersion.objects.select_for_update().filter(status=CatalogueVersion.Status.ACTIVE).first()


def _assert_base_unchanged(delta: CatalogueDelta, base: Optional[CatalogueVersion]) -> None:
    """Hash-pin / CAS: the pinned base must still be the current ACTIVE version with the same manifest. Fail closed."""
    if delta.base_version_id is None:
        if base is not None:
            raise ValidationError("stale base: a version became ACTIVE since this first-ever delta was proposed")
        return
    if base is None or base.pk != delta.base_version_id:
        raise ValidationError("stale base: the pinned base version is no longer the ACTIVE version")
    if svc.norm_sha(base.manifest_sha256) != delta.base_manifest_sha256:
        raise ValidationError("stale base: the ACTIVE version's manifest changed since this delta was proposed")


# ---- inspection / reports (read-only) ---------------------------------------------------------------

def current_state() -> dict:
    active = svc.resolve_active_version()
    if active is None:
        return {"active_version": None, "manifest_sha256": "", "manifest_verified": False,
                "artefact_count": 0, "brokers": []}
    arts = list(CatalogueArtefact.objects.filter(version=active))
    return {"active_version": active.label, "manifest_sha256": active.manifest_sha256,
            "manifest_verified": svc.verify_version_integrity(active),
            "artefact_count": len(arts), "brokers": sorted(a.broker_id for a in arts)}


def delta_report(delta: CatalogueDelta) -> dict:
    return {
        "id": delta.pk, "state": delta.state, "in_flight": delta.in_flight,
        "base_version": (delta.base_version.label if delta.base_version else None),
        "base_manifest_sha256": delta.base_manifest_sha256,
        "proposed_version": delta.proposed_version.label, "proposed_manifest_sha256": delta.proposed_manifest_sha256,
        "delta_sha256": delta.delta_sha256,
        "counts": {"added": len(delta.added_identities), "removed": len(delta.removed_identities),
                   "changed": len(delta.changed_identities)},
        "added": delta.added_identities, "removed": delta.removed_identities, "changed": delta.changed_identities,
        "proposer": delta.proposer, "reviewer": delta.reviewer, "approver": delta.approver,
        "ticket": delta.ticket, "reason": delta.reason,
    }


def staging_artifact_descriptor(delta: CatalogueDelta) -> dict:
    """A DETERMINISTIC, checksummed descriptor of what would be staged for the proposed version — artefact IDENTITY
    + SHA + size + host_relpath + the version manifest. NOT the bytes; prepared so a later, separately-gated host
    STAGE op can act on exactly these. Repository-only (no host write here)."""
    import hashlib
    import json
    arts = sorted(CatalogueArtefact.objects.filter(version=delta.proposed_version),
                  key=lambda a: a.broker_id)
    entries = [{"broker_id": a.broker_id, "servers": sorted(str(s).strip() for s in (a.servers or [])),
                "sha256": svc.norm_sha(a.sha256), "size_bytes": int(a.size_bytes or 0),
                "host_relpath": a.host_relpath, "artefact_kind": a.artefact_kind} for a in arts]
    body = json.dumps({"version": delta.proposed_version.label,
                       "manifest_sha256": delta.proposed_manifest_sha256, "artefacts": entries},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {"version": delta.proposed_version.label, "manifest_sha256": delta.proposed_manifest_sha256,
            "artefacts": entries, "descriptor_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


# ---- lifecycle --------------------------------------------------------------------------------------

@transaction.atomic
def propose_delta(*, proposed_label: str, actor: str, ticket: str, reason: str = "",
                  expected_base_manifest: Optional[str] = None) -> CatalogueDelta:
    if not actor or not ticket:
        raise ValidationError("propose requires a staff actor and a ticket")
    if CatalogueDelta.objects.filter(in_flight=True).exists():
        inflight = CatalogueDelta.objects.filter(in_flight=True).first()
        raise ValidationError(f"an in-flight delta already exists (#{inflight.pk}, {inflight.state}); resolve it first")
    proposed = CatalogueVersion.objects.filter(label=proposed_label).first()
    if proposed is None:
        raise ValidationError(f"no catalogue version {proposed_label!r} (build it first via build_catalogue_version)")
    if proposed.status != CatalogueVersion.Status.DRAFT:
        raise ValidationError(f"proposed version {proposed_label} must be DRAFT (is {proposed.status})")
    base = svc.resolve_active_version()
    base_manifest = svc.norm_sha(base.manifest_sha256) if base else ""
    if expected_base_manifest is not None and svc.norm_sha(expected_base_manifest) != base_manifest:
        raise ValidationError("stale base: the supplied expected base manifest does not match the current ACTIVE version")
    if base is not None and not svc.verify_version_integrity(base):
        raise ValidationError("the current ACTIVE version fails manifest integrity; refusing to propose on it")
    if base is not None and base.pk == proposed.pk:
        raise ValidationError("the proposed version is the current ACTIVE version")
    problems = structural_problems(proposed)
    if problems:
        raise ValidationError(f"proposed version failed structural validation: {problems}")
    added, removed, changed = compute_diff(base, proposed)
    if not (added or removed or changed):
        raise ValidationError("no-op delta: the proposed version is identical to the base (nothing to propose)")
    delta = CatalogueDelta(
        base_version=base, base_manifest_sha256=base_manifest, proposed_version=proposed,
        proposed_manifest_sha256=svc.compute_manifest_sha(proposed, algo=svc.MANIFEST_ALGO),
        added_identities=added, removed_identities=removed, changed_identities=changed,
        state=ST.DRAFT, in_flight=True, proposer=(actor or "")[:128], ticket=(ticket or "")[:64],
        reason=(reason or "")[:256])
    delta.delta_sha256 = delta.compute_delta_sha256()
    delta.save()
    _audit(delta, ACT.PROPOSE, actor=actor, reason=reason, ticket=ticket)
    return delta


def validate_delta(delta: CatalogueDelta, *, actor: str, persist: bool = False) -> dict:
    """Dry-run re-validation (default: no mutation). Re-checks the hash-pin, structural validity and the delta
    checksum, and returns a redacted report with any ``problems``. With ``persist`` and no problems, marks a DRAFT
    delta REVIEWED."""
    problems: List[str] = []
    base = svc.resolve_active_version()
    try:
        _assert_base_unchanged(delta, base)
    except ValidationError as e:
        problems.append(str(e.message if hasattr(e, "message") else e))
    problems.extend(structural_problems(delta.proposed_version))
    if delta.compute_delta_sha256() != delta.delta_sha256:
        problems.append("delta checksum mismatch (the recorded diff was tampered)")
    if delta.proposed_version.status != CatalogueVersion.Status.DRAFT:
        problems.append(f"proposed version is no longer DRAFT ({delta.proposed_version.status})")
    elif svc.compute_manifest_sha(delta.proposed_version, algo=svc.MANIFEST_ALGO) != delta.proposed_manifest_sha256:
        problems.append("stale proposal: the proposed version's content changed since this delta was proposed")
    report = delta_report(delta)
    report["problems"] = problems
    report["valid"] = not problems
    if persist and not problems and delta.state == ST.DRAFT:
        with transaction.atomic():
            d = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
            if d.state != ST.DRAFT:
                raise ValidationError(f"only a DRAFT delta may be reviewed (is {d.state})")
            d.state = ST.REVIEWED
            d.reviewer = (actor or "")[:128]
            d.reviewed_at = timezone.now()
            d.save()
            _audit(d, ACT.REVIEW, actor=actor)
            report["state"] = d.state
    return report


@transaction.atomic
def approve_delta(delta: CatalogueDelta, *, actor: str, ticket: str, reason: str) -> CatalogueDelta:
    """APPROVE a REVIEWED delta (separate from activation — this grants authority, it does NOT activate)."""
    if not actor or not ticket or not reason:
        raise ValidationError("approve requires a staff actor, ticket and reason")
    delta = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
    if delta.state != ST.REVIEWED:
        raise ValidationError(f"only a REVIEWED delta may be approved (is {delta.state})")
    # Minimal separation of duty: the approver must differ from the proposer. (The actor labels are audit labels,
    # not authenticated principals — fuller SoD, e.g. reviewer≠approver≠activator, is enforced out-of-band by
    # management-command access control; see the module docstring / ADR-0102.)
    if delta.proposer and (actor or "").strip() == delta.proposer.strip():
        raise ValidationError("separation of duty: the approver must differ from the proposer")
    # Re-validate at approval so a stale/tampered/mutated proposal cannot be approved.
    problems = validate_delta(delta, actor=actor, persist=False)["problems"]
    if problems:
        raise ValidationError(f"cannot approve; validation problems: {problems}")
    delta.state = ST.APPROVED
    delta.approver = (actor or "")[:128]
    delta.approved_at = timezone.now()
    delta.ticket = (ticket or "")[:64]
    delta.reason = (reason or "")[:256]
    delta.save()
    _audit(delta, ACT.APPROVE, actor=actor, reason=reason, ticket=ticket)
    return delta


@transaction.atomic
def stage_delta(delta: CatalogueDelta, *, actor: str) -> dict:
    """STAGE an APPROVED delta: prepare the deterministic deployment-artifact descriptor and mark STAGED. No host
    write (the actual host STAGE op is a separate Sponsor-gated step). Returns the descriptor."""
    if not actor:
        raise ValidationError("stage requires a staff actor")
    delta = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
    if delta.state != ST.APPROVED:
        raise ValidationError(f"only an APPROVED delta may be staged (is {delta.state})")
    _assert_proposed_unchanged(delta)
    descriptor = staging_artifact_descriptor(delta)
    delta.state = ST.STAGED
    delta.staged_at = timezone.now()
    delta.save()
    _audit(delta, ACT.STAGE, actor=actor, reason=f"descriptor {descriptor['descriptor_sha256'][:12]}")
    return descriptor


@transaction.atomic
def activate_delta(delta: CatalogueDelta, *, actor: str) -> CatalogueDelta:
    """ACTIVATE a STAGED delta: re-check the hash-pin (CAS, fail closed on a stale base), then delegate the version
    flip to the proven ``activate_catalogue_version`` gates (no ``--attest-host`` → DARK, no host write). The whole
    operation is atomic — a gate failure rolls everything back and the delta stays STAGED."""
    if not actor:
        raise ValidationError("activate requires a staff actor")
    delta = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
    if delta.state != ST.STAGED:
        raise ValidationError(f"only a STAGED delta may be activated (is {delta.state})")
    # CAS: re-check the pinned base against the ROW-LOCKED active pointer (no TOCTOU vs a concurrent direct command).
    _assert_base_unchanged(delta, _active_locked())
    # Re-pin the PROPOSED content: an operator must not add/remove a broker after approval (ship-unreviewed guard).
    _assert_proposed_unchanged(delta)
    call_command("activate_catalogue_version", label=delta.proposed_version.label,
                 stdout=StringIO(), stderr=StringIO())
    delta.proposed_version.refresh_from_db()
    if delta.proposed_version.status != CatalogueVersion.Status.ACTIVE:
        raise ValidationError("activation did not make the proposed version ACTIVE")
    delta.state = ST.ACTIVATED
    delta.approver = delta.approver or (actor or "")[:128]
    delta.activated_at = timezone.now()
    delta.in_flight = False
    delta.save()
    _audit(delta, ACT.ACTIVATE, actor=actor)
    return delta


@transaction.atomic
def reject_delta(delta: CatalogueDelta, *, actor: str, reason: str) -> CatalogueDelta:
    """REJECT a non-terminal, pre-activation delta."""
    if not actor or not reason:
        raise ValidationError("reject requires a staff actor and reason")
    delta = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
    if delta.state not in (ST.DRAFT, ST.REVIEWED, ST.APPROVED, ST.STAGED):
        raise ValidationError(f"only a pre-activation delta may be rejected (is {delta.state})")
    delta.state = ST.REJECTED
    delta.rejected_at = timezone.now()
    delta.reason = (reason or "")[:256]
    delta.in_flight = False
    delta.save()
    _audit(delta, ACT.REJECT, actor=actor, reason=reason)
    return delta


@transaction.atomic
def rollback_delta(delta: CatalogueDelta, *, actor: str, ticket: str, reason: str) -> CatalogueDelta:
    """ROLL BACK an ACTIVATED delta: re-activate the base version (the proven ``rollback_catalogue_version`` path)
    and mark the delta ROLLED_BACK. Fail-closed: the delta must be ACTIVATED and have a base to restore."""
    if not actor or not ticket or not reason:
        raise ValidationError("rollback requires a staff actor, ticket and reason")
    delta = CatalogueDelta.objects.select_for_update().get(pk=delta.pk)
    if delta.state != ST.ACTIVATED:
        raise ValidationError(f"only an ACTIVATED delta may be rolled back (is {delta.state})")
    if delta.base_version_id is None:
        raise ValidationError("cannot roll back: this delta had no base version to restore")
    # Only roll back if THIS delta's proposed version is still the ACTIVE one — never undo a newer, separately
    # approved activation (row-locked to avoid a concurrent-flip race).
    active = _active_locked()
    if active is None or active.pk != delta.proposed_version_id:
        raise ValidationError("the delta's proposed version is no longer the ACTIVE version; nothing to roll back")
    delta.base_version.refresh_from_db()
    call_command("rollback_catalogue_version", to=delta.base_version.label, stdout=StringIO(), stderr=StringIO())
    delta.base_version.refresh_from_db()
    if delta.base_version.status != CatalogueVersion.Status.ACTIVE:
        raise ValidationError("rollback did not restore the base version to ACTIVE")
    delta.state = ST.ROLLED_BACK
    delta.rolled_back_at = timezone.now()
    delta.save()
    _audit(delta, ACT.ROLLBACK, actor=actor, reason=reason, ticket=ticket)
    return delta


def audit_history(delta: CatalogueDelta) -> List[dict]:
    return [{"action": r.action, "actor": r.actor, "reason": r.reason, "ticket": r.ticket,
             "state_after": r.state_after, "delta_sha256": r.delta_sha256[:12], "at": r.created_at.isoformat()}
            for r in CatalogueDeltaAudit.objects.filter(delta=delta).order_by("id")]
