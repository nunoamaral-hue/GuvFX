"""activate_catalogue_version — promote a DRAFT catalogue version to ACTIVE (governed, atomic, fail-closed).

The activation transaction refuses to promote unless EVERY gate passes, so a live catalogue is never partially
active and never carries an unverified artefact:
  * every artefact is human-APPROVED for its exact SHA (approvals app);
  * every artefact's approval carries a machine sanitiser PASS verdict (``metadata.sanitiser.passed``);
  * the artefact's SHA + size + servers match its approval (approval binds the exact bytes/identity);
  * no server name is claimed by two artefacts (deterministic, unambiguous routing);
  * a broker carried over from the outgoing ACTIVE version keeps its SHA (no silent byte change) unless
    ``--allow-byte-change`` is given;
  * with ``--attest-host``, the bytes staged on the host at each ``host_relpath`` read back to the approved SHA;
  * with ``--require-certified``, behavioural ``certification_result == PASS``.
On success it retires the previous ACTIVE (recording ``rollback_to`` so re-activating the predecessor is the
rollback) and stamps the aggregate strong-algo manifest. Atomic under a transaction; on any failure the previous
ACTIVE version remains authoritative.

Usage:  activate_catalogue_version --label v2
        activate_catalogue_version --label v2 --attest-host --require-certified
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from broker_catalogue import sanitiser as SAN
from broker_catalogue import service as S
from broker_catalogue.models import CatalogueVersion


# Injection seam for host byte-staging attestation (Phase 7). Returns an object with
# ``attest_broker_artefact(host_relpath, expected_sha256) -> {"ok": bool, "verified_sha256": str}`` or None.
# STATUS: no production attestation transport is wired yet (the signed host executor has no attest primitive), so
# ``--attest-host`` FAILS CLOSED until it is implemented — it is NOT a silent no-op. When ``--attest-host`` is NOT
# passed, activation performs no host-byte check and relies on consumption-time read-back verification
# (broker_catalogue.preseed + Preseed-GuvfxBrokerArtefact.ps1). Wiring a real machine-level attest primitive is a
# tracked follow-up (see docs/KNOWN_ISSUES.md).
def _attest_executor():
    return None


class Command(BaseCommand):
    help = "Promote a DRAFT catalogue version to ACTIVE (atomic, fail-closed; sanitiser + uniqueness + attest gates)."

    def add_arguments(self, parser):
        parser.add_argument("--label", required=True)
        parser.add_argument("--require-certified", action="store_true",
                            help="Also require certification_result == PASS for every artefact (behavioural cert).")
        parser.add_argument("--attest-host", action="store_true",
                            help="Read back the host-staged bytes and require each SHA == the approved artefact SHA.")
        parser.add_argument("--allow-byte-change", action="store_true",
                            help="Permit a carried-over broker's SHA to differ from the outgoing ACTIVE version.")

    def handle(self, *args, **opts):
        label = opts["label"]
        with transaction.atomic():
            try:
                version = CatalogueVersion.objects.select_for_update().get(label=label)
            except CatalogueVersion.DoesNotExist:
                raise CommandError(f"no such catalogue version: {label}")
            if version.status == CatalogueVersion.Status.ACTIVE:
                self.stdout.write(f"[catalogue] {label} already ACTIVE"); return
            if version.status != CatalogueVersion.Status.DRAFT:
                raise CommandError(f"version {label} is {version.status}; only a DRAFT can be activated")
            # Lock the artefact rows (M1): the manifest stamped below is computed from THIS exact validated list, so a
            # concurrent committed artefact mutation cannot slip between the gate reads and the stamp.
            arts = list(version.artefacts.select_for_update().order_by("broker_id"))
            if not arts:
                raise CommandError(f"version {label} has no artefacts")

            # Gate 1 — human approval for the exact SHA of every artefact.
            unapproved = [a.broker_id for a in arts if not S.artefact_is_approved(a)]
            if unapproved:
                raise CommandError(f"REFUSED: unapproved artefact(s): {unapproved}. "
                                   f"Human approval (approvals app) required for each exact SHA before activation.")

            # Gate 2 — the machine sanitiser verdict must be a genuine PASS bound to the EXACT approved bytes (not
            # operator free-text): verify_verdict requires passed + sha256==artefact SHA + a self-consistent evidence
            # hash. Also cross-check the approval's size/servers against the artefact, and that each server actually
            # belongs to this broker (no cross-broker/foreign server that would mis-route preseed).
            for a in arts:
                appr = S.approved_row_for(a)                       # by identity (kind, ref, exact SHA)
                meta = (appr.metadata or {}) if appr else {}
                if not SAN.verify_verdict(meta.get("sanitiser") or {}, expected_sha256=a.sha256):
                    raise CommandError(f"REFUSED: {a.broker_id} sanitiser verdict is not a valid PASS bound to the "
                                       f"approved bytes — run sanitise_broker_artefact on the exact artefact + re-approve.")
                m_size = meta.get("size_bytes")
                if m_size is not None and int(m_size) != int(a.size_bytes or 0):
                    raise CommandError(f"REFUSED: {a.broker_id} size {a.size_bytes} != approval size {m_size}.")
                m_servers = meta.get("servers_intended") or meta.get("servers")
                if m_servers is not None:
                    if sorted(str(s).strip().lower() for s in m_servers) != \
                       sorted(str(s).strip().lower() for s in (a.servers or [])):
                        raise CommandError(f"REFUSED: {a.broker_id} servers != approval servers_intended.")
                own = S.server_ownership_problems(a.broker_id, a.servers)
                if own:
                    raise CommandError(f"REFUSED: {a.broker_id} server ownership: {own}")

            # Gate 3 — deterministic, unambiguous routing: no server name claimed by two artefacts (locked list).
            dups = S.duplicate_server_names(version, artefacts=arts)
            if dups:
                raise CommandError(f"REFUSED: server name(s) claimed by multiple artefacts: {dups}")

            if opts["require_certified"]:
                uncert = [a.broker_id for a in arts if a.certification_result != "PASS"]
                if uncert:
                    raise CommandError(f"REFUSED: uncertified artefact(s): {uncert} (behavioural cert not PASS).")

            prev = CatalogueVersion.objects.select_for_update().filter(
                status=CatalogueVersion.Status.ACTIVE).first()

            # Gate 4 — carried-over brokers keep their bytes (no silent byte change) unless explicitly allowed.
            if prev is not None and not opts["allow_byte_change"]:
                prev_sha = {a.broker_id: S.norm_sha(a.sha256) for a in prev.artefacts.all()}
                changed = [a.broker_id for a in arts
                           if a.broker_id in prev_sha and prev_sha[a.broker_id] != S.norm_sha(a.sha256)]
                if changed:
                    raise CommandError(f"REFUSED: carried-over broker(s) changed SHA vs ACTIVE {prev.label}: "
                                       f"{changed}. Re-approve intentionally + pass --allow-byte-change.")

            # Gate 5 — host byte-staging attestation (each host_relpath reads back to the approved SHA).
            if opts["attest_host"]:
                ex = _attest_executor()
                if ex is None:
                    raise CommandError("REFUSED: --attest-host requested but no attestation transport is configured.")
                for a in arts:
                    try:
                        res = ex.attest_broker_artefact(host_relpath=a.host_relpath,
                                                        expected_sha256=S.norm_sha(a.sha256))
                    except Exception:  # noqa: BLE001 — any host error fails the attestation closed
                        res = {"ok": False}
                    if not (res and res.get("ok") and
                            S.norm_sha(res.get("verified_sha256", "")) == S.norm_sha(a.sha256)):
                        raise CommandError(f"REFUSED: host attestation failed for {a.broker_id} "
                                           f"(staged bytes at {a.host_relpath} do not match the approved SHA).")

            now = timezone.now()
            if prev is not None:
                prev.status = CatalogueVersion.Status.RETIRED
                prev.retired_at = now
                prev.save(update_fields=["status", "retired_at"])
                version.rollback_to = prev
            version.status = CatalogueVersion.Status.ACTIVE
            version.activated_at = now
            version.manifest_algo = S.MANIFEST_ALGO                                   # new versions use the strong algo
            version.manifest_sha256 = S.compute_manifest_sha(version, artefacts=arts)  # stamp the VALIDATED locked list
            version.save(update_fields=["status", "activated_at", "manifest_algo", "manifest_sha256", "rollback_to"])
            if not S.verify_version_integrity(version, artefacts=arts):               # self-check the stamp round-trips
                raise CommandError("REFUSED: post-stamp manifest self-verification failed (integrity bug).")
        self.stdout.write(f"[catalogue] ACTIVATED {label} algo={version.manifest_algo} "
                          f"manifest_sha256={version.manifest_sha256} artefacts={[a.broker_id for a in arts]}")
