"""broker_catalogue.carry_forward — Option B: an explicit, fail-closed carry-forward-active DRAFT builder.

The shipped ``build_catalogue_version`` groups approvals by ref-label suffix (``<broker>/<label>``), so it cannot
assemble a successor version that keeps the ACTIVE version's already-approved artefacts AND adds a small number of
separately-approved refs at a DIFFERENT label (e.g. carry ``*/v2`` forward and add ``tradersway/v1`` + ``vantage/v1``).

This module builds that successor as a DRAFT by COPYING the single ACTIVE version's artefacts verbatim (preserving
their original approval ref / SHA / size / servers / sanitiser verdict) and adding ONLY the explicitly-named,
separately-APPROVED refs. It is NOT an auto-approve path: every carried artefact must still resolve to an APPROVED
exact-SHA row, and every added ref must be APPROVED and sanitiser-bound to its exact bytes. It creates a DRAFT only —
no activation, no host staging, no provisioning — and is deterministic + idempotent: an identical rerun is a no-op,
while a rerun that would change the DRAFT's inventory is REFUSED rather than mutating it (the DRAFT is deleted and
rebuilt to change inputs). Governance: CLAUDE.md architecture.md (small additive change; no implicit active-broker
replacement) + the human-gated approvals rule (model output can never approve; this only COPIES existing human YES).
"""
from __future__ import annotations

import re

from django.db import transaction

from approvals.models import ArtefactApproval
from broker_catalogue import sanitiser as SAN
from broker_catalogue import service as S
from broker_catalogue.models import CatalogueArtefact, CatalogueVersion

CARRY_FORWARD_REASON = "carry_forward_active"
_KIND = "broker_servers_dat"
_BROKER_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# A ref is "<broker_slug>/<label>"; the label charset mirrors CatalogueVersion.label usage (short, filesystem-safe).
_REF_RE = re.compile(r"^[a-z0-9_]{1,32}/[A-Za-z0-9._-]{1,32}$")


class CarryForwardError(Exception):
    """A carry-forward precondition failed. The message is operator-facing and secret-free."""


def _norm_servers(servers) -> list:
    return sorted(str(s).strip().lower() for s in (servers or []) if str(s).strip())


def _carried_spec(src_art, *, target_label: str, source: CatalogueVersion) -> dict:
    """Byte-for-byte carry of a source artefact's identity into the successor. Only ``host_relpath`` is re-labelled
    (the SAME bytes are re-staged under the new version label); provenance records the carry lineage."""
    prov = dict(src_art.capture_provenance or {})
    prov["carry_forward"] = {
        "from_version": source.label,
        "from_artefact_id": src_art.id,
        "approval_ref": src_art.artefact_ref,
        "sha256": S.norm_sha(src_art.sha256),
        "reason": CARRY_FORWARD_REASON,
    }
    return {
        "broker_id": src_art.broker_id,
        "display_name": src_art.display_name,
        "servers": list(src_art.servers or []),
        "artefact_kind": src_art.artefact_kind,
        "artefact_ref": src_art.artefact_ref,
        "sha256": S.norm_sha(src_art.sha256),
        "size_bytes": int(src_art.size_bytes or 0),
        "source_mt5_build": src_art.source_mt5_build,
        "host_relpath": f"versions/{target_label}/{src_art.broker_id}/servers.dat",
        "capture_provenance": prov,
        "sanitisation_result": src_art.sanitisation_result,
        "certification_result": src_art.certification_result,
        "origin": "carried",
    }


def _added_spec(appr: ArtefactApproval, broker_id: str, *, target_label: str) -> dict:
    """Build a successor artefact from an APPROVED approval's non-secret metadata (identical shape to the legacy
    builder), bound to the approval's exact SHA + servers + size, re-labelled under the successor version."""
    meta = appr.metadata or {}
    servers = [str(s).strip() for s in (meta.get("servers_intended") or meta.get("servers") or []) if str(s).strip()]
    san = meta.get("sanitiser") or {}
    return {
        "broker_id": broker_id,
        "display_name": meta.get("broker", broker_id),
        "servers": servers,
        "artefact_kind": _KIND,
        "artefact_ref": appr.artefact_ref,
        "sha256": S.norm_sha(appr.sha256),
        "size_bytes": int(meta.get("size_bytes") or 0),
        "source_mt5_build": str(meta.get("source_mt5_build") or ""),
        "host_relpath": f"versions/{target_label}/{broker_id}/servers.dat",
        "capture_provenance": {k: meta.get(k) for k in ("source", "candidate_id", "sanitisation_basis")}
        | {"sanitiser_version": san.get("version", ""), "sanitiser_evidence_sha256": san.get("evidence_sha256", ""),
           "carry_forward": {"from_version": None, "from_artefact_id": None, "approval_ref": appr.artefact_ref,
                             "sha256": S.norm_sha(appr.sha256), "reason": "include_approved_ref"}},
        "sanitisation_result": ("PASS" if san.get("passed") is True else (meta.get("sanitisation") or "")),
        "certification_result": ("PASS" if meta.get("behavioural_cert") == "PASS" else "PENDING"),
        "origin": "added",
    }


