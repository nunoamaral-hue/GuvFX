"""ADR-0099 (Lane A) — governed broker-fingerprint onboarding service.

The TESTABLE core behind the ``broker_fingerprint`` staff command. Every mutation is explicit, audited and
fail-closed; NO step silently certifies a discovered broker. The lifecycle is:

    propose → validate (dry-run) → certify (capability, version-bound) → activate → [deactivate | rollback]

CERTIFICATION is the safety boundary. ``certify_fingerprint`` refuses unless: a staff actor + reason/ticket are
supplied; a broker_profile relationship exists; the fingerprint's authenticated sender domains are a subset of
that profile's operator-approved domains; no OTHER active+certified fingerprint claims an overlapping domain
(conflict scan, fail-closed); representative SYNTHETIC fixtures are present; and the fixtures pass the dry-run
regression (every positive example is recognised, every negative example is NOT auto-accepted). Activation is a
SEPARATE step — a merely-certified version grants no authority until it is activated, and only ONE active+certified
version may exist per broker (DB-enforced).

PRIVACY: dry-run / delta reports carry ONLY structural booleans/enums/counts — never a raw subject/body, name,
full account number, email address, URL or token.
"""
from __future__ import annotations

from typing import List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import generic_detector as gdx
from .models import BrokerFingerprint, BrokerFingerprintAudit, BrokerProfile, Provenance
from .redaction import contains_sensitive

GS = BrokerFingerprint.GovernanceStatus
_ACT = BrokerFingerprintAudit.Action


# ---- helpers ----------------------------------------------------------------------------------------

def _audit(fp: BrokerFingerprint, action: str, *, actor: str, reason: str = "", ticket: str = "") -> None:
    BrokerFingerprintAudit.objects.create(
        fingerprint=fp, action=action, actor=(actor or "")[:128], reason=(reason or "")[:256],
        ticket=(ticket or "")[:64], source_hash=fp.source_hash,
        snapshot={"governance_status": fp.governance_status, "is_active": fp.is_active,
                  "certified_capability": fp.certified_capability, "version": fp.version})


def _next_version(name: str) -> int:
    latest = (BrokerFingerprint.objects.filter(name=name).order_by("-version")
              .values_list("version", flat=True).first())
    return (latest or 0) + 1


def _covered(domain: str, approved: List[str]) -> bool:
    d = (domain or "").strip().lower()
    return bool(d) and any(d == a or d.endswith("." + a) for a in approved)


def _fp_as_certified_dict(fp: BrokerFingerprint) -> dict:
    """The fingerprint in the detector's dict form, forced ELIGIBLE (``cert=True``) so a dry-run can exercise the
    certified path BEFORE the row is actually certified. Used only for in-memory dry-run; never persisted."""
    return {
        "name": fp.name, "version": fp.version, "broker_display_name": fp.broker_display_name, "cert": True,
        "authenticated_sender_domains": list(fp.authenticated_sender_domains or []),
        "envelope_from_domains": list(fp.envelope_from_domains or []),
        "reply_to_domains": list(fp.reply_to_domains or []),
        "dkim_signing_domains": list(fp.dkim_signing_domains or []),
        "subject_indicators": list(fp.subject_indicators or []),
        "body_indicators": list(fp.body_indicators or []),
        "lifecycle_phrases": dict(fp.lifecycle_phrases or {}),
        "currency_hint": fp.currency_hint, "reference_hint": fp.reference_hint,
        "semantic_overrides": dict(fp.semantic_overrides or {}),
    }


def _run_example(fp_dict: dict, example: dict) -> dict:
    """Run the pure detector over ONE synthetic example and return a REDACTED structural outcome (never content)."""
    gd = gdx.detect(
        subject=str(example.get("subject", "")), body=str(example.get("body", "")),
        from_domain=str(example.get("from_domain", "") or (fp_dict.get("authenticated_sender_domains") or [""])[0]),
        auth_verdict=str(example.get("auth_verdict", "pass")), sender_allowlisted=bool(example.get("allowlisted", False)),
        html_text=str(example.get("html", "")), fingerprints=[fp_dict],
        dkim_domains=example.get("dkim_domains", ()), envelope_from_domain=str(example.get("envelope_from_domain", "")),
        reply_to_domain=str(example.get("reply_to_domain", "")))
    category = gd.parsed_event.transaction_category if gd.parsed_event is not None else ""
    return {
        "disposition": gd.disposition, "proposed_state": gd.proposed_state,
        "transaction_category": category, "certified_match": gdx.RC_CERTIFIED_FINGERPRINT in gd.reason_codes,
        "amount_present": gd.amount is not None, "currency_present": bool(gd.currency),
        "reference_present": bool(gd.broker_reference_id), "suffix_present": bool(gd.masked_account_suffix),
        "ambiguous": gdx.RC_MULTIPLE_FINGERPRINTS in gd.reason_codes,
        "expect": example.get("expect", ""),
    }


