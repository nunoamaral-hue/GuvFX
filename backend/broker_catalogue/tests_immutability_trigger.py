"""Stream A / ADR-0078 — DB-level immutability for CatalogueArtefact.

Proves the DB trigger enforces the SAME rule as the ORM save() guard and closes the raw-UPDATE / bulk_update
bypass: a frozen (non-DRAFT version) artefact cannot be mutated by ANY write path, while a DRAFT artefact
remains freely editable. The raw-SQL-path assertions require PostgreSQL (the trigger is a no-op elsewhere; the
ORM guard still applies everywhere).
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import Error, connection, transaction
from django.test import TestCase
from unittest import skipUnless

from .models import CatalogueArtefact, CatalogueVersion

_PG = connection.vendor == "postgresql"


class CatalogueArtefactImmutabilityTests(TestCase):
    def _draft_artefact(self, *, label="vtest", broker="brokerx", sha="a" * 64):
        v = CatalogueVersion.objects.create(label=label, status=CatalogueVersion.Status.DRAFT)
        a = CatalogueArtefact.objects.create(
            version=v, broker_id=broker, display_name="Broker X", servers=["BrokerX-Live"],
            artefact_ref=f"{broker}/v1", sha256=sha, size_bytes=100,
            host_relpath=f"versions/{label}/{broker}/servers.dat", sanitisation_result="PASS")
        return v, a

    def _freeze(self, v, status=CatalogueVersion.Status.ACTIVE):
        # Set the version non-DRAFT WITHOUT touching the artefact (version table has no trigger/guard).
        CatalogueVersion.objects.filter(pk=v.pk).update(status=status)

    def test_draft_artefact_is_mutable_via_both_paths(self):
        v, a = self._draft_artefact()
        self.assertEqual(CatalogueArtefact.objects.filter(pk=a.pk).update(size_bytes=200), 1)  # raw update OK in DRAFT
        a.refresh_from_db()
        self.assertEqual(a.size_bytes, 200)
        a.size_bytes = 300
        a.save()  # ORM save OK in DRAFT
        a.refresh_from_db()
        self.assertEqual(a.size_bytes, 300)

    def test_orm_save_refused_once_active(self):
        v, a = self._draft_artefact()
        self._freeze(v, CatalogueVersion.Status.ACTIVE)
        a.refresh_from_db()
        a.size_bytes = 999
        with self.assertRaises(ValidationError):
            a.save()

    @skipUnless(_PG, "DB trigger is PostgreSQL-only; the ORM guard covers other backends")
    def test_raw_update_refused_once_active(self):
        v, a = self._draft_artefact(label="vactive", broker="brokeractive")
        self._freeze(v, CatalogueVersion.Status.ACTIVE)
        with self.assertRaises(Error):
            with transaction.atomic():
                CatalogueArtefact.objects.filter(pk=a.pk).update(sha256="f" * 64)
        a.refresh_from_db()
        self.assertEqual(a.sha256, "a" * 64)  # bytes-pin untouched

    @skipUnless(_PG, "DB trigger is PostgreSQL-only")
    def test_raw_update_refused_once_retired(self):
        v, a = self._draft_artefact(label="vretired", broker="brokerretired")
        self._freeze(v, CatalogueVersion.Status.RETIRED)
        with self.assertRaises(Error):
            with transaction.atomic():
                CatalogueArtefact.objects.filter(pk=a.pk).update(size_bytes=123456)
        a.refresh_from_db()
        self.assertEqual(a.size_bytes, 100)

    @skipUnless(_PG, "DB trigger is PostgreSQL-only")
    def test_bulk_update_refused_once_active(self):
        v, a = self._draft_artefact(label="vbulk", broker="brokerbulk")
        self._freeze(v, CatalogueVersion.Status.ACTIVE)
        a.refresh_from_db()
        a.size_bytes = 777
        with self.assertRaises(Error):
            with transaction.atomic():
                CatalogueArtefact.objects.bulk_update([a], ["size_bytes"])
        a.refresh_from_db()
        self.assertEqual(a.size_bytes, 100)

    @skipUnless(_PG, "DB trigger is PostgreSQL-only")
    def test_adversarial_update_cannot_move_artefact_to_another_frozen_version(self):
        # An attacker tries to re-point a frozen artefact at a different version via raw update -> still refused
        # (the OLD row's version is non-DRAFT, so the BEFORE UPDATE trigger fires before the move).
        v, a = self._draft_artefact(label="vmoveA", broker="brokermove")
        other = CatalogueVersion.objects.create(label="vmoveB", status=CatalogueVersion.Status.DRAFT)
        self._freeze(v, CatalogueVersion.Status.ACTIVE)
        with self.assertRaises(Error):
            with transaction.atomic():
                CatalogueArtefact.objects.filter(pk=a.pk).update(version=other)
        a.refresh_from_db()
        self.assertEqual(a.version_id, v.pk)