def _identity(spec: dict) -> tuple:
    """The identity-relevant projection of a spec used for idempotency comparison (order-independent servers)."""
    return (spec["broker_id"], spec["artefact_ref"], S.norm_sha(spec["sha256"]), int(spec["size_bytes"] or 0),
            tuple(_norm_servers(spec["servers"])), spec["host_relpath"])


def _artefact_identity(a: CatalogueArtefact) -> tuple:
    return (a.broker_id, a.artefact_ref, S.norm_sha(a.sha256), int(a.size_bytes or 0),
            tuple(_norm_servers(a.servers)), a.host_relpath)


def plan_carry_forward(*, target_label: str, include_refs) -> list:
    """Compute the deterministic successor inventory (list of specs, sorted by broker_id) WITHOUT writing anything.
    Raises ``CarryForwardError`` on any gate failure. Shared by the writer and by read-only preview/tests."""
    label = str(target_label or "").strip()
    if not label or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", label):
        raise CarryForwardError(f"malformed target label: {target_label!r}")

    # Exactly one ACTIVE source version (the single atomic pointer; defence-in-depth over the DB partial-unique).
    actives = list(CatalogueVersion.objects.filter(status=CatalogueVersion.Status.ACTIVE))
    if len(actives) != 1:
        raise CarryForwardError(f"expected exactly one ACTIVE source version, found {len(actives)}")
    source = actives[0]
    if source.label == label:
        raise CarryForwardError(f"target label {label!r} equals the ACTIVE source; choose a successor label")
    # The source manifest must verify BEFORE we trust its artefacts for carry-forward (no carrying a tampered set).
    if not S.verify_version_integrity(source):
        raise CarryForwardError(f"ACTIVE source {source.label} failed manifest integrity; refuse to carry forward")

    specs: list = []
    by_broker: dict = {}
    by_ref: dict = {}

    # 1) Carry every ACTIVE artefact — each MUST still resolve to an APPROVED exact-SHA row, and its approval's
    #    size/servers must still match (catch an approval that changed under a carried artefact).
    for a in source.artefacts.all().order_by("broker_id"):
        appr = S.approved_row_for(a)
        if appr is None:
            raise CarryForwardError(f"carried broker {a.broker_id} ({a.artefact_ref} {S.norm_sha(a.sha256)[:12]}) "
                                    f"no longer has an APPROVED exact-SHA record; refuse to carry forward")
        meta = appr.metadata or {}
        m_size = meta.get("size_bytes")
        if m_size is not None and int(m_size) != int(a.size_bytes or 0):
            raise CarryForwardError(f"carried {a.broker_id}: size {a.size_bytes} != approval size {m_size}")
        m_servers = meta.get("servers_intended") or meta.get("servers")
        if m_servers is not None and _norm_servers(m_servers) != _norm_servers(a.servers):
            raise CarryForwardError(f"carried {a.broker_id}: servers differ from the approval's servers_intended")
        own = S.server_ownership_problems(a.broker_id, a.servers)
        if own:
            raise CarryForwardError(f"carried {a.broker_id} server ownership: {own}")
        spec = _carried_spec(a, target_label=label, source=source)
        specs.append(spec)
        by_broker[a.broker_id] = spec
        by_ref[a.artefact_ref] = spec

    # 2) Add ONLY the explicitly-named refs. Never "every approved row"; each must be APPROVED + sanitiser-bound and
    #    must NOT implicitly replace a carried (active) broker.
    seen_include: set = set()
    for raw in (include_refs or []):
        ref = str(raw or "").strip()
        if not _REF_RE.fullmatch(ref):
            raise CarryForwardError(f"malformed --include-approved-ref: {raw!r} (expected '<broker>/<label>')")
        if ref in seen_include:
            raise CarryForwardError(f"duplicate --include-approved-ref: {ref}")
        seen_include.add(ref)
        broker_id = ref.split("/", 1)[0]
        if not _BROKER_RE.fullmatch(broker_id):
            raise CarryForwardError(f"malformed broker slug in ref: {ref}")
        if broker_id in by_broker:
            raise CarryForwardError(f"ref {ref} broker {broker_id!r} is already ACTIVE (carried) — refuse to "
                                    f"implicitly replace an active broker; a byte change needs its own version")
        if ref in by_ref:
            raise CarryForwardError(f"duplicate destination ref {ref}")
        approved = list(ArtefactApproval.objects.filter(
            artefact_kind=_KIND, artefact_ref=ref, status=ArtefactApproval.Status.APPROVED))
        if not approved:
            raise CarryForwardError(f"no APPROVED approval for ref {ref}")
        if len(approved) > 1:   # defence-in-depth: the DB partial-unique already forbids this
            raise CarryForwardError(f"ref {ref} has {len(approved)} APPROVED rows; expected exactly one")
        appr = approved[0]
        if not SAN.verify_verdict((appr.metadata or {}).get("sanitiser") or {}, expected_sha256=appr.sha256):
            raise CarryForwardError(f"ref {ref} sanitiser verdict is not a valid PASS bound to the approved bytes")
        spec = _added_spec(appr, broker_id, target_label=label)
        if not spec["servers"]:
            raise CarryForwardError(f"ref {ref}: approval metadata declares no servers_intended")
        own = S.server_ownership_problems(broker_id, spec["servers"])
        if own:
            raise CarryForwardError(f"ref {ref} server ownership: {own}")
        specs.append(spec)
        by_broker[broker_id] = spec
        by_ref[ref] = spec

    # 3) Whole-set invariants: unique destination host_relpath + no server-name claimed by two artefacts.
    relpaths = [s["host_relpath"] for s in specs]
    if len(set(relpaths)) != len(relpaths):
        raise CarryForwardError("duplicate destination host_relpath across the successor inventory")
    server_claims: dict = {}
    for s in specs:
        for name in _norm_servers(s["servers"]):
            if name in server_claims and server_claims[name] != s["broker_id"]:
                raise CarryForwardError(f"server {name!r} claimed by both {server_claims[name]} and {s['broker_id']}")
            server_claims[name] = s["broker_id"]

    return sorted(specs, key=lambda s: s["broker_id"])


