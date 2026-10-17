"""§4-G — strictly READ-ONLY extraction of a privacy-safe, structure-preserving fixture from a genuine evidence blob.

Side-effect-free: it (1) verifies the evidence content hash, (2) parses the stored raw bytes with the deployed
header parser, (3) renders a PII-redacted, STRUCTURE-PRESERVING view (``fixture_extraction``), and (4) asserts that
NO BrokerEvent/Withdrawal/EvidenceBlob row changed and NO mailbox cursor advanced (counts + blob hash snapshotted
before and after). It NEVER ingests, NEVER writes to the DB, and NEVER commits anything to the repository.

The output is a REVIEW AID for a human preparing a committed fixture — names, e-mail addresses, account/reference
numbers, confirmation URLs and authorization tokens are masked; labels, layout and amount *shape* are preserved. It
also lists any residual numeric runs it left, so the reviewer can finish the redaction. A fixture derived this way is
the ``Provenance.SANITISED`` tier; it must NEVER be re-labelled REAL or committed with un-reviewed residue.

Usage (read-only):
    python manage.py extract_sanitised_fixture --blob-id 1
    python manage.py extract_sanitised_fixture --blob-id 1 --out /path/outside/repo/tradersway_template.sanitised.txt
Exit code 0 iff the hash verified AND nothing mutated.
"""
from __future__ import annotations

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("READ-ONLY extract a PII-redacted, structure-preserving fixture from a genuine evidence blob "
            "(no ingestion, no DB write, no auto-commit).")

    def add_arguments(self, parser):
        parser.add_argument("--blob-id", type=int, required=True, help="EvidenceBlob id to render a sanitised fixture from.")
        parser.add_argument("--min-account-digits", type=int, default=5,
                            help="Digit-run length at/above which a number is masked as an account/reference (default 5).")
        parser.add_argument("--out", default=None,
                            help="Optional path to write the sanitised fixture to (OUTSIDE the repo). Never auto-committed.")
        parser.add_argument("--json", action="store_true", help="Emit the sanitised fixture as a single JSON object.")

    def handle(self, *args, **o):
        import os
        from django.conf import settings
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox
        from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.fixture_extraction import extract_fixture

        # Reject an --out path resolving INSIDE the repository: a sanitised fixture is reviewed + hand-finished before
        # it is ever committed; writing it into the repo tree makes it trivially git-addable with un-reviewed residue.
        out_path = o.get("out")
        if out_path:
            repo_root = os.path.realpath(os.path.dirname(str(settings.BASE_DIR)))   # BASE_DIR == <repo>/backend
            resolved = os.path.realpath(out_path)
            if resolved == repo_root or resolved.startswith(repo_root + os.sep):
                raise CommandError("--out must resolve OUTSIDE the repository (%s is inside %s)" % (resolved, repo_root))

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- BEFORE snapshot (mutation guards) ----
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_before = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_before = blob.sha256

        # ---- READ-ONLY: load raw (EvidenceStore.read RE-HASHES and RAISES on tamper), parse, render. A tamper is a
        #      REAL failure of the integrity check, reported as hash_ok=False rather than an uncaught traceback. ----
        fixture = None
        f = {}
        try:
            raw = EvidenceStore().read(blob)
            hash_ok = hashlib.sha256(raw).hexdigest() == sha_before
            f = parse_rfc822(raw)
            fixture = extract_fixture(
                from_address=f.get("from_address", ""), to_addresses=f.get("to_addresses", ()),
                subject=f.get("subject", ""), body=f.get("body", ""),
                date=str(f.get("received_at", "") or ""), auth_verdict=f.get("auth_verdict", ""),
                min_account_digits=o["min_account_digits"])
        except EvidenceStoreError:
            hash_ok = False                        # stored bytes no longer match the recorded hash (tamper/corruption)

        # ---- AFTER snapshot ----
        after = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_after = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_after = EvidenceBlob.objects.get(id=blob.id).sha256
        no_mutation = before == after and cursors_before == cursors_after and sha_before == sha_after

        if o["json"]:
            payload = dict(fixture or {}, blob_id=blob.id, evidence_hash_verified=hash_ok, no_mutation=no_mutation,
                           provenance_tier="SANITISED")
            self.stdout.write(json.dumps(payload, default=str))
        else:
            w = self.stdout.write
            w("=== SANITISED FIXTURE (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
            w("evidence_hash_verified=%s  provenance_tier=SANITISED" % hash_ok)
            if fixture is not None:
                h = fixture["header"]
                w("--- header (addresses -> domain only; subject sanitised) ---")
                w("  from_domain=%s" % h["from_domain"])
                w("  to_domains=%s" % h["to_domains"])
                w("  subject=%r" % h["subject"])
                w("  date=%s  auth_verdict=%s" % (h["date"], h["auth_verdict"]))
                w("--- body (PII/identifiers masked; structure/amount-shape preserved) ---")
                w(fixture["body"])
                if fixture["residual_numeric_runs"]:
                    w("--- RESIDUAL NUMERIC RUNS (review: any sensitive identifier left, incl. decimal-glued) ---")
                    w("  %s" % fixture["residual_numeric_runs"])
                if fixture["residual_token_candidates"]:
                    w("--- RESIDUAL TOKEN CANDIDATES (review: short mixed-alnum codes redact() may miss) ---")
                    w("  %s" % fixture["residual_token_candidates"])
            else:
                w("INTEGRITY CHECK FAILED: stored evidence does not match its recorded hash (no fixture rendered).")
            w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
              % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))

        # Guard FIRST: a failed read-only guarantee must prevent any --out write.
        if not (hash_ok and no_mutation):
            raise CommandError("extraction FAILED read-only guarantees (hash_ok=%s no_mutation=%s)" % (hash_ok, no_mutation))

        if out_path and fixture is not None:
            # Write the sanitised rendering to a human-chosen review path OUTSIDE the repo (never the DB, never
            # auto-committed). Embed integrity markers so an on-disk artifact carries its provenance/verification.
            artifact = dict(fixture, blob_id=blob.id, evidence_hash_verified=hash_ok, no_mutation=no_mutation,
                            provenance_tier="SANITISED")
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("# SANITISED fixture (Provenance.SANITISED) — review residual_* before committing.\n")
                fh.write(json.dumps(artifact, indent=2, default=str))
            self.stdout.write("wrote sanitised fixture to %s (review before use; NOT auto-committed)" % out_path)
