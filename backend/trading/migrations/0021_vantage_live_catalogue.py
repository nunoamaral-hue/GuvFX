"""Additively register the Vantage live MT5 server in the authoritative BrokerServerCatalogue.

Account #52 (Vantage NR) connects to the live server ``VantageMarkets-Live 10`` and was operator-verified as
LIVE from production evidence (visibly connected, balance in GBP) on 2026-10-27. Vantage was absent from the
pilot catalogue seeded by ``0020`` (IS6FX + TradersWay only), so its ``BrokerServer`` row was auto-created with
``environment_source='inferred'`` — correct demo/live classification, but not catalogue-authoritative.

This migration is ADDITIVE by construction:
  (1) ``update_or_create`` on the casefolded ``name_key`` adds exactly the Vantage catalogue row and can never
      remove or alter the IS6FX/TradersWay rows ``0020`` seeded (regression-tested).
  (2) It re-stamps ONLY the existing ``VantageMarkets-Live 10`` BrokerServer row from ``inferred`` to
      ``catalogue`` — and only when that row's ``environment`` is already ``live`` (consistent with the verified
      catalogue). Provisioning never retroactively re-stamps an existing row, so without this the live Vantage
      row would stay ``inferred`` forever. No other BrokerServer row is touched; no account is touched.

Reverse is a no-op (mirrors ``0020``): the catalogue is add-only and we never destructively unseed a verified
server or silently downgrade a stamped row.
"""
from django.db import migrations

VANTAGE_SERVER = "VantageMarkets-Live 10"


def _seed_vantage(apps, schema_editor):
    BrokerServer = apps.get_model("trading", "BrokerServer")
    Cat = apps.get_model("trading", "BrokerServerCatalogue")
    seed = [
        (VANTAGE_SERVER, "live", "Vantage",
         "Vantage published live MT5 server; operator-verified 2026-10-27 (account #52 connected live, GBP balance)."),
    ]
    for name, env, disp, ev in seed:
        canonical = " ".join(name.split())
        key = canonical.casefold()
        Cat.objects.update_or_create(
            name_key=key,
            defaults={"server_name": canonical, "environment": env,
                      "is_verified": True, "broker_display_name": disp, "evidence": ev})
    # Re-stamp the single existing Vantage BrokerServer row inferred -> catalogue, only if already classified live
    # (never flip an environment; only upgrade the provenance to match the now-authoritative catalogue).
    for s in BrokerServer.objects.filter(server_name__iexact=VANTAGE_SERVER):
        if s.environment == "live" and s.environment_source != "catalogue":
            s.environment_source = "catalogue"
            s.save(update_fields=["environment_source"])


class Migration(migrations.Migration):

    dependencies = [
        ('trading', '0020_broker_server_environment_governance'),
    ]

    operations = [
        migrations.RunPython(_seed_vantage, migrations.RunPython.noop),
    ]
