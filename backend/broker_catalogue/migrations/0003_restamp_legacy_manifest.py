"""Re-stamp any legacy (manifest_algo == "") ACTIVE/RETIRED catalogue version to the strong manifest algo.

Before this branch, versions carried the weak legacy manifest (broker_id:sha256 pairs only), which does NOT cover
servers/size — so a legacy ACTIVE version's routing was tamper-transparent, and the algo field was a downgrade
selector. The hardened ``verify_version_integrity`` now REQUIRES the strong algo, so this one-time data migration
upgrades existing ACTIVE/RETIRED versions in place (recomputing the strong manifest from their unchanged artefacts).

Safety: this only rewrites the version's ``manifest_sha256``/``manifest_algo`` (not any artefact bytes/servers). On a
fresh DB (tests/new installs) there are no such versions, so it is a no-op. Rolling the code back is safe too — the
pre-hardening code does not verify the manifest at consumption at all. The strong computation is inlined here (frozen)
so this historical migration never drifts from a future ``service._strong_manifest``.
"""
import hashlib

from django.db import migrations

MANIFEST_ALGO = "servers_v2"


def _norm(s):
    return str(s or "").strip().lower()


def _strong_manifest(version):
    rows = []
    for a in version.artefacts.all():
        servers = "|".join(sorted(str(s).strip().lower() for s in (a.servers or [])))
        rows.append("\x1f".join([
            "broker=" + str(a.broker_id),
            "sha256=" + _norm(a.sha256),
            "servers=" + servers,
            "size=" + str(int(a.size_bytes or 0)),
            "kind=" + str(a.artefact_kind or ""),
            "sanitised=" + str(a.sanitisation_result or ""),
            "host_relpath=" + str(a.host_relpath or ""),
            "ref=" + str(a.artefact_ref or ""),
        ]))
    body = "\n".join(sorted(rows))
    return hashlib.sha256((MANIFEST_ALGO + "\x1e" + body).encode("utf-8")).hexdigest()


def restamp(apps, schema_editor):
    CatalogueVersion = apps.get_model("broker_catalogue", "CatalogueVersion")
    for v in CatalogueVersion.objects.exclude(manifest_algo=MANIFEST_ALGO).filter(status__in=["ACTIVE", "RETIRED"]):
        if not v.artefacts.exists():
            continue
        v.manifest_sha256 = _strong_manifest(v)
        v.manifest_algo = MANIFEST_ALGO
        v.save(update_fields=["manifest_sha256", "manifest_algo"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("broker_catalogue", "0002_catalogueversion_manifest_algo")]
    operations = [migrations.RunPython(restamp, noop)]
