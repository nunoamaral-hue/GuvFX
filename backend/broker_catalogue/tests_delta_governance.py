"""ADR-0102 (Lane C) — governed catalogue-delta workflow tests: hash-pinned propose, dry-run validate, approve,
stage, activate (delegating to the proven gates), reject, rollback, append-only audit, deterministic checksum,
structural validation, and the adversarial matrix. SYNTHETIC only; no host write; no servers.dat bytes.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from approvals.models import ArtefactApproval
from broker_catalogue import delta_governance as dg
from broker_catalogue import sanitiser as SAN
from broker_catalogue import service as svc
from broker_catalogue.models import (CatalogueArtefact, CatalogueDelta, CatalogueDeltaAudit, CatalogueVersion)

_HA, _HB, _HC, _HD = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def _sanv(sha, passed=True):
    v = {"passed": passed, "version": "sanitiser_v1", "size_bytes": 0, "sha256": str(sha).lower(),
         "identity_hits": [] if passed else [{"term_masked": "x***", "encoding": "ascii"}],
         "entropy": {"windows": 0, "max_bits_per_byte": 0.0, "windows_over_7_5": 0}}
    v["evidence_sha256"] = SAN.recompute_evidence_sha(v)
    return v


def _draft(label, brokers):
    v = CatalogueVersion.objects.create(label=label, status=CatalogueVersion.Status.DRAFT)
    for broker, sha, servers, size in brokers:
        CatalogueArtefact.objects.create(
            version=v, broker_id=broker, display_name=broker.title(), servers=servers,
            artefact_ref=f"{broker}/{label}", sha256=sha, size_bytes=size,
            host_relpath=f"versions/{label}/{broker}/servers.dat", sanitisation_result="PASS")
    return v


def _approve_full(broker, label, sha, servers, size, passed=True):
    ArtefactApproval.objects.create(
        artefact_kind="broker_servers_dat", artefact_ref=f"{broker}/{label}", sha256=sha,
        status=ArtefactApproval.Status.APPROVED,
        metadata={"broker": broker.title(), "servers_intended": servers, "size_bytes": size,
                  "sanitiser": _sanv(sha, passed)})


def _mk(label, brokers, *, activate):
    v = _draft(label, brokers)
    for broker, sha, servers, size in brokers:
        _approve_full(broker, label, sha, servers, size)
    if activate:
        call_command("activate_catalogue_version", "--label", label)
        v.refresh_from_db()
    return v


PEP = ("pepperstone", _HA, ["PepperstoneUK-Demo", "PepperstoneUK-Live"], 100)
IS6 = ("is6", _HB, ["IS6Technologies-Demo", "IS6Technologies-Live"], 200)
NEW = ("acme", _HC, ["Acme-Demo", "Acme-Live"], 300)


@override_settings(APPROVALS_ENABLED="1")
class ProposeAndValidate(TestCase):
    def test_additive_delta_proposed(self):
        _mk("v1", [PEP, IS6], activate=True)
        _mk("v2", [PEP, IS6, NEW], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1", reason="add acme")
        self.assertEqual(d.state, "DRAFT")
        self.assertEqual([x["broker_id"] for x in d.added_identities], ["acme"])
        self.assertEqual(d.removed_identities, [])
        self.assertEqual(d.changed_identities, [])
        self.assertTrue(d.delta_sha256 and d.in_flight)
        self.assertTrue(CatalogueDeltaAudit.objects.filter(delta=d, action="PROPOSE").exists())

    def test_removed_and_changed_detected(self):
        _mk("v1", [PEP, IS6], activate=True)
        # v2 drops IS6 and changes pepperstone's servers
        _mk("v2", [("pepperstone", _HD, ["PepperstoneUK-Demo"], 101)], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        self.assertEqual([x["broker_id"] for x in d.removed_identities], ["is6"])
        self.assertEqual([x["broker_id"] for x in d.changed_identities], ["pepperstone"])

    def test_no_op_delta_refused(self):
        _mk("v1", [PEP, IS6], activate=True)
        _mk("v2", [PEP, IS6], activate=False)   # identical content
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_stale_base_hash_refused(self):
        _mk("v1", [PEP, IS6], activate=True)
        _mk("v2", [PEP, IS6, NEW], activate=False)
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1",
                             expected_base_manifest="f" * 64)   # wrong pin

    def test_ambiguous_duplicate_server_refused(self):
        _mk("v1", [PEP], activate=True)
        # two brokers claim the same normalised server name
        _draft("v2", [("pepperstone", _HA, ["PepperstoneUK-Demo"], 100),
                      ("pepperclone", _HB, ["pepperstoneuk-demo"], 100)])
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_conflicting_broker_ownership_refused(self):
        _mk("v1", [PEP], activate=True)
        _draft("v2", [("taurex", _HC, ["PepperstoneUK-Demo"], 100)])   # taurex claiming a pepperstone server
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_malformed_artefact_refused(self):
        _mk("v1", [PEP], activate=True)
        v2 = _draft("v2", [("acme", _HC, ["Acme-Demo"], 100)])
        art = v2.artefacts.get(broker_id="acme")
        art.servers = ["Acme-Demo\x01evil"]; art.save()   # control char
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_bidi_zero_width_server_name_refused(self):   # review privacy Finding A
        _mk("v1", [PEP], activate=True)
        v2 = _draft("v2", [("acme", _HC, ["Acme-Demo"], 100)])
        art = v2.artefacts.get(broker_id="acme")
        art.servers = ["Acme-Demo‮EVIL"]; art.save()   # right-to-left override
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_traversal_host_relpath_refused(self):   # review privacy Finding B
        _mk("v1", [PEP], activate=True)
        v2 = _draft("v2", [("acme", _HC, ["Acme-Demo"], 100)])
        v2.artefacts.filter(broker_id="acme").update(host_relpath="../../../../etc/passwd/servers.dat")
        with self.assertRaises(ValidationError):
            dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_one_in_flight_delta_db_enforced(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        _mk("v3", [PEP, IS6], activate=False)
        with self.assertRaises(ValidationError):   # service refuses a 2nd in-flight
            dg.propose_delta(proposed_label="v3", actor="user:1", ticket="T-1")

    def test_validate_dry_run_then_review(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        rep = dg.validate_delta(d, actor="user:1", persist=False)
        self.assertTrue(rep["valid"] and rep["problems"] == [])
        self.assertEqual(CatalogueDelta.objects.get(pk=d.pk).state, "DRAFT")   # dry-run: no mutation
        dg.validate_delta(d, actor="user:2", persist=True)
        self.assertEqual(CatalogueDelta.objects.get(pk=d.pk).state, "REVIEWED")

    def test_deterministic_checksum(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        self.assertEqual(d.compute_delta_sha256(), d.delta_sha256)
        self.assertEqual(dg.staging_artifact_descriptor(d)["descriptor_sha256"],
                         dg.staging_artifact_descriptor(d)["descriptor_sha256"])   # deterministic


@override_settings(APPROVALS_ENABLED="1")
class ApproveStageActivateRollback(TestCase):
    def _reviewed(self):
        _mk("v1", [PEP, IS6], activate=True)
        _mk("v2", [PEP, IS6, NEW], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1", reason="add acme")
        dg.validate_delta(d, actor="user:2", persist=True)
        return CatalogueDelta.objects.get(pk=d.pk)

    def test_approve_does_not_activate(self):
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        self.assertEqual(d.state, "APPROVED")
        self.assertEqual(svc.resolve_active_version().label, "v1")   # NOT activated
        self.assertTrue(CatalogueDeltaAudit.objects.filter(delta=d, action="APPROVE").exists())

    def test_stage_emits_deterministic_descriptor(self):
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        desc = dg.stage_delta(d, actor="user:3")
        self.assertEqual(len(desc["descriptor_sha256"]), 64)
        self.assertEqual(CatalogueDelta.objects.get(pk=d.pk).state, "STAGED")

    def test_full_activate_flips_version_and_rollback_restores(self):
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        d = dg.activate_delta(d, actor="user:3")
        self.assertEqual(d.state, "ACTIVATED")
        self.assertFalse(d.in_flight)
        self.assertEqual(svc.resolve_active_version().label, "v2")   # proposed is now ACTIVE
        # rollback restores the base
        d = dg.rollback_delta(d, actor="user:3", ticket="T-3", reason="revert")
        self.assertEqual(d.state, "ROLLED_BACK")
        self.assertEqual(svc.resolve_active_version().label, "v1")

    def test_double_activation_refused(self):
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        dg.activate_delta(d, actor="user:3")
        with self.assertRaises(ValidationError):
            dg.activate_delta(CatalogueDelta.objects.get(pk=d.pk), actor="user:3")

    def test_activate_requires_staged(self):
        with self.assertRaises(ValidationError):   # REVIEWED, not STAGED
            dg.activate_delta(self._reviewed(), actor="user:3")

    def test_unapproved_proposed_activation_fails_closed(self):
        # proposed version whose NEW broker has NO ArtefactApproval → the real activation gate refuses → STAGED stays
        _mk("v1", [PEP], activate=True)
        _draft("v2", [PEP, NEW])              # artefacts but NO approval for NEW
        _approve_full("pepperstone", "v2", _HA, PEP[2], PEP[3])   # only pepperstone approved
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        dg.validate_delta(d, actor="user:2", persist=True)
        dg.approve_delta(d, actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        with self.assertRaises((ValidationError, CommandError)):
            dg.activate_delta(CatalogueDelta.objects.get(pk=d.pk), actor="user:3")
        self.assertEqual(svc.resolve_active_version().label, "v1")   # base preserved
        self.assertEqual(CatalogueDelta.objects.get(pk=d.pk).state, "STAGED")   # not activated

    def test_stale_base_blocks_activation_cas(self):
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        # another version becomes ACTIVE out-of-band (base no longer matches the pin)
        _mk("v9", [PEP, IS6, ("beta", _HD, ["Beta-Demo"], 50)], activate=False)
        CatalogueVersion.objects.filter(label="v1").update(status="RETIRED")
        call_command("activate_catalogue_version", "--label", "v9")
        with self.assertRaises(ValidationError):
            dg.activate_delta(CatalogueDelta.objects.get(pk=d.pk), actor="user:3")

    def test_reject_terminalises(self):
        d = dg.reject_delta(self._reviewed(), actor="user:3", reason="bad")
        self.assertEqual(d.state, "REJECTED")
        self.assertFalse(d.in_flight)
        # a new delta can now be proposed (in-flight freed)
        d2 = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-9")
        self.assertEqual(d2.state, "DRAFT")

    def test_approver_must_differ_from_proposer(self):   # review Finding D (SoD)
        d = self._reviewed()   # proposer = user:1
        with self.assertRaises(ValidationError):
            dg.approve_delta(d, actor="user:1", ticket="T-2", reason="self-approve")

    def test_mutated_proposal_after_approval_blocks_activation(self):   # review Finding A (HIGH)
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        # smuggle a SECOND broker into the still-DRAFT proposed version AFTER approval (+ its own approval)
        prop = CatalogueVersion.objects.get(label="v2")
        CatalogueArtefact.objects.create(
            version=prop, broker_id="beta", display_name="Beta", servers=["Beta-Demo"],
            artefact_ref="beta/v2", sha256=_HD, size_bytes=50, host_relpath="versions/v2/beta/servers.dat",
            sanitisation_result="PASS")
        _approve_full("beta", "v2", _HD, ["Beta-Demo"], 50)
        with self.assertRaises(ValidationError):   # re-pin: proposed content changed since propose
            dg.activate_delta(CatalogueDelta.objects.get(pk=d.pk), actor="user:3")
        self.assertEqual(svc.resolve_active_version().label, "v1")   # base preserved; unreviewed set NOT shipped

    def test_rollback_refused_when_proposed_no_longer_active(self):   # review Finding B
        d = dg.approve_delta(self._reviewed(), actor="user:3", ticket="T-2", reason="ok")
        dg.stage_delta(d, actor="user:3")
        d = dg.activate_delta(d, actor="user:3")   # v2 now ACTIVE
        # a newer version is activated out-of-band (v2 retires)
        _mk("v3", [PEP, IS6, NEW, ("gamma", _HD, ["Gamma-Demo"], 40)], activate=True)
        self.assertEqual(svc.resolve_active_version().label, "v3")
        with self.assertRaises(ValidationError):   # must not undo v3's newer activation
            dg.rollback_delta(CatalogueDelta.objects.get(pk=d.pk), actor="user:3", ticket="T-3", reason="late")
        self.assertEqual(svc.resolve_active_version().label, "v3")   # preserved


@override_settings(APPROVALS_ENABLED="1")
class AuditAndPrivacy(TestCase):
    def _delta(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        return dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")

    def test_audit_append_only_model_guard(self):
        d = self._delta()
        a = CatalogueDeltaAudit.objects.filter(delta=d).first()
        a.reason = "x"
        with self.assertRaises(ValidationError):
            a.save()
        with self.assertRaises(ValidationError):
            a.delete()

    def test_audit_append_only_db_trigger(self):
        d = self._delta()
        a = CatalogueDeltaAudit.objects.filter(delta=d).first()
        with self.assertRaises(Exception):
            with transaction.atomic():
                CatalogueDeltaAudit.objects.filter(pk=a.pk).update(actor="rewritten")
        with self.assertRaises(Exception):
            with transaction.atomic():
                CatalogueDeltaAudit.objects.filter(pk=a.pk).delete()

    def test_report_is_privacy_safe(self):
        import json
        d = self._delta()
        blob = json.dumps(dg.delta_report(d))
        # only identities, hashes, enums, counts — no credential/account markers
        self.assertNotIn("accounts.dat", blob)
        self.assertNotIn("password", blob.lower())
        self.assertIn("Acme-Demo", blob)   # server identity (public) IS present
        self.assertIn("added", blob)

    def test_existing_base_artefacts_unchanged_by_propose(self):
        self._delta()
        base = CatalogueVersion.objects.get(label="v1")
        pep = base.artefacts.get(broker_id="pepperstone")
        self.assertEqual(sorted(pep.servers), sorted(PEP[2]))   # base untouched
        self.assertEqual(base.status, "ACTIVE")


@override_settings(APPROVALS_ENABLED="1")
class CommandSurface(TestCase):
    def _run(self, *args):
        from io import StringIO
        out = StringIO()
        call_command("catalogue_delta", *args, stdout=out, stderr=StringIO())
        import json
        return json.loads(out.getvalue())

    def test_parser_builds_no_django_collision(self):
        from broker_catalogue.management.commands.catalogue_delta import Command
        p = Command().create_parser("manage.py", "catalogue_delta")
        opts = {o for a in p._actions for o in a.option_strings}
        self.assertIn("--version", opts)          # Django built-in preserved (Lane-A lesson: no custom --version)
        self.assertIn("--proposed-label", opts)

    def test_inspect_readonly(self):
        _mk("v1", [PEP], activate=True)
        res = self._run("inspect")
        self.assertEqual(res["current"]["active_version"], "v1")

    def test_propose_dry_run_creates_nothing_then_apply_creates(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        dry = self._run("propose", "--proposed-label", "v2")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["would_propose"]["counts"]["added"], 1)
        self.assertEqual(CatalogueDelta.objects.count(), 0)   # dry-run created nothing
        applied = self._run("propose", "--proposed-label", "v2", "--actor", "user:1", "--ticket", "T-1", "--apply")
        self.assertEqual(applied["proposed"]["state"], "DRAFT")
        self.assertEqual(CatalogueDelta.objects.count(), 1)

    def test_mutating_action_requires_apply(self):
        _mk("v1", [PEP], activate=True)
        _mk("v2", [PEP, NEW], activate=False)
        d = dg.propose_delta(proposed_label="v2", actor="user:1", ticket="T-1")
        dry = self._run("approve", "--delta-id", str(d.pk), "--actor", "user:3", "--ticket", "T", "--reason", "r")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(CatalogueDelta.objects.get(pk=d.pk).state, "DRAFT")   # not approved (dry-run)