def _domain_conflicts(name: str, domains: List[str]) -> List[str]:
    """Other CERTIFIED fingerprints (different broker name) whose authenticated domains OVERLAP ``domains``. Scans
    ALL certified peers regardless of ``is_active`` — an inactive-but-certified peer could be activated later, so
    certifying two different brokers on one domain (then activating both) must be blocked at certify-time; activation
    re-checks too. An overlap would let two certified brokers claim one message, so we fail closed on it."""
    doms = BrokerFingerprint._norm_domains(domains)
    conflicts: List[str] = []
    for other in BrokerFingerprint.objects.filter(certified_capability=True).exclude(name=name):
        od = other.domains()
        if any(d == o or d.endswith("." + o) or o.endswith("." + d) for d in doms for o in od):
            conflicts.append(f"{other.name}:{other.version}")
    return conflicts


# ---- lifecycle operations ---------------------------------------------------------------------------

@transaction.atomic
def propose_fingerprint(*, name: str, broker_display_name: str, actor: str, broker_profile: Optional[BrokerProfile] = None,
                        reason: str = "", ticket: str = "", provenance: str = Provenance.SYNTHETIC,
                        **config) -> BrokerFingerprint:
    """Create a new PROPOSED fingerprint at the next version for ``name`` (never mutates an existing version).
    ``config`` holds the matching fields (domains, indicators, lifecycle_phrases, hints, semantic_overrides, …)."""
    if not name or not broker_display_name:
        raise ValidationError("propose requires name and broker_display_name")
    if not actor:
        raise ValidationError("propose requires a staff actor")
    allowed = set(BrokerFingerprint.MATCHING_FIELDS) | {"positive_examples", "negative_examples"}
    bad = set(config) - allowed - {"name", "version", "broker_display_name"}
    if bad:
        raise ValidationError(f"unknown fingerprint field(s): {sorted(bad)}")
    fp = BrokerFingerprint(
        name=name, version=_next_version(name), broker_display_name=broker_display_name,
        broker_profile=broker_profile, provenance=provenance, governance_status=GS.PROPOSED,
        is_active=False, certified_capability=False, created_by=(actor or "")[:128],
        reason=(reason or "")[:256], ticket=(ticket or "")[:64])
    for f, v in config.items():
        if f not in ("name", "version", "broker_display_name"):
            setattr(fp, f, v)
    fp.save()
    _audit(fp, _ACT.PROPOSE, actor=actor, reason=reason, ticket=ticket)
    return fp


def validate_fingerprint(fp: BrokerFingerprint, *, actor: str, examples: Optional[List[dict]] = None,
                         persist: bool = True) -> dict:
    """Dry-run the fingerprint over SYNTHETIC examples (defaults to its own positive+negative examples) and return a
    REDACTED report. Marks the fingerprint VALIDATED (when ``persist``). NEVER certifies and NEVER mutates matching
    fields."""
    pos = list(fp.positive_examples or [])
    neg = [{**e, "expect": e.get("expect", "negative")} for e in (fp.negative_examples or [])]
    exset = examples if examples is not None else ([{**e, "expect": e.get("expect", "positive")} for e in pos] + neg)
    fp_dict = _fp_as_certified_dict(fp)
    results = [_run_example(fp_dict, e) for e in exset]
    would_match = sum(1 for r in results if r["certified_match"])
    quarantined = sum(1 for r in results if r["disposition"] == gdx.QUARANTINED)
    review_only = sum(1 for r in results if r["disposition"] == gdx.REVIEW_REQUIRED)
    ambiguous = sum(1 for r in results if r["ambiguous"])
    conflicts = _domain_conflicts(fp.name, list(fp.authenticated_sender_domains or []))
    report = {
        "name": fp.name, "version": fp.version, "examples": len(exset), "would_match": would_match,
        "review_only": review_only, "quarantined": quarantined, "ambiguous": ambiguous,
        "domain_conflicts": conflicts, "source_hash": fp.source_hash, "results": results,
    }
    if persist and fp.governance_status == GS.PROPOSED:
        fp.governance_status = GS.VALIDATED
        fp.save()
        _audit(fp, _ACT.VALIDATE, actor=actor, reason=f"dry-run {would_match}/{len(exset)} match")
    return report


