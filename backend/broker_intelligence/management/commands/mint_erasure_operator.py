"""ADR-0054 — mint (or revoke) a short-lived per-operator executor LOGIN (F-5).

Creates exactly one conforming ``guvfx_erx_<suffix>`` LOGIN role, a member of the NOLOGIN
``guvfx_erasure_exec`` group, least-privilege, with a HARD ``VALID UNTIL`` expiry. The name is validated
fail-closed (``erasure_operators``) so the governed rollback's ``DROP ... guvfx_erx_%`` enumeration always
covers it. Run by the independent grant-admin (a role with CREATEROLE), never the web/worker runtime.

Secrets: the operator password is read from ``ERASURE_OPERATOR_PW`` (env/secret store) — never a CLI argument,
never logged. The role also expires via ``VALID UNTIL``, so a leaked/forgotten login stops authenticating.

Lifecycle:
  create:  manage.py mint_erasure_operator --suffix <s> [--valid-minutes N]    (ERASURE_OPERATOR_PW set)
  revoke:  manage.py mint_erasure_operator --suffix <s> --revoke                (DROP the login after use)
Revocation is also performed wholesale by ops/erasure/production_rollback_erasure.sql (drops guvfx_erx_%).
"""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from broker_intelligence.erasure_operators import OperatorNamingError, operator_login_name


class Command(BaseCommand):
    help = "Mint/revoke a short-lived guvfx_erx_<suffix> executor login (member of guvfx_erasure_exec)."

    def add_arguments(self, parser):
        parser.add_argument("--suffix", required=True, help="operator suffix; the login is guvfx_erx_<suffix>")
        parser.add_argument("--valid-minutes", type=int, default=60, dest="valid_minutes",
                            help="VALID UNTIL expiry in minutes from now (create only; default 60)")
        parser.add_argument("--revoke", action="store_true", help="DROP the operator login instead of creating it")

    def handle(self, *args, **opts):
        # Fail-closed naming FIRST — before any DB work — so a non-conforming name never touches the cluster.
        try:
            name = operator_login_name(opts["suffix"])
        except OperatorNamingError as exc:
            raise CommandError(f"REFUSED: {exc}. Nothing was changed.")

        if opts["revoke"]:
            with connection.cursor() as cur:
                cur.execute(f'DROP ROLE IF EXISTS "{name}"')
            self.stdout.write(self.style.SUCCESS(f"revoked operator login {name} (dropped if present)."))
            return

        pw = os.environ.get("ERASURE_OPERATOR_PW", "")
        if not pw:
            raise CommandError("REFUSED: ERASURE_OPERATOR_PW is not set (the password is supplied out of band, "
                               "never on the command line). Nothing was changed.")
        minutes = max(1, int(opts["valid_minutes"]))
        valid_until = (timezone.now() + timezone.timedelta(minutes=minutes))
        vu = valid_until.strftime("%Y-%m-%d %H:%M:%S%z")
        # Identifier is validated to [a-z0-9_]; password is a quoted literal (doubled quotes). The statement is
        # NOT logged by this command (and production runs with DEBUG off), so the secret is not emitted.
        pw_lit = "'" + pw.replace("'", "''") + "'"
        stmt = (f'CREATE ROLE "{name}" LOGIN PASSWORD {pw_lit} VALID UNTIL \'{vu}\' '
                f'NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS IN ROLE guvfx_erasure_exec')
        with connection.cursor() as cur:
            cur.execute(f'DROP ROLE IF EXISTS "{name}"')
            cur.execute(stmt)
        self.stdout.write(self.style.SUCCESS(
            f"minted operator login {name} (member of guvfx_erasure_exec), VALID UNTIL {vu}. "
            f"Drop it after use: --suffix {opts['suffix']} --revoke (or production_rollback_erasure.sql)."))
