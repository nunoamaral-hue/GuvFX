"""
Seed marketplace strategies into the database.

Creates Strategy rows (and optionally StrategyAssignment rows) from
the MARKETPLACE_STRATEGIES catalogue in views.py.  Idempotent — safe
to run repeatedly.

Idempotency key (in priority order):
  1. (owner, filters__marketplace_id=<mid>)     — stable, preferred
  2. (owner, filters__template_slug=<slug>)     — fallback
  3. (owner, name=<template_name>)              — legacy only; backfills keys

Usage examples
--------------
# Seed all automation-ready strategies (Strategy rows only):
  python manage.py seed_strategies --owner-email admin@example.com

# Seed specific marketplace IDs:
  python manage.py seed_strategies --owner-email admin@example.com \\
      --marketplace-ids mp-005 mp-009

# Create TEST assignments on an account:
  python manage.py seed_strategies --owner-email admin@example.com \\
      --account-id 1 --create-assignments

# Create LIVE assignments (demo accounts only, double opt-in):
  python manage.py seed_strategies --owner-email admin@example.com \\
      --account-id 1 --create-assignments --stage LIVE --allow-live
"""

import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from strategies.models import Strategy, StrategyAssignment
from strategies.views import MARKETPLACE_STRATEGIES
from trading.models import TradingAccount

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = "Seed marketplace strategies into the database (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            required=True,
            help="Email of the User who will own the Strategy rows.",
        )
        parser.add_argument(
            "--marketplace-ids",
            nargs="*",
            default=None,
            help=(
                "Marketplace IDs to seed (e.g. mp-005 mp-009). "
                "Default: all entries with automation_ready=True."
            ),
        )
        parser.add_argument(
            "--account-id",
            type=int,
            default=None,
            help="TradingAccount ID for assignment creation (requires --create-assignments).",
        )
        parser.add_argument(
            "--create-assignments",
            action="store_true",
            default=False,
            help="Also create StrategyAssignment rows. Requires --account-id.",
        )
        parser.add_argument(
            "--stage",
            choices=["TEST", "LIVE"],
            default="TEST",
            help="Stage for created assignments (default: TEST).",
        )
        parser.add_argument(
            "--allow-live",
            action="store_true",
            default=False,
            help="Required alongside --stage LIVE. Only demo accounts accepted.",
        )

    def handle(self, *args, **options):
        owner_email = options["owner_email"]
        marketplace_ids = options["marketplace_ids"]
        account_id = options["account_id"]
        create_assignments = options["create_assignments"]
        stage = options["stage"]
        allow_live = options["allow_live"]

        # ── Governance checks ──────────────────────────────────────
        if stage == "LIVE" and create_assignments and not allow_live:
            raise CommandError(
                "Creating LIVE assignments requires --allow-live. "
                "This is a safety gate — LIVE assignments trigger real order placement."
            )

        if create_assignments and account_id is None:
            raise CommandError("--create-assignments requires --account-id.")

        # ── Resolve owner ──────────────────────────────────────────
        try:
            owner = User.objects.get(email=owner_email)
        except User.DoesNotExist:
            raise CommandError(f"User with email '{owner_email}' not found.")

        # ── Resolve account (optional) ─────────────────────────────
        account = None
        if account_id is not None:
            account = TradingAccount.objects.filter(id=account_id).first()
            if not account:
                raise CommandError(f"TradingAccount id={account_id} not found.")

            if create_assignments:
                if not getattr(account, "is_active", True):
                    raise CommandError(
                        f"TradingAccount id={account_id} is inactive. "
                        "Cannot create assignments on inactive accounts."
                    )
                if stage == "LIVE" and not getattr(account, "is_demo", False):
                    raise CommandError(
                        f"TradingAccount id={account_id} is not a demo account. "
                        "LIVE assignments are only allowed on demo accounts."
                    )

        # ── Select marketplace entries ─────────────────────────────
        if marketplace_ids:
            entries = {
                mid: tpl
                for mid, tpl in MARKETPLACE_STRATEGIES.items()
                if mid in marketplace_ids
            }
            missing = set(marketplace_ids) - set(entries)
            if missing:
                raise CommandError(
                    f"Unknown marketplace IDs: {', '.join(sorted(missing))}. "
                    f"Valid: {', '.join(sorted(MARKETPLACE_STRATEGIES))}"
                )
        else:
            # Default: seed only automation-ready templates
            entries = {
                mid: tpl
                for mid, tpl in MARKETPLACE_STRATEGIES.items()
                if tpl.get("automation_ready")
            }

        if not entries:
            self.stdout.write("Nothing to seed.")
            return

        # ── Seed each entry ────────────────────────────────────────
        allowed_fields = {f.name for f in Strategy._meta.fields}

        for mid, tpl in entries.items():
            defaults = tpl.get("defaults") or {}
            template_name = tpl.get("name") or mid
            template_slug = (defaults.get("filters") or {}).get("template_slug", "")

            # Ensure filters carry stable idempotency keys
            filters = dict(defaults.get("filters") or {})
            filters["marketplace_id"] = mid
            if template_slug:
                filters["template_slug"] = template_slug

            create_kwargs = {
                k: v for k, v in defaults.items() if k in allowed_fields
            }
            create_kwargs["owner"] = owner
            create_kwargs["name"] = template_name
            create_kwargs["description"] = tpl.get("description") or ""
            create_kwargs["filters"] = filters

            with transaction.atomic():
                strategy = self._find_strategy(owner, mid, template_slug, template_name)

                if strategy:
                    # Ensure stable keys are present + refresh filters if stale
                    dirty = False
                    current_filters = strategy.filters or {}
                    if current_filters.get("marketplace_id") != mid:
                        current_filters["marketplace_id"] = mid
                        dirty = True
                    if template_slug and current_filters.get("template_slug") != template_slug:
                        current_filters["template_slug"] = template_slug
                        dirty = True
                    if current_filters != filters:
                        strategy.filters = filters
                        dirty = True
                    if dirty:
                        strategy.save(update_fields=["filters", "updated_at"])
                        self.stdout.write(
                            f"  [UPDATE] {mid} → strategy id={strategy.id} "
                            f"'{template_name}' (filters refreshed)"
                        )
                    else:
                        self.stdout.write(
                            f"  [EXISTS] {mid} → strategy id={strategy.id} "
                            f"'{template_name}'"
                        )
                else:
                    strategy = Strategy.objects.create(**create_kwargs)
                    self.stdout.write(
                        f"  [CREATE] {mid} → strategy id={strategy.id} "
                        f"'{template_name}'"
                    )

                # Assignment creation (opt-in only)
                if create_assignments and account:
                    self._ensure_assignment(strategy, account, stage)

        self.stdout.write(self.style.SUCCESS(f"[DONE] Seeded {len(entries)} strategies."))

    # ── Helpers ────────────────────────────────────────────────────

    def _find_strategy(self, owner, marketplace_id, template_slug, template_name):
        """
        Find existing Strategy using stable idempotency keys (priority order):
          1. (owner, filters__marketplace_id)
          2. (owner, filters__template_slug)
          3. (owner, name)  — legacy fallback
        """
        # 1. Primary key: marketplace_id in filters
        strategy = (
            Strategy.objects
            .filter(owner=owner, filters__marketplace_id=marketplace_id)
            .order_by("-id")
            .first()
        )
        if strategy:
            return strategy

        # 2. Fallback: template_slug in filters
        if template_slug:
            strategy = (
                Strategy.objects
                .filter(owner=owner, filters__template_slug=template_slug)
                .order_by("-id")
                .first()
            )
            if strategy:
                return strategy

        # 3. Legacy fallback: owner + name
        strategy = (
            Strategy.objects
            .filter(owner=owner, name=template_name)
            .order_by("-id")
            .first()
        )
        return strategy

    def _ensure_assignment(self, strategy, account, stage):
        """Create or reactivate a StrategyAssignment."""
        assignment, created = StrategyAssignment.objects.get_or_create(
            strategy=strategy,
            account=account,
            defaults={
                "is_active": True,
                "stage": stage,
            },
        )
        if created:
            self.stdout.write(
                f"         assignment id={assignment.id} "
                f"account={account.id} stage={stage}"
            )
        elif not assignment.is_active:
            assignment.is_active = True
            assignment.stage = stage
            assignment.save(update_fields=["is_active", "stage", "updated_at"])
            self.stdout.write(
                f"         assignment id={assignment.id} "
                f"reactivated stage={stage}"
            )
        else:
            self.stdout.write(
                f"         assignment id={assignment.id} "
                f"already active (stage={assignment.stage})"
            )