@transaction.atomic
def certify_fingerprint(fp: BrokerFingerprint, *, actor: str, reason: str, ticket: str,
                        broker_profile: Optional[BrokerProfile] = None) -> BrokerFingerprint:
    """CERTIFY a validated fingerprint (grant version-bound certified capability). Fail-closed on every precondition.
    Does NOT activate it. Never silently certifies — each precondition must hold."""
    if not actor:
        raise ValidationError("certify requires a staff actor")
    if not reason or not ticket:
        raise ValidationError("certify requires an explicit reason and ticket")
    fp = BrokerFingerprint.objects.select_for_update().get(pk=fp.pk)
    if fp.certified_capability:
        raise ValidationError("fingerprint is already certified (certification is version-bound; create a new version)")
    if fp.governance_status not in (GS.VALIDATED, GS.PROPOSED):
        raise ValidationError(f"only a VALIDATED/PROPOSED fingerprint may be certified (is {fp.governance_status})")
    profile = broker_profile or fp.broker_profile
    if profile is None:
        raise ValidationError("certify requires a broker_profile relationship")
    if not getattr(profile, "is_active", False):
        raise ValidationError("certify requires an ACTIVE broker_profile")
    doms = fp.domains()
    if not doms:
        raise ValidationError("certify requires at least one authenticated sender domain")
    approved = profile.approved_domains()
    uncovered = [d for d in doms if not _covered(d, approved)]
    if uncovered:
        raise ValidationError(f"authenticated domains not covered by the broker profile's approved senders: {uncovered}")
    conflicts = _domain_conflicts(fp.name, doms)
    if conflicts:
        raise ValidationError(f"domain overlaps an existing certified fingerprint (fail-closed): {conflicts}")
    # Specificity: a certified fingerprint must declare at least one subject/body indicator — a domain-ONLY
    # fingerprint would claim every withdrawal-lifecycle email from the domain and, with a generic lifecycle phrase,
    # risk mis-counting a non-withdrawal (financial-state truthfulness). Require a representative negative fixture too.
    if not (list(fp.subject_indicators or []) + list(fp.body_indicators or [])):
        raise ValidationError("certify requires at least one subject/body indicator (no domain-only fingerprint)")
    if not (fp.positive_examples or []):
        raise ValidationError("certify requires representative SYNTHETIC positive fixtures")
    if not (fp.negative_examples or []):
        raise ValidationError("certify requires at least one representative SYNTHETIC negative fixture")
    # Privacy: operator-authored config must not carry authorization material (URL/token).
    for fld in [fp.broker_display_name, fp.reference_hint, fp.amount_hint, fp.currency_hint] + doms:
        if contains_sensitive(str(fld)):
            raise ValidationError("certify refused: operator config carries a URL/token (redact it)")
    # Dry-run REGRESSION gate: every positive example must be recognised (certified match) and no negative example
    # may auto-accept (certified match). A fixture failure blocks certification.
    fp_dict = _fp_as_certified_dict(fp)
    for i, ex in enumerate(fp.positive_examples or []):
        if not _run_example(fp_dict, ex)["certified_match"]:
            raise ValidationError(f"positive fixture #{i} is NOT recognised by the fingerprint; certification blocked")
    for i, ex in enumerate(fp.negative_examples or []):
        if _run_example(fp_dict, ex)["certified_match"]:
            raise ValidationError(f"negative fixture #{i} is auto-accepted by the fingerprint; certification blocked")
    fp.broker_profile = profile
    fp.certified_capability = True
    fp.governance_status = GS.CERTIFIED
    fp.approved_by = (actor or "")[:128]
    fp.approved_at = timezone.now()
    fp.reason = (reason or "")[:256]
    fp.ticket = (ticket or "")[:64]
    fp.save()
    _audit(fp, _ACT.CERTIFY, actor=actor, reason=reason, ticket=ticket)
    return fp


