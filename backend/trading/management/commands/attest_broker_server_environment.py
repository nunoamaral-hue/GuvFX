"""Governed operator attestation of a broker server's verified demo/live environment (GFX-DOMINION-SERVER-ATTESTATION-001).

The authoritative source for a server's demo/live classification is the staff-curated ``BrokerServerCatalogue``
(``trading.server_environment.resolve_verified_environment``). Entries were previously only ever added by data
migrations (0020 pilots, 0021 Vantage); there is no runtime primitive to record a NEW operator attestation, nor to
reconcile the mirrored ``BrokerServer`` row from an unverified ``inferred`` classification to an authoritative one.
This command IS that governed, audited, idempotent primitive (the Sponsor-directed replacement for an ad-hoc DB edit).

Given a server name + environment + operator attestation evidence it, in ONE transaction:
  1. Upserts the ``BrokerServerCatalogue`` entry (additive — ``update_or_create`` on the casefolded ``name_key``;
     never removes or alters any other entry) so the server resolves as the VERIFIED environment.
  2. Reconciles ONLY the exactly-named ``BrokerServer`` row(s) to ``environment=<env>, environment_source=operator``
     (the model's supported, validated save path — never raw SQL, never a hand-edited column).

It FAILS CLOSED before any write when authoritative evidence contradicts the attestation (an existing verified
catalogue entry, or a BrokerServer row already stamped ``operator``/``catalogue``, that disagrees with ``--environment``).
It NEVER touches any other server, NEVER touches any account/workspace/provisioning/entitlement, NEVER performs a
broker login, and NEVER records a credential. Dry-run by default; ``--apply`` to execute. Idempotent: re-running on an
already-consistent server is a no-op. Rollback evidence (the exact before-state) is printed on every apply.

Usage:
  manage.py attest_broker_server_environment --server "DominionMarkets-Live" --environment live \
      --evidence "Sponsor-attested broker-issued account evidence dated 2026-11-04 (GFX-DOMINION-SERVER-ATTESTATION-001)"            # dry-run
  manage.py attest_broker_server_environment --server "DominionMarkets-Live" --environment live --evidence "..." --apply
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = ("Governed operator attestation of a broker server's verified demo/live environment: upsert the "
            "authoritative BrokerServerCatalogue entry + reconcile the mirrored BrokerServer row(s). Dry-run by default.")

    def add_arguments(self, parser):
        parser.add_argument("--server", required=True, help="Canonical broker server name as the broker publishes it.")
        parser.add_argument("--environment", required=True, choices=["demo", "live"],
                            help="The operator-verified environment for this server.")
        parser.add_argument("--evidence", required=True,
                            help="Operator attestation evidence (provenance). No credentials/logins.")
        parser.add_argument("--display", default="", help="Optional broker display name for the catalogue entry.")
        parser.add_argument("--apply", action="store_true",
                            help="Apply the change. Without this flag the command only reports (dry-run).")

    def handle(self, *args, **opts):
        from trading.models import BrokerServer, BrokerServerCatalogue
        from trading.server_environment import (normalise_environment, normalise_server_name, server_name_key,
                                                resolve_verified_environment)

        raw_server = opts["server"]
        server = normalise_server_name(raw_server)
        env = normalise_environment(opts["environment"])
        if env is None:
            raise CommandError(f"--environment must be demo/live, got {opts['environment']!r}")
        evidence = (opts["evidence"] or "").strip()
        if not evidence:
            raise CommandError("--evidence is required (operator attestation provenance).")
        # Evidence is persisted (catalogue + audit) and must carry NO credential. Reject obvious secret keywords
        # (defence-in-depth over operator discipline; the audit sanitiser redacts by key name, not by value).
        if any(tok in evidence.lower() for tok in ("password", "passwd", "pwd", "secret", "token", "api_key", "apikey")):
            raise CommandError("REFUSED: --evidence appears to contain a credential keyword. Evidence must be a "
                               "non-sensitive provenance note (no passwords/tokens/logins).")
        display = (opts["display"] or "").strip()
        apply = bool(opts["apply"])
        key = server_name_key(server)

        # ---- read current state -------------------------------------------------------------------------------
        existing_cat = BrokerServerCatalogue.objects.filter(name_key=key).first()
        rows = list(BrokerServer.objects.filter(server_name__iexact=server))
        verified_env, verified_src = resolve_verified_environment(server)

        before = {
            "server": server,
            "requested_environment": env,
            "catalogue_entry": (None if existing_cat is None else
                                {"server_name": existing_cat.server_name, "environment": existing_cat.environment,
                                 "is_verified": existing_cat.is_verified,
                                 "broker_display_name": existing_cat.broker_display_name,
                                 "evidence": existing_cat.evidence}),
            "resolved_verified_environment": verified_env,
            "broker_server_rows": [{"id": str(r.id), "server_name": r.server_name, "environment": r.environment,
                                    "environment_source": r.environment_source} for r in rows],
        }

        # ---- fail-closed refusals (authoritative contradiction) BEFORE any write -------------------------------
        if existing_cat is not None and existing_cat.environment != env:
            _verified = "VERIFIED " if existing_cat.is_verified else ""
            raise CommandError(
                f"REFUSED: an existing {_verified}catalogue entry classifies {server!r} as "
                f"{existing_cat.environment!r}, which contradicts the attested {env!r}. An existing catalogue entry "
                f"is not overridden here — reconcile the catalogue through its own governed review, not this command.")
        for r in rows:
            if r.environment_source in (BrokerServer.OPERATOR, BrokerServer.CATALOGUE) and r.environment != env:
                raise CommandError(
                    f"REFUSED: BrokerServer row {r.id} is stamped environment_source={r.environment_source!r} "
                    f"(authoritative) as {r.environment!r}, contradicting the attested {env!r}. Not overriding an "
                    f"authoritative classification.")

        # ---- compute intended changes ------------------------------------------------------------------------
        cat_change = (existing_cat is None or existing_cat.environment != env or not existing_cat.is_verified
                      or (display and existing_cat.broker_display_name != display))
        # Reconcile a row only when it actually needs it: a wrong environment, or an UNVERIFIED (inferred) source.
        # A row already stamped authoritative (operator/catalogue) AND already on the attested environment is left
        # untouched — never downgrade a catalogue-mirrored provenance to operator for a no-op (a disagreeing
        # operator/catalogue row was already refused above).
        row_changes = [r for r in rows if r.environment != env or r.environment_source == BrokerServer.INFERRED]

        plan = {
            "catalogue": ("CREATE" if existing_cat is None else ("UPDATE" if cat_change else "UNCHANGED")),
            "catalogue_target": {"server_name": server, "environment": env, "is_verified": True,
                                  "broker_display_name": display, "evidence": evidence},
            "broker_server_row_updates": [{"id": str(r.id), "from": {"environment": r.environment,
                                           "environment_source": r.environment_source},
                                           "to": {"environment": env,
                                                  "environment_source": BrokerServer.OPERATOR}} for r in row_changes],
        }
        self.stdout.write("=== BEFORE ===")
        self.stdout.write(json.dumps(before, indent=2, default=str))
        self.stdout.write("=== PLAN ===")
        self.stdout.write(json.dumps(plan, indent=2, default=str))

        if not cat_change and not row_changes:
            self.stdout.write(self.style.SUCCESS("ALREADY CONSISTENT — no change needed (idempotent no-op)."))
            return

        if not apply:
            self.stdout.write(self.style.WARNING("DRY-RUN — no changes written. Re-run with --apply to execute."))
            self.stdout.write("ROLLBACK (after an apply): delete the catalogue entry if it was CREATEd, and restore "
                              "each BrokerServer row's environment/environment_source to its 'from' value above.")
            return

        # ---- apply (atomic) ----------------------------------------------------------------------------------
        with transaction.atomic():
            # Never blank an existing display name: write broker_display_name ONLY when one was supplied (on CREATE
            # the model default "" applies; on UPDATE an omitted field is left unchanged by update_or_create).
            cat_defaults = {"server_name": server, "environment": env, "is_verified": True, "evidence": evidence}
            if display:
                cat_defaults["broker_display_name"] = display
            cat, created = BrokerServerCatalogue.objects.update_or_create(name_key=key, defaults=cat_defaults)
            for r in row_changes:
                r.environment = env
                r.environment_source = BrokerServer.OPERATOR
                r.save(update_fields=["environment", "environment_source"])

        # audit (fail-open; no credentials — server name + env + evidence ref only)
        try:
            from core.audit import log_event
            log_event(None, "broker_server_environment_attested", severity="WARN",
                      entity_type="broker_server", entity_id=server,
                      metadata={"environment": env, "environment_source": BrokerServer.OPERATOR,
                                "catalogue": ("created" if created else "updated"),
                                "rows_reconciled": [str(r.id) for r in row_changes], "evidence": evidence,
                                "before": before})
        except Exception:  # noqa: BLE001 - audit is best-effort, never fails the governed change
            pass

        verified_after, _src_after = resolve_verified_environment(server)
        self.stdout.write(self.style.SUCCESS(
            f"APPLIED — catalogue {'created' if created else 'updated'}; "
            f"{len(row_changes)} BrokerServer row(s) reconciled to {env}/operator; "
            f"resolve_verified_environment({server!r}) = {verified_after!r}."))
        self.stdout.write("=== ROLLBACK EVIDENCE (before-state for manual reversal) ===")
        self.stdout.write(json.dumps(before, indent=2, default=str))
