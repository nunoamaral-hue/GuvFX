"""§4-G — strictly READ-ONLY extraction of a privacy-safe, structure-preserving fixture from a genuine evidence blob.

Side-effect-free: it (1) verifies the evidence content hash, (2) parses the stored raw bytes with the deployed
header parser, (3) renders a PII-redacted, STRUCTURE-PRESERVING view (``fixture_extraction``), and (4) asserts that
NO BrokerEvent/Withdrawal/EvidenceBlob row changed and NO mailbox cursor advanced (counts + blob hash snapshotted
before and after). It NEVER ingests, NEVER writes to the DB, and NEVER commits anything to the repository.

OUTPUT SAFETY (R1-CLOSEOUT Blocker B). Automated redaction is best-effort, so the full rendering is SENSITIVE until a
human reviewer approves it. Therefore:
  * DEFAULT stdout/--json is a SAFE SUMMARY only — domains, auth verdict, mutation/integrity guards, and the COUNTS of
    residual candidates. It NEVER prints the (partially redacted) subject/body or any literal residual identifier, so
    ordinary operational logging of this command is safe.
  * The full detail is written ONLY to an explicitly requested PRIVATE ARTIFACT (``--out``): a path OUTSIDE the repo,
    the credential store, the evidence store and any static/media root; created owner-only (0600), O_EXCL (no
    overwrite) + O_NOFOLLOW (no symlink), written directly (no insecure temp file), never auto-staged or published,
    and confirmed with a log line carrying NO raw identifiers. A human must review that artifact before deriving any
    committed fixture.

Usage (read-only):
    python manage.py extract_sanitised_fixture --blob-id 1
    python manage.py extract_sanitised_fixture --blob-id 1 --out /private/review/tw_template.sanitised.json
Exit code 0 iff the hash verified AND nothing mutated (AND, with --out, the private artifact was written safely).
"""
from __future__ import annotations

