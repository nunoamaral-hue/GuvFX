"""broker_catalogue.readiness — READ-ONLY catalogue-readiness evaluator (Stream A / ADR-0078).

Given a set of brokers, report — without mutating anything — whether each has an APPROVED, sanitiser-PASSed
immutable ``servers.dat`` artefact that is eligible to enter a FUTURE additive catalogue build, with a stable
per-broker reason code. This is a planning/report function: it performs ONLY SELECTs, never builds, activates,
stages, approves, captures or writes. It intentionally reads the stored ``ArtefactApproval``/``CatalogueArtefact``
state DIRECTLY (not via the DARK ``is_artefact_approved`` consumption gate) because it reports the *governance*
state, not what provisioning is currently allowed to consume.

Reason codes are a stable contract (consumed by the report command, tests, and future build planning):

* ``ELIGIBLE``                  — approved + sanitiser PASS + not already active → ready for an additive build.
* ``ALREADY_ACTIVE``            — the approved artefact is already in the ACTIVE catalogue version.
* ``SANITISER_NOT_PASS``        — an approved artefact exists but its sanitisation verdict is not PASS.
* ``NO_APPROVED_ARTEFACT``      — an artefact/approval exists for the broker but none is APPROVED.
* ``NEEDS_CAPTURE``             — a governed NEEDS_CAPTURE candidate exists; no servers.dat captured yet.
* ``NOT_CAPTURED``              — the broker is unknown to the catalogue (no artefact, approval or candidate).
* ``DUPLICATE_SERVER_CONFLICT`` — the broker's server names collide with another eligible/active artefact.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from approvals.models import ArtefactApproval

from .models import CatalogueArtefact, CatalogueCaptureCandidate, CatalogueVersion

ARTEFACT_KIND = "broker_servers_dat"

RC_ELIGIBLE = "ELIGIBLE"
RC_ALREADY_ACTIVE = "ALREADY_ACTIVE"
RC_SANITISER_NOT_PASS = "SANITISER_NOT_PASS"
RC_NO_APPROVED_ARTEFACT = "NO_APPROVED_ARTEFACT"
RC_NEEDS_CAPTURE = "NEEDS_CAPTURE"
RC_NOT_CAPTURED = "NOT_CAPTURED"
RC_DUPLICATE_SERVER_CONFLICT = "DUPLICATE_SERVER_CONFLICT"

ALL_REASON_CODES = (
    RC_ELIGIBLE, RC_ALREADY_ACTIVE, RC_SANITISER_NOT_PASS, RC_NO_APPROVED_ARTEFACT,
    RC_NEEDS_CAPTURE, RC_NOT_CAPTURED, RC_DUPLICATE_SERVER_CONFLICT,
)


@dataclass(frozen=True)
class BrokerReadiness:
    broker_id: str
    display_name: str
    eligible: bool
    reason_code: str
    detail: str
    approved_artefact_ref: str | None
    sha256_suffix: str | None
    servers: tuple = field(default_factory=tuple)
    in_active_catalogue: bool = False

    def as_dict(self) -> dict:
        return {
            "broker_id": self.broker_id, "display_name": self.display_name, "eligible": self.eligible,
            "reason_code": self.reason_code, "detail": self.detail,
            "approved_artefact_ref": self.approved_artefact_ref, "sha256_suffix": self.sha256_suffix,
            "servers": list(self.servers), "in_active_catalogue": self.in_active_catalogue,
        }


def _norm_servers(values) -> tuple:
    return tuple(sorted({str(s) for s in (values or []) if str(s)}))


def _known_broker_ids() -> list[str]:
    ids = set(CatalogueArtefact.objects.values_list("broker_id", flat=True))
    ids |= set(CatalogueCaptureCandidate.objects.values_list("broker_id", flat=True))
    for ref in ArtefactApproval.objects.filter(artefact_kind=ARTEFACT_KIND).values_list("artefact_ref", flat=True):
        ids.add(str(ref).split("/", 1)[0])
    return sorted(i for i in ids if i)


def _evaluate_one(broker_id: str, active_version) -> BrokerReadiness:
    approved = (ArtefactApproval.objects
                .filter(artefact_kind=ARTEFACT_KIND, artefact_ref__startswith=broker_id + "/",
                        status=ArtefactApproval.Status.APPROVED)
                .order_by("-decided_at", "-created_at").first())
    candidate = CatalogueCaptureCandidate.objects.filter(broker_id=broker_id).first()
    display = ""
    if candidate:
        display = candidate.display_name

    if approved is not None:
        artefact = (CatalogueArtefact.objects
                    .filter(broker_id=broker_id, sha256=approved.sha256)
                    .order_by("-created_at").first())
        if artefact is not None and not display:
            display = artefact.display_name
        servers = _norm_servers(artefact.servers if artefact else approved.metadata.get("servers"))
        sanitiser = (artefact.sanitisation_result if artefact
                     else str(approved.metadata.get("sanitisation") or approved.metadata.get("sanitisation_result") or ""))
        in_active = bool(active_version and artefact and artefact.version_id == active_version.id)
        sha_suffix = approved.sha256[-12:]
        if in_active:
            return BrokerReadiness(broker_id, display or broker_id, False, RC_ALREADY_ACTIVE,
                                   f"approved artefact already in ACTIVE version {active_version.label}",
                                   approved.artefact_ref, sha_suffix, servers, True)
        if sanitiser != "PASS":
            return BrokerReadiness(broker_id, display or broker_id, False, RC_SANITISER_NOT_PASS,
                                   f"approved but sanitisation_result={sanitiser!r} (not PASS)",
                                   approved.artefact_ref, sha_suffix, servers, False)
        return BrokerReadiness(broker_id, display or broker_id, True, RC_ELIGIBLE,
                               "approved + sanitiser PASS; eligible for a future additive build",
                               approved.artefact_ref, sha_suffix, servers, False)

    if candidate is not None and candidate.capture_status == CatalogueCaptureCandidate.Status.NEEDS_CAPTURE:
        return BrokerReadiness(broker_id, display or broker_id, False, RC_NEEDS_CAPTURE,
                               "governed NEEDS_CAPTURE candidate; no servers.dat captured yet",
                               None, None, (), False)

    if ArtefactApproval.objects.filter(artefact_kind=ARTEFACT_KIND,
                                        artefact_ref__startswith=broker_id + "/").exists():
        return BrokerReadiness(broker_id, display or broker_id, False, RC_NO_APPROVED_ARTEFACT,
                               "artefact/approval exists but none is APPROVED", None, None, (), False)

    return BrokerReadiness(broker_id, display or broker_id, False, RC_NOT_CAPTURED,
                           "unknown to the catalogue (no artefact, approval or candidate)", None, None, (), False)


def evaluate_catalogue_readiness(broker_ids=None) -> list[BrokerReadiness]:
    """Evaluate readiness for the given brokers (or every broker known to the catalogue). READ-ONLY."""
    ids = list(broker_ids) if broker_ids else _known_broker_ids()
    active_version = CatalogueVersion.objects.filter(status=CatalogueVersion.Status.ACTIVE).first()
    results = [_evaluate_one(bid, active_version) for bid in ids]

    # Duplicate-server detection: a server name may belong to at most one broker. If an ELIGIBLE broker's server
    # collides with another ELIGIBLE broker OR with a DIFFERENT broker's artefact already in the ACTIVE version,
    # the additive build would introduce an ambiguous server -> flip that broker to DUPLICATE_SERVER_CONFLICT.
    active_server_owner: dict[str, str] = {}
    if active_version is not None:
        for art in CatalogueArtefact.objects.filter(version=active_version):
            for s in _norm_servers(art.servers):
                active_server_owner.setdefault(s, art.broker_id)

    eligible = [r for r in results if r.reason_code == RC_ELIGIBLE]
    seen: dict[str, str] = {}
    conflicted: set[str] = set()
    conflict_detail: dict[str, str] = {}
    for r in eligible:
        for s in r.servers:
            owner = active_server_owner.get(s)
            if owner is not None and owner != r.broker_id:
                conflicted.add(r.broker_id); conflict_detail[r.broker_id] = f"server {s!r} already owned by ACTIVE broker {owner!r}"
            if s in seen and seen[s] != r.broker_id:
                conflicted.add(r.broker_id); conflicted.add(seen[s])
                conflict_detail.setdefault(r.broker_id, f"server {s!r} also claimed by {seen[s]!r}")
                conflict_detail.setdefault(seen[s], f"server {s!r} also claimed by {r.broker_id!r}")
            seen.setdefault(s, r.broker_id)

    if not conflicted:
        return results
    out = []
    for r in results:
        if r.broker_id in conflicted:
            out.append(BrokerReadiness(r.broker_id, r.display_name, False, RC_DUPLICATE_SERVER_CONFLICT,
                                       conflict_detail.get(r.broker_id, "server name collides with another broker"),
                                       r.approved_artefact_ref, r.sha256_suffix, r.servers, r.in_active_catalogue))
        else:
            out.append(r)
    return out
