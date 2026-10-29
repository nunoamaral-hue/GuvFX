"""Additively register the Pepperstone UK live MT5 server in the authoritative BrokerServerCatalogue.

Account #49 (Pepperstone Live GBP) connects to the live server ``PepperstoneUK-Live``. It was operator-attested
by Nuno Amaral and freshly bridge-verified from production on 2026-10-29 (``account_match=true``, masked login
``***5983``, server ``PepperstoneUK-Live``, ``trade_mode=2`` LIVE, 9 deals incl. 4 closed trades on the
read-only history endpoint). Pepperstone was absent from the pilot catalogue seeded by ``0020`` (IS6FX +
TradersWay only) and from ``0021`` (Vantage), so its ``BrokerServer`` row was auto-created with
``environment_source='inferred'`` — correct demo/live classification, but not catalogue-authoritative.

This migration is ADDITIVE by construction (identical shape to ``0021``):
  (1) ``update_or_create`` on the casefolded ``name_key`` adds exactly the Pepperstone catalogue row and can
      never remove or alter the IS6FX/TradersWay/Vantage rows ``0020``/``0021`` seeded (regression-tested).
  (2) It re-stamps ONLY the existing ``PepperstoneUK-Live`` BrokerServer row from ``inferred`` to ``catalogue``
      — and only when that row's ``environment`` is already ``live`` (consistent with the verified catalogue).
      No other BrokerServer row is touched; no account is touched.

Reverse is a no-op (mirrors ``0020``/``0021``): the catalogue is add-only and we never destructively unseed a
verified server or silently downgrade a stamped row.
"""
from django.db import migrations

PEPPERSTONE_SERVER = "PepperstoneUK-Live"


def _seed_pepperstone(apps, schema_editor):
    BrokerServer = apps.get_model("trading", "BrokerServer")
    Cat = apps.get_model("trading", "BrokerServerCatalogue")
    seed = [
        (PEPPERSTONE_SERVER, "live", "Pepperstone",
         "Pepperstone published live MT5 server; operator-attested by Nuno Amaral + bridge-verified 2026-10-29 "
         "(account #49 connected live, PepperstoneUK-Live, trade_mode=2, read-only 90-day history available)."),
    ]
    for name, env, disp, ev in seed:
        canonical = " ".join(name.split())
        key = canonical.casefold()
        Cat.objects.update_or_create(
            name_key=key,
            defaults={"server_name": canonical, "environment": env,
                      "is_verified": True, "broker_display_name": disp, "evidence": ev})
    # Re-stamp the single existing Pepperstone BrokerServer row inferred -> catalogue, only if already classified
    # live (never flip an environment; only upgrade the provenance to match the now-authoritative catalogue).
    for s in BrokerServer.objects.filter(server_name__iexact=PEPPERSTONE_SERVER):
        if s.environment == "live" and s.environment_source != "catalogue":
            s.environment_source = "catalogue"
            s.save(update_fields=["environment_source"])


class Migration(migrations.Migration):

    dependencies = [
        ('trading', '0021_vantage_live_catalogue'),
    ]

    operations = [
        migrations.RunPython(_seed_pepperstone, migrations.RunPython.noop),
    ]