@transaction.atomic
def activate_fingerprint(fp: BrokerFingerprint, *, actor: str, reason: str = "") -> BrokerFingerprint:
    """Activate a CERTIFIED fingerprint (make it live for the detector). Only ONE active+certified version per broker
    (DB-enforced) — deactivate the prior active version first (or use ``rollback``)."""
    if not actor:
        raise ValidationError("activate requires a staff actor")
    fp = BrokerFingerprint.objects.select_for_update().get(pk=fp.pk)
    if not fp.certified_capability or fp.governance_status != GS.CERTIFIED:
        raise ValidationError("only a CERTIFIED fingerprint may be activated")
    if fp.is_active:
        return fp
    # Re-check domain conflicts at activation (not just at certify): two different brokers can be certified on an
    # overlapping domain while both inactive, so activation must fail closed if ANOTHER certified broker already
    # claims an overlapping domain (whether or not it is currently active). The per-name DB constraint cannot catch
    # a cross-name overlap.
    conflicts = _domain_conflicts(fp.name, fp.domains())
    active_conflicts = [c for c in conflicts
                        if BrokerFingerprint.objects.filter(
                            name=c.split(":")[0], is_active=True, certified_capability=True).exists()]
    if active_conflicts:
        raise ValidationError(f"cannot activate: an active certified fingerprint already claims an overlapping "
                              f"domain (fail-closed): {active_conflicts}")
    fp.is_active = True
    fp.save()
    _audit(fp, _ACT.ACTIVATE, actor=actor, reason=reason)
    return fp


@transaction.atomic
def deactivate_fingerprint(fp: BrokerFingerprint, *, actor: str, reason: str = "") -> BrokerFingerprint:
    """Deactivate a fingerprint (retire it from the detector). Certified capability is retained (version-bound); the
    status becomes DEACTIVATED so the detector no longer treats it as live."""
    if not actor:
        raise ValidationError("deactivate requires a staff actor")
    fp = BrokerFingerprint.objects.select_for_update().get(pk=fp.pk)
    if not fp.is_active and fp.governance_status == GS.DEACTIVATED:
        return fp
    fp.is_active = False
    if fp.certified_capability:
        fp.governance_status = GS.DEACTIVATED
    fp.deactivated_by = (actor or "")[:128]
    fp.deactivated_at = timezone.now()
    fp.save()
    _audit(fp, _ACT.DEACTIVATE, actor=actor, reason=reason)
    return fp


@transaction.atomic
def rollback_to_version(name: str, target_version: int, *, actor: str, reason: str, ticket: str) -> BrokerFingerprint:
    """Roll a broker back to a prior CERTIFIED version: deactivate the current active version (status ROLLED_BACK) and
    re-activate ``target_version`` (which must already be certified). Append-only audit on both."""
    if not actor or not reason or not ticket:
        raise ValidationError("rollback requires a staff actor, reason and ticket")
    target = BrokerFingerprint.objects.select_for_update().filter(name=name, version=target_version).first()
    if target is None:
        raise ValidationError(f"no fingerprint {name} v{target_version}")
    if not target.certified_capability:
        raise ValidationError("rollback target must be a certified version")
    current = (BrokerFingerprint.objects.select_for_update()
               .filter(name=name, is_active=True, certified_capability=True).exclude(pk=target.pk).first())
    if current is not None:
        current.is_active = False
        current.governance_status = GS.ROLLED_BACK
        current.deactivated_by = (actor or "")[:128]
        current.deactivated_at = timezone.now()
        current.save()
        _audit(current, _ACT.ROLLBACK, actor=actor, reason=f"rolled back to v{target_version}: {reason}", ticket=ticket)
    target.governance_status = GS.CERTIFIED
    target.is_active = True
    target.save()
    _audit(target, _ACT.ROLLBACK, actor=actor, reason=f"restored as active: {reason}", ticket=ticket)
    return target


def reject_fingerprint(fp: BrokerFingerprint, *, actor: str, reason: str) -> BrokerFingerprint:
    if not actor or not reason:
        raise ValidationError("reject requires a staff actor and reason")
    if fp.certified_capability:
        raise ValidationError("a certified version cannot be rejected; deactivate/rollback instead")
    fp.governance_status = GS.REJECTED
    fp.is_active = False
    fp.save()
    _audit(fp, _ACT.REJECT, actor=actor, reason=reason)
    return fp


def delta_report(name: str, *, examples: List[dict]) -> dict:
    """Privacy-safe delta: how the current active+certified version of ``name`` classifies a set of SYNTHETIC
    examples (structural outcomes only). Used to preview the effect of a candidate before certifying/activating it."""
    active = BrokerFingerprint.objects.filter(name=name, is_active=True, certified_capability=True).first()
    if active is None:
        return {"name": name, "active_version": None, "results": []}
    fp_dict = _fp_as_certified_dict(active)
    return {"name": name, "active_version": active.version, "source_hash": active.source_hash,
            "results": [_run_example(fp_dict, e) for e in examples]}
