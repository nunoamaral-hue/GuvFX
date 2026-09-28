"""broker_catalogue.service — resolution + verification for Broker Catalogue V1.

Pure, side-effect-light logic consumed by provisioning. Fail-closed everywhere; DARK-aware (the master flag is
checked by the caller/preseed layer, not here, so this module stays unit-testable without env). It NEVER copies
files, contacts a host, promotes a candidate, or touches the golden — it only RESOLVES what the active,
approved catalogue says a given broker server should bootstrap from, and VERIFIES the human approval binding.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from broker_catalogue.models import CatalogueArtefact, CatalogueVersion

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Current STRONG manifest algorithm. A version records the algo it was built with (CatalogueVersion.manifest_algo);
# "" means the legacy algo (v1, broker_id:sha256 pairs only). The strong algo also binds each artefact's servers,
# size, kind and sanitisation verdict, so a servers-list / size / sanitisation mutation — not only a byte change —
# changes the manifest. New builds stamp MANIFEST_ALGO; verification recomputes with the version's own algo, so
# already-ACTIVE legacy versions keep verifying unchanged.
MANIFEST_ALGO = "servers_v2"

# Resolution outcomes (stable, secret-free) — mirror the order_transport reason-code idiom.
PRESEED_SUPPORTED = "catalogue_supported"                 # approved + verified artefact resolved -> preseed it
PRESEED_NATIVE_FALLBACK = "catalogue_fallback_native_discovery"  # broker unsupported -> leave neutral, native
PRESEED_UNAPPROVED = "catalogue_artefact_unapproved"      # supported broker but no APPROVED exact-SHA row
PRESEED_NO_ACTIVE = "catalogue_no_active_version"         # no ACTIVE version -> native fallback
PRESEED_DISABLED = "catalogue_disabled"                   # master flag off (set by the preseed layer)
PRESEED_MANIFEST_INVALID = "catalogue_manifest_invalid"   # ACTIVE version failed manifest re-verification -> native


@dataclass(frozen=True)
class PreseedPlan:
    """The resolved plan for one account. ``preseed`` gates the host copy; when False the runtime is left
    broker-neutral (native discovery). All identity fields are server-derived (never client-supplied)."""
    preseed: bool
    reason_code: str
    broker_id: str = ""
    server_name: str = ""
    sha256: str = ""
    host_relpath: str = ""
    catalogue_version: str = ""
    fallback_native: bool = False

    def as_dict(self) -> dict:
        return {"preseed": self.preseed, "reason_code": self.reason_code, "broker_id": self.broker_id,
                "server_name": self.server_name, "sha256": self.sha256, "host_relpath": self.host_relpath,
                "catalogue_version": self.catalogue_version, "fallback_native": self.fallback_native}


def norm_sha(s: str) -> str:
    return str(s or "").strip().lower()


def resolve_active_version() -> Optional[CatalogueVersion]:
    """The single ACTIVE catalogue version, or None. The DB partial-unique constraint guarantees at most one."""
    return CatalogueVersion.objects.filter(status=CatalogueVersion.Status.ACTIVE).first()


def resolve_artefact_for_server(server_name: str, version: Optional[CatalogueVersion] = None
                                ) -> Optional[CatalogueArtefact]:
    """Find the active version's artefact whose ``servers`` list contains ``server_name`` (case-insensitive).
    Broker is resolved from the AUTHORITATIVE broker-server name, never from email/account-number/runtime
    remnants. Returns None (=> native fallback) when unsupported."""
    server_name = str(server_name or "").strip()
    if not server_name:
        return None
    version = version or resolve_active_version()
    if version is None:
        return None
    target = server_name.lower()
    for art in version.artefacts.all():
        if any(target == str(s).strip().lower() for s in (art.servers or [])):
            return art
    return None


def artefact_is_approved(artefact: CatalogueArtefact) -> bool:
    """True only when the human approvals gate holds an APPROVED row for these EXACT bytes. Fail-closed:
    delegates to approvals.is_artefact_approved (which itself requires APPROVALS_ENABLED)."""
    from approvals.services import is_artefact_approved
    sha = norm_sha(artefact.sha256)
    if not _SHA_RE.fullmatch(sha):
        return False
    return is_artefact_approved(artefact_kind=artefact.artefact_kind, artefact_ref=artefact.artefact_ref, sha256=sha)


def approved_row_for(artefact: CatalogueArtefact):
    """The APPROVED ``ArtefactApproval`` binding these exact (kind, ref, sha256) bytes, or None. Resolved by
    identity (not the FK) so the binding does not depend on build having populated the link."""
    from approvals.models import ArtefactApproval
    sha = norm_sha(artefact.sha256)
    if not _SHA_RE.fullmatch(sha):
        return None
    return ArtefactApproval.objects.filter(
        artefact_kind=artefact.artefact_kind, artefact_ref=artefact.artefact_ref,
        sha256=sha, status=ArtefactApproval.Status.APPROVED).first()


def resolve_broker_preseed(account) -> PreseedPlan:
    """Resolve the preseed plan for a hosted account from its authoritative broker server. Fail-closed.

    - No ACTIVE version                      -> native fallback (PRESEED_NO_ACTIVE)
    - Server not covered by any artefact     -> native fallback (PRESEED_NATIVE_FALLBACK)  [NOT an error]
    - Covered but NOT approved (exact SHA)   -> do NOT preseed (PRESEED_UNAPPROVED); native fallback preserved
    - Covered + approved + valid SHA         -> preseed (PRESEED_SUPPORTED)
    """
    bs = getattr(account, "broker_server", None)
    server_name = getattr(bs, "server_name", None) or ""
    version = resolve_active_version()
    if version is None:
        return PreseedPlan(False, PRESEED_NO_ACTIVE, server_name=server_name, fallback_native=True)
    # Tamper-evidence at consumption: an ACTIVE version whose artefacts no longer match its stamped manifest (e.g. a
    # post-activation DB edit) is NOT trusted — fall back to native discovery rather than copy possibly-mutated bytes.
    if not verify_version_integrity(version):
        return PreseedPlan(False, PRESEED_MANIFEST_INVALID, server_name=server_name,
                           catalogue_version=version.label, fallback_native=True)
    art = resolve_artefact_for_server(server_name, version)
    if art is None:
        return PreseedPlan(False, PRESEED_NATIVE_FALLBACK, server_name=server_name,
                           catalogue_version=version.label, fallback_native=True)
    sha = norm_sha(art.sha256)
    if not _SHA_RE.fullmatch(sha) or not artefact_is_approved(art):
        # A SUPPORTED broker whose artefact is not approved (or malformed) must NOT be copied. Fall back to native
        # discovery rather than fail the whole provisioning (safety condition = never copy unapproved bytes).
        return PreseedPlan(False, PRESEED_UNAPPROVED, broker_id=art.broker_id, server_name=server_name,
                           catalogue_version=version.label, fallback_native=True)
    return PreseedPlan(True, PRESEED_SUPPORTED, broker_id=art.broker_id, server_name=server_name,
                       sha256=sha, host_relpath=art.host_relpath, catalogue_version=version.label)


def _legacy_manifest(version: CatalogueVersion) -> str:
    """The shipped v1 algo — aggregate over (broker_id, sha256) pairs only. Kept byte-identical so already-ACTIVE
    legacy versions (manifest_algo == "") continue to verify. Do NOT change this."""
    parts = sorted(f"{a.broker_id}:{norm_sha(a.sha256)}" for a in version.artefacts.all())
    return hashlib.sha256(("\n".join(parts)).encode("utf-8")).hexdigest()


def _strong_manifest(version: CatalogueVersion) -> str:
    """The hardened algo — binds, per artefact, broker_id + sha256 + sorted servers + size + kind + sanitisation,
    so a servers-list / size / sanitisation change (not only a byte change) alters the manifest. Deterministic:
    fields are canonicalised and artefacts are sorted by broker_id (no filesystem/query-order dependence)."""
    rows = []
    for a in version.artefacts.all():
        servers = "|".join(sorted(str(s).strip().lower() for s in (a.servers or [])))
        rows.append("\x1f".join([
            "broker=" + str(a.broker_id),
            "sha256=" + norm_sha(a.sha256),
            "servers=" + servers,
            "size=" + str(int(a.size_bytes or 0)),
            "kind=" + str(a.artefact_kind or ""),
            "sanitised=" + str(a.sanitisation_result or ""),
        ]))
    body = "\n".join(sorted(rows))
    return hashlib.sha256((MANIFEST_ALGO + "\x1e" + body).encode("utf-8")).hexdigest()


def compute_manifest_sha(version: CatalogueVersion, algo: Optional[str] = None) -> str:
    """Deterministic aggregate manifest hash for a version, tamper-evident as a whole. ``algo`` selects the scheme;
    when None it defaults to the strong current algo (for NEW builds). Verification passes the version's stored
    ``manifest_algo`` so a legacy (v1) version recomputes under the legacy algo and keeps verifying."""
    use = MANIFEST_ALGO if algo is None else algo
    if use == MANIFEST_ALGO:
        return _strong_manifest(version)
    return _legacy_manifest(version)   # "" / legacy


def verify_version_integrity(version: CatalogueVersion) -> bool:
    """True iff the version's stored ``manifest_sha256`` still matches a recompute under its own ``manifest_algo``.
    Detects any post-activation mutation of an artefact's identity/SHA/servers/size (e.g. a direct DB edit)."""
    stored = norm_sha(version.manifest_sha256)
    if not _SHA_RE.fullmatch(stored):
        return False
    return compute_manifest_sha(version, algo=(version.manifest_algo or "")) == stored


def duplicate_server_names(version: CatalogueVersion) -> list:
    """Server names claimed by MORE THAN ONE artefact in the version (case-insensitive). A non-empty result means
    ambiguous resolution / catalogue poisoning and MUST fail activation. Deterministic (sorted)."""
    from collections import Counter
    counts = Counter()
    for a in version.artefacts.all():
        for s in {str(x).strip().lower() for x in (a.servers or []) if str(x).strip()}:
            counts[s] += 1
    return sorted(name for name, n in counts.items() if n > 1)