@transaction.atomic
def build_carry_forward_draft(*, target_label: str, include_refs) -> CatalogueVersion:
    """Create (or idempotently confirm) the DRAFT successor from the ACTIVE version + explicit refs. Returns the
    DRAFT ``CatalogueVersion``. Fail-closed: raises ``CarryForwardError`` on any gate failure, and NEVER mutates an
    existing DRAFT whose inventory differs from the planned one (delete it to rebuild with different inputs)."""
    specs = plan_carry_forward(target_label=str(target_label).strip(), include_refs=include_refs)
    label = str(target_label).strip()

    target, _created = CatalogueVersion.objects.select_for_update().get_or_create(
        label=label, defaults={"status": CatalogueVersion.Status.DRAFT})
    if target.status != CatalogueVersion.Status.DRAFT:
        raise CarryForwardError(f"target {label} is {target.status}; a carry-forward build creates a DRAFT only")

    existing = list(target.artefacts.all())
    if existing:
        want = {s["broker_id"]: _identity(s) for s in specs}
        have = {a.broker_id: _artefact_identity(a) for a in existing}
        if want != have:
            raise CarryForwardError(
                f"DRAFT {label} already exists with a DIFFERENT inventory; refuse to mutate it. "
                f"Delete the DRAFT and rebuild to change inputs (deterministic idempotency).")
        return target   # identical rerun -> no-op

    for s in specs:
        fields = {k: v for k, v in s.items() if k != "origin"}
        CatalogueArtefact.objects.create(version=target, **fields)
    return target


def inventory_rows(version: CatalogueVersion) -> list:
    """A stable, secret-free row-by-row inventory of a version (for the build/activate reports). Sorted by broker."""
    return [{"broker_id": a.broker_id, "artefact_ref": a.artefact_ref, "sha256": S.norm_sha(a.sha256),
             "size_bytes": int(a.size_bytes or 0), "servers": list(a.servers or []),
             "host_relpath": a.host_relpath, "sanitisation_result": a.sanitisation_result,
             "certification_result": a.certification_result, "approved": S.approved_row_for(a) is not None}
            for a in version.artefacts.all().order_by("broker_id")]
