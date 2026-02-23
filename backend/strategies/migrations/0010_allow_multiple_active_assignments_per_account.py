"""
Migration: Replace single-active-per-account constraint with per-(account,strategy).

Allows TBP + ALTS + SCE (and future engines) to all be active on the
same trading account simultaneously.

Old constraint: uniq_active_strategy_assignment_per_instance
  → UniqueConstraint(fields=["account"], condition=Q(is_active=True))
  → Only ONE active assignment allowed per account.

New constraint: uniq_active_assignment_per_account_strategy
  → UniqueConstraint(fields=["account", "strategy"], condition=Q(is_active=True))
  → One active assignment per (account, strategy) pair.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0009_strategyruntimestate_strategyruntimeevent"),
    ]

    operations = [
        # 1. Remove the old "one active per account" constraint
        migrations.RemoveConstraint(
            model_name="strategyassignment",
            name="uniq_active_strategy_assignment_per_instance",
        ),
        # 2. Add the new "one active per (account, strategy)" constraint
        migrations.AddConstraint(
            model_name="strategyassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("account", "strategy"),
                name="uniq_active_assignment_per_account_strategy",
            ),
        ),
    ]
