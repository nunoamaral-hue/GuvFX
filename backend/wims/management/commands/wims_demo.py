"""
WP-1 MVP demonstration.

Runs the Educational Content Flow end-to-end and prints the six required
evidence artefacts:

    1. Source record      (EducationalTopic)
    2. Context record
    3. Content record
    4. Review record
    5. Publish record
    6. Audit records

    Educational Topic -> Context -> Content -> Human Review -> Published

Usage:
    python manage.py wims_demo
    python manage.py wims_demo --decision REJECT     # exercise the reject path
    python manage.py wims_demo --actor someuser      # attribute to an existing user
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from wims import services
from wims.models import AuditEvent, Content, Publish, Review
from wims.services import workflow_state_for_topic

User = get_user_model()


class Command(BaseCommand):
    help = "Demonstrate the WP-1 Educational Content Flow end-to-end."

    def add_arguments(self, parser):
        parser.add_argument(
            "--actor",
            help="Username/email of an existing user to attribute the workflow to. "
                 "Defaults to a throwaway 'wims_demo_operator' account.",
        )
        parser.add_argument(
            "--decision",
            choices=Review.Decision.values,
            default=Review.Decision.APPROVE,
            help="Review decision to apply (default: APPROVE).",
        )
        parser.add_argument(
            "--channel",
            choices=Publish.Channel.values,
            default=Publish.Channel.TELEGRAM,
            help="Publish channel (default: TELEGRAM).",
        )

    # -- helpers ------------------------------------------------------------
    def _line(self, char="-"):
        self.stdout.write(char * 64)

    def _section(self, n, title):
        self._line("=")
        self.stdout.write(self.style.MIGRATE_HEADING(f"{n}. {title}"))

    def _resolve_actor(self, identifier):
        if identifier:
            user = (
                User.objects.filter(username=identifier).first()
                or User.objects.filter(email=identifier).first()
            )
            if not user:
                self.stderr.write(f"No user matches {identifier!r}; using demo operator.")
            else:
                return user
        user, _ = User.objects.get_or_create(
            username="wims_demo_operator",
            defaults={"email": "wims_demo_operator@example.invalid"},
        )
        return user

    # -- entrypoint ---------------------------------------------------------
    def handle(self, *args, **opts):
        actor = self._resolve_actor(opts["actor"])
        decision = opts["decision"]
        channel = opts["channel"]

        self.stdout.write(self.style.SUCCESS(
            "\nWP-1 — Educational Content Flow MVP demonstration"
        ))
        self.stdout.write(f"Operator: {actor}  |  decision: {decision}  |  channel: {channel}\n")

        # 1 — Source -------------------------------------------------------
        topic = services.create_topic(
            title="What Is Risk Management?",
            description="Educational topic introducing risk management for traders.",
            actor=actor,
            status="ACTIVE",
        )
        self._section(1, "SOURCE RECORD (EducationalTopic)")
        self.stdout.write(f"  id={topic.id}  status={topic.status}  workflow={workflow_state_for_topic(topic)}")
        self.stdout.write(f"  title={topic.title!r}")

        # 2 — Context ------------------------------------------------------
        ctx = services.create_context(
            topic=topic,
            context_text=(
                "Why risk management matters: it preserves capital so a trader can "
                "stay in the game.\n"
                "Common mistakes: oversizing positions, no stop loss, moving stops "
                "against the plan.\n"
                "What traders should understand: position sizing, risk-per-trade, and "
                "expectancy over many trades."
            ),
            actor=actor,
        )
        self._section(2, "CONTEXT RECORD")
        self.stdout.write(f"  id={ctx.id}  source_id={ctx.source_id}  status={ctx.status}")

        # 3 — Content ------------------------------------------------------
        content = services.create_content(
            context=ctx,
            title="Risk Management 101: Protect Your Capital First",
            content_text=(
                "Most traders blow up not because their entries are bad, but because "
                "their risk is unmanaged. Risk a small, fixed fraction of your account "
                "per trade, always define your stop before you enter, and let "
                "expectancy do the work over many trades."
            ),
            actor=actor,
        )
        self._section(3, "CONTENT RECORD")
        self.stdout.write(f"  id={content.id}  context_id={content.context_id}  status={content.status}")
        self.stdout.write(f"  title={content.title!r}")

        # 4 — Human review (mandatory) ------------------------------------
        services.submit_for_review(content=content, actor=actor)
        self.stdout.write(self.style.WARNING(
            f"  -> submitted for review; workflow={workflow_state_for_topic(topic)}"
        ))
        review = services.review_content(
            content=content,
            decision=decision,
            reviewer=actor,
            notes="Reviewed for the WP-1 MVP demonstration.",
        )
        content.refresh_from_db()
        self._section(4, "REVIEW RECORD (mandatory human review)")
        self.stdout.write(f"  id={review.id}  decision={review.review_decision}  reviewer={review.reviewer}")
        self.stdout.write(f"  content.status -> {content.status}")

        # 5 — Publish (only if approved) ----------------------------------
        self._section(5, "PUBLISH RECORD (manual; channel simulated)")
        pub = None
        if content.status == Content.Status.APPROVED:
            pub = services.publish_content(
                content=content, channel=channel, publisher=actor,
            )
            content.refresh_from_db()
            self.stdout.write(
                f"  id={pub.id}  channel={pub.channel}  simulated={pub.simulated}"
            )
            self.stdout.write(f"  content.status -> {content.status}")
        else:
            self.stdout.write(self.style.NOTICE(
                "  Content was REJECTED — publish correctly blocked (no publish record)."
            ))
        self.stdout.write(f"  topic workflow state -> {workflow_state_for_topic(topic)}")

        # 6 — Audit trail --------------------------------------------------
        self._section(6, "AUDIT RECORDS")
        related_ids = {topic.id, ctx.id, content.id}
        events = AuditEvent.objects.filter(
            object_id__in=related_ids
        ).order_by("timestamp", "id")
        for ev in events:
            actor_str = ev.actor.get_username() if ev.actor else "system"
            self.stdout.write(
                f"  {ev.timestamp:%H:%M:%S}  {ev.event:<22} "
                f"{ev.object_type}#{ev.object_id:<4} by {actor_str}  {ev.detail or ''}"
            )

        self._line("=")
        ok = (
            content.status in (Content.Status.PUBLISHED, Content.Status.REJECTED)
            and events.count() >= 5
        )
        if ok:
            self.stdout.write(self.style.SUCCESS(
                "PASS — workflow demonstrated end-to-end with persisted records "
                "and audit evidence."
            ))
        else:  # pragma: no cover - defensive
            self.stderr.write("FAIL — workflow did not complete as expected.")
