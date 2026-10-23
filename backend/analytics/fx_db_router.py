"""analytics.fx_db_router — routing guard for the FX ingestion alias (ADR-0058 / A3).

The ``fx_ingest`` alias is a dedicated least-privilege identity used ONLY via an EXPLICIT ``.using("fx_ingest")``
by ``fetch_fx_daily``. This router:

* does NOT auto-route any read/write to ``fx_ingest`` (returns ``None`` => Django uses ``default``), so no
  unrelated ORM activity ever reaches the ingestion connection; and
* BLOCKS migrations on ``fx_ingest`` (both aliases target the same physical database; schema changes run only via
  ``default`` under the separately governed migration identity).

It is otherwise opinion-free (``None``), so all existing routing and migrations on ``default`` are unchanged.
"""


class FxIngestRouter:
    FX_INGEST = "fx_ingest"

    def db_for_read(self, model, **hints):
        return None

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never migrate through the ingestion alias; no opinion on any other database.
        if db == self.FX_INGEST:
            return False
        return None
