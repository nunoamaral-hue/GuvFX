"""
SIGNAL-ACQUISITION-MVP — onboard / arm / pause a signal provider (operator entry).

Providers are data: create the row (chat id from Nuno's link), then arm it. No
Telegram login or API call happens here — the chat id is supplied by the operator.

    python manage.py onboard_provider --slug wayond --chat-id 123 --parser wayond_v1
    python manage.py onboard_provider --slug wayond --arm
    python manage.py onboard_provider --slug wayond --pause --reason "provider down"
"""

from django.core.management.base import BaseCommand, CommandError

from signal_intake.models import ParserProfile, SignalProvider


class Command(BaseCommand):
    help = "Create/update, arm, or pause a SignalProvider (no Telegram call)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True, help="Stable provider slug.")
        parser.add_argument("--name", default="", help="Display name.")
        parser.add_argument("--chat-id", default=None, help="Telegram chat id (trust boundary).")
        parser.add_argument("--parser", default="wayond_v1", help="ParserProfile slug.")
        parser.add_argument("--window", type=int, default=None, help="Acquisition window seconds.")
        parser.add_argument("--arm", action="store_true", help="Set status ARMED.")
        parser.add_argument("--pause", action="store_true", help="Set status PAUSED.")
        parser.add_argument("--reason", default="", help="Disabled reason (with --pause).")
        parser.add_argument(
            "--reset-watermark", action="store_true",
            help="REQUIRED to change the chat id of a provider that already has a catch-up "
                 "watermark (a channel switch). Resets the catch-up floor so the new chat is "
                 "caught up from the start — a new chat's message ids are independent of the "
                 "old chat's, so inheriting the old watermark would silently skip them (T1).")

    def handle(self, *args, **o):
        if o["arm"] and o["pause"]:
            raise CommandError("--arm and --pause are mutually exclusive")

        provider = SignalProvider.objects.filter(slug=o["slug"]).first()
        if provider is None:
            profile = ParserProfile.objects.filter(slug=o["parser"]).first()
            if profile is None:
                raise CommandError(
                    f"parser profile {o['parser']!r} does not exist — seed it first"
                )
            provider = SignalProvider.objects.create(
                slug=o["slug"], name=o["name"], parser_profile=profile,
                telegram_chat_id=str(o["chat_id"] or ""),
                acquisition_window_seconds=o["window"] or 600,
            )
            self.stdout.write(f"created provider '{provider.slug}' (status={provider.status})")
        else:
            fields = []
            if o["chat_id"] is not None:
                new_chat = str(o["chat_id"])
                if new_chat != provider.telegram_chat_id:
                    # CHANNEL SWITCH (T1 fail-closed): the per-provider watermark is a
                    # chat-blind scalar used as the catch-up fetch floor (iter_messages
                    # min_id). A new chat numbers its message ids independently, so
                    # inheriting the old chat's high-water mark would make catch-up
                    # SILENTLY SKIP every new-chat message with a lower id — a
                    # cross-channel loss the dedup fix alone cannot prevent (it never runs
                    # for messages filtered out before acquisition). Refuse the switch
                    # unless the operator explicitly resets the watermark.
                    if provider.watermark_last_message_id and not o["reset_watermark"]:
                        raise CommandError(
                            f"refusing to change chat id for '{provider.slug}': a catch-up "
                            f"watermark ({provider.watermark_last_message_id!r}) is set. A new "
                            f"chat's message ids are independent, so the old watermark would "
                            f"silently skip new-chat messages. Re-run with --reset-watermark "
                            f"to switch (resets the catch-up floor to re-catch-up from the start)."
                        )
                    if o["reset_watermark"]:
                        provider.watermark_last_message_id = ""
                        fields.append("watermark_last_message_id")
                provider.telegram_chat_id = new_chat; fields.append("telegram_chat_id")
            if o["name"]:
                provider.name = o["name"]; fields.append("name")
            if o["window"] is not None:
                provider.acquisition_window_seconds = o["window"]; fields.append("acquisition_window_seconds")
            if fields:
                provider.save(update_fields=fields + ["updated_at"])

        if o["arm"]:
            if not provider.telegram_chat_id:
                raise CommandError("cannot arm a provider without a verified chat id")
            provider.status = SignalProvider.Status.ARMED
            provider.disabled_reason = ""
            provider.save(update_fields=["status", "disabled_reason", "updated_at"])
            self.stdout.write(f"provider '{provider.slug}' ARMED")
        elif o["pause"]:
            provider.status = SignalProvider.Status.PAUSED
            provider.disabled_reason = o["reason"]
            provider.save(update_fields=["status", "disabled_reason", "updated_at"])
            self.stdout.write(f"provider '{provider.slug}' PAUSED ({o['reason']})")

        self.stdout.write(
            f"provider '{provider.slug}' status={provider.status} "
            f"parser={provider.parser_profile.slug} window={provider.acquisition_window_seconds}s"
        )
