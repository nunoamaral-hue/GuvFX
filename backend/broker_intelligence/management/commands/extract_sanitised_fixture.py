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
        parser.add_argument("--owner-id", type=int, default=None,
                            help="Explicit owner (GuvFX user id) whose known name is used as a redaction input. Only "
                                 "needed when the blob's owner cannot be resolved unambiguously; never prints the name.")

    # ---- private-artifact path safety (Blocker B) -------------------------------------------------------------------
    def _unsafe_roots(self):
        from django.conf import settings
        from broker_intelligence.evidence import evidence_root
        roots = []
        base = str(getattr(settings, "BASE_DIR", "") or "")
        if base:
            # The REPOSITORY root. In a dev checkout BASE_DIR == <repo>/backend, so the repository is its parent.
            # In the deployed container BASE_DIR IS the app root (e.g. /app) whose parent is the filesystem root '/';
            # protecting '/' would mark the ENTIRE filesystem protected and reject every private --out path. When the
            # parent resolves to the filesystem root, protect BASE_DIR ITSELF (still fully protecting the repository)
            # rather than '/'. The evidence / credential / static / media roots below stay protected independently —
            # this NARROWS an over-broad guard; it never broadens access to any sensitive root. Realpath first so a
            # symlinked BASE_DIR is compared by its canonical location.
            base_real = os.path.realpath(base)
            parent = os.path.dirname(base_real)
            roots.append(base_real if (parent in ("", os.sep) or parent == base_real) else parent)
        try:
            roots.append(evidence_root())                  # immutable evidence store
        except Exception:  # noqa: BLE001
            pass
        cred = getattr(settings, "BROKER_INTELLIGENCE_CREDENTIAL_ROOT", None) or os.getenv("BROKER_INTELLIGENCE_CREDENTIAL_ROOT")
        for extra in (cred, getattr(settings, "STATIC_ROOT", None), getattr(settings, "MEDIA_ROOT", None)):
            if extra:
                roots.append(str(extra))
        return [os.path.realpath(r) for r in roots if r]

    def _within_protected_root(self, parent_realpath):
        """True if the (realpath'd) parent dir IS, or is nested under, any protected root. Compared by device+inode
        (os.path.samestat) walking up the parent chain — robust to a CASE-INSENSITIVE filesystem (where a wrong-case
        path is the same physical dir but a string startswith misses) and to symlinks, which a string compare is not."""
        root_stats = []
        for r in self._unsafe_roots():
            try:
                root_stats.append(os.stat(r))
            except OSError:
                pass
        cur = parent_realpath
        while True:
            try:
                st = os.stat(cur)
            except OSError:
                return False
            if any(os.path.samestat(st, rs) for rs in root_stats):
                return True
            parent = os.path.dirname(cur)
            if parent == cur:
                return False
            cur = parent

    def _validate_out(self, out_path):
        parent = os.path.dirname(os.path.abspath(out_path)) or "."
        if not os.path.isdir(parent):
            raise CommandError("--out parent directory does not exist (refusing to create one)")
        parent_real = os.path.realpath(parent)                 # resolves symlinks in the whole existing parent chain
        if self._within_protected_root(parent_real):
            raise CommandError("--out must be OUTSIDE the repo/credential/evidence/static roots (resolved under a protected root)")
        return os.path.join(parent_real, os.path.basename(out_path))

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

    # ---- owner-identity resolution (name is a REDACTION INPUT only; never printed/returned) --------------------------
    def _resolve_owner_names(self, blob, owner_id):
        """Return (name_strings, owner_source) for the blob's owner — the GuvFX-known name used ONLY to build name
        masks. Resolution order: explicit --owner-id; the blob's own BrokerEvent owner; the blob's capturing mailbox
        owner; else the single distinct connected-mailbox owner. Ambiguous (several distinct owners) or unavailable ->
        ([], reason): the caller FAILS CLOSED. Never emits the name itself — only the owner id and a source label."""
        from django.contrib.auth import get_user_model
        from broker_intelligence.models import BrokerEvent, ConnectedMailbox
        User = get_user_model()

        def _names_for(uid):
            u = User.objects.filter(id=uid).first()
            if u is None:
                return None
            vals = [getattr(u, "first_name", ""), getattr(u, "last_name", ""),
                    (u.get_full_name() if hasattr(u, "get_full_name") else "")]
            return [v for v in vals if (v or "").strip()]

        if owner_id is not None:
            names = _names_for(owner_id)
            if not names:
                return [], "explicit owner id has no usable name"
            return names, "explicit --owner-id"

        candidates = set(BrokerEvent.objects.filter(evidence_id=blob.id)
                         .exclude(owner__isnull=True).values_list("owner_id", flat=True))
        src = "blob BrokerEvent owner"
        if not candidates:
            try:
                candidates = set(obs.connected_mailbox.user_id for obs in blob.message_observations.all()
                                 if getattr(obs, "connected_mailbox", None) is not None)
                src = "blob capturing mailbox owner"
            except Exception:  # noqa: BLE001
                candidates = set()
        if not candidates:
            candidates = set(ConnectedMailbox.objects.exclude(user__isnull=True).values_list("user_id", flat=True))
            src = "single connected-mailbox owner"
        candidates.discard(None)
        if len(candidates) != 1:
            return [], ("ambiguous owner (%d candidates) — pass --owner-id" % len(candidates)) if candidates \
                else "owner identity unavailable"
        names = _names_for(next(iter(candidates)))
        if not names:
            return [], "resolved owner has no usable name in GuvFX"
        return names, src

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox
        from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.fixture_extraction import extract_fixture, validate_pii_clean

        out_path = o.get("out")
        resolved_out = self._validate_out(out_path) if out_path else None   # fail fast, before any read

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- OWNER IDENTITY as a redaction input (never printed). Packet 008: the owner's known name is masked so the
        # member is not leaked. If it cannot be resolved unambiguously, FAIL CLOSED before any read -> no fixture. ----
        known_names, owner_source = self._resolve_owner_names(blob, o.get("owner_id"))
        if not known_names:
            raise CommandError("owner identity for name redaction is unavailable/ambiguous (%s); refusing to extract"
                               % owner_source)

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
                known_names=known_names, min_account_digits=o["min_account_digits"])
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

        # ---- PII ACCEPTANCE GATE (packet 008): prove no personal name/email/number/token/phone survived, or fail
        # closed. Reports CATEGORY COUNTS ONLY — never a literal value. ----
        pii = (validate_pii_clean(subject=header.get("subject", ""), body=fixture["body"], known_names=known_names,
                                  min_account_digits=o["min_account_digits"])
               if fixture is not None else {"ok": False, "counts": {}})

        # ---- SAFE SUMMARY (default stdout / --json): domains + counts + guards ONLY; never body/residual literals ----
        # The recipient (member) mailbox domain is deliberately NOT in the default summary — on a vanity/corporate
        # domain it would identify the member in ordinary logs. Only the broker's from_domain (safe) is shown; the
        # full header (incl. to_domains) lives only in the private --out artifact.
        summary = {
            "blob_id": blob.id, "evidence_hash_verified": hash_ok, "no_mutation": no_mutation,
            "provenance_tier": "SANITISED",
            "from_domain": header.get("from_domain", ""),
            "auth_verdict": header.get("auth_verdict", ""),
            "owner_name_source": owner_source,
            "name_tokens_masked_count": fixture["name_tokens_masked_count"] if fixture else 0,
            "pii_clean": pii["ok"], "pii_residual_counts": pii["counts"],
            "residual_numeric_run_count": n_numeric, "residual_token_candidate_count": n_token,
        }
        if o["json"]:
            self.stdout.write(json.dumps(summary, default=str))
        else:
            w = self.stdout.write
            w("=== SANITISED FIXTURE SUMMARY (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
            w("evidence_hash_verified=%s  provenance_tier=SANITISED" % hash_ok)
            if fixture is not None:
                w("from_domain=%s  auth_verdict=%s  owner_name_source=%s  name_tokens_masked=%d"
                  % (summary["from_domain"], summary["auth_verdict"], owner_source, summary["name_tokens_masked_count"]))
                w("PII GATE: clean=%s  residual_counts=%s" % (pii["ok"], pii["counts"]))
                w("residual_numeric_runs=%d  residual_token_candidates=%d  (DETAIL only via --out private artifact)"
                  % (n_numeric, n_token))
            else:
                w("INTEGRITY CHECK FAILED: stored evidence does not match its recorded hash (no fixture rendered).")
            w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
              % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))

        # Guard FIRST: a failed read-only guarantee prevents any --out write.
        if not (hash_ok and no_mutation):
            raise CommandError("extraction FAILED read-only guarantees (hash_ok=%s no_mutation=%s)" % (hash_ok, no_mutation))
        # Then the PII gate: never write a fixture that still carries personal data (categories only in the message).
        if not pii["ok"]:
            raise CommandError("extraction FAILED PII validation — residual categories %s; refusing to write fixture"
                               % {k: v for k, v in pii["counts"].items() if v})

        if resolved_out and fixture is not None:
            artifact = dict(fixture, blob_id=blob.id, evidence_hash_verified=hash_ok, no_mutation=no_mutation,
                            provenance_tier="SANITISED", owner_name_source=owner_source,
                            pii_validation={"clean": pii["ok"], "counts": pii["counts"]})
            self._write_private(resolved_out, artifact)
            # Confirmation carries NO raw identifiers — only the path + counts.
            self.stdout.write("wrote private sanitised fixture to %s (0600; %d numeric + %d token residual candidates "
                              "to review; review before use; NOT auto-committed)" % (resolved_out, n_numeric, n_token))