import hashlib
import json
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("READ-ONLY extract a PII-redacted, structure-preserving fixture from a genuine evidence blob "
            "(safe summary by default; full detail only to a private --out artifact; no DB write, no auto-commit).")

    def add_arguments(self, parser):
        parser.add_argument("--blob-id", type=int, required=True, help="EvidenceBlob id to render a sanitised fixture from.")
        parser.add_argument("--min-account-digits", type=int, default=5,
                            help="Digit-run length at/above which a number is masked as an account/reference (default 5).")
        parser.add_argument("--out", default=None,
                            help="Private artifact path for the FULL detail (OUTSIDE repo/credential/evidence/static roots; "
                                 "created 0600, no overwrite, no symlink). The only place body + residual literals appear.")
        parser.add_argument("--json", action="store_true", help="Emit the SAFE SUMMARY as JSON (never body/residual literals).")

    # ---- private-artifact path safety (Blocker B) -------------------------------------------------------------------
    def _unsafe_roots(self):
        from django.conf import settings
        from broker_intelligence.evidence import evidence_root
        roots = []
        base = str(getattr(settings, "BASE_DIR", "") or "")
        if base:
            roots.append(os.path.dirname(base))            # the repository (BASE_DIR == <repo>/backend)
        try:
            roots.append(evidence_root())                  # immutable evidence store
        except Exception:  # noqa: BLE001
            pass
        cred = getattr(settings, "BROKER_INTELLIGENCE_CREDENTIAL_ROOT", None) or os.getenv("BROKER_INTELLIGENCE_CREDENTIAL_ROOT")
        for extra in (cred, getattr(settings, "STATIC_ROOT", None), getattr(settings, "MEDIA_ROOT", None)):
            if extra:
                roots.append(str(extra))
        return [os.path.realpath(r) for r in roots if r]

    def _validate_out(self, out_path):
        parent = os.path.dirname(os.path.abspath(out_path)) or "."
        if not os.path.isdir(parent):
            raise CommandError("--out parent directory does not exist (refusing to create one)")
        # realpath resolves symlinks in the (existing) parent chain, so a symlinked parent cannot smuggle the file
        # into an unsafe root.
        resolved = os.path.join(os.path.realpath(parent), os.path.basename(out_path))
        for root in self._unsafe_roots():
            if resolved == root or resolved.startswith(root + os.sep):
                raise CommandError("--out must be OUTSIDE the repo/credential/evidence/static roots (resolved under a protected root)")
        return resolved

    def _write_private(self, out_path, artifact):
        # Secure, direct write: owner-only, no overwrite (O_EXCL), no symlink (O_NOFOLLOW), no temp plaintext file.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(out_path, flags, 0o600)
        except FileExistsError:
            raise CommandError("--out target already exists (refusing to overwrite)")
        except OSError as e:
            raise CommandError("--out target could not be created safely (symlink or unsafe path): %s" % e.__class__.__name__)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fd, 0o600)                            # enforce 0600 regardless of umask
            fh.write("# SANITISED fixture (Provenance.SANITISED) — review residual_* before deriving a committed fixture.\n")
            fh.write(json.dumps(artifact, indent=2, default=str))

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox
        from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.fixture_extraction import extract_fixture

        out_path = o.get("out")
        resolved_out = self._validate_out(out_path) if out_path else None   # fail fast, before any read

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- BEFORE snapshot (mutation guards) ----
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_before = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_before = blob.sha256

        # ---- READ-ONLY: load raw (re-hashed; RAISES on tamper -> hash_ok=False), parse, render ----
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
            hash_ok = False

        # ---- AFTER snapshot ----
        after = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_after = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_after = EvidenceBlob.objects.get(id=blob.id).sha256
        no_mutation = before == after and cursors_before == cursors_after and sha_before == sha_after

        n_numeric = len(fixture["residual_numeric_runs"]) if fixture else 0
        n_token = len(fixture["residual_token_candidates"]) if fixture else 0
        header = fixture["header"] if fixture else {}

        # ---- SAFE SUMMARY (default stdout / --json): domains + counts + guards ONLY; never body/residual literals ----
        summary = {
            "blob_id": blob.id, "evidence_hash_verified": hash_ok, "no_mutation": no_mutation,
            "provenance_tier": "SANITISED",
            "from_domain": header.get("from_domain", ""), "to_domains": header.get("to_domains", []),
            "auth_verdict": header.get("auth_verdict", ""),
            "residual_numeric_run_count": n_numeric, "residual_token_candidate_count": n_token,
        }
        if o["json"]:
            self.stdout.write(json.dumps(summary, default=str))
        else:
            w = self.stdout.write
            w("=== SANITISED FIXTURE SUMMARY (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
            w("evidence_hash_verified=%s  provenance_tier=SANITISED" % hash_ok)
            if fixture is not None:
                w("from_domain=%s  to_domains=%s  auth_verdict=%s"
                  % (summary["from_domain"], summary["to_domains"], summary["auth_verdict"]))
                w("residual_numeric_runs=%d  residual_token_candidates=%d  (DETAIL only via --out private artifact)"
                  % (n_numeric, n_token))
            else:
                w("INTEGRITY CHECK FAILED: stored evidence does not match its recorded hash (no fixture rendered).")
            w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
              % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))

        # Guard FIRST: a failed read-only guarantee prevents any --out write.
        if not (hash_ok and no_mutation):
            raise CommandError("extraction FAILED read-only guarantees (hash_ok=%s no_mutation=%s)" % (hash_ok, no_mutation))

        if resolved_out and fixture is not None:
            artifact = dict(fixture, blob_id=blob.id, evidence_hash_verified=hash_ok, no_mutation=no_mutation,
                            provenance_tier="SANITISED")
            self._write_private(resolved_out, artifact)
            # Confirmation carries NO raw identifiers — only the path + counts.
            self.stdout.write("wrote private sanitised fixture to %s (0600; %d numeric + %d token residual candidates "
                              "to review; review before use; NOT auto-committed)" % (resolved_out, n_numeric, n_token))
