"""PR4 — read-only DUAL-BROKER ingestion READINESS report.

Asserts every precondition for arming continuous Gmail ingestion WITHOUT enabling anything: it never polls a mailbox,
never ingests, never contacts a broker, never writes. It prints a secret-free report (credential PRESENCE only, never
values) and a verdict so an operator can decide to arm — the arming itself stays a separate, deliberate Sponsor act
(set ``BROKER_INTELLIGENCE_INGEST_ENABLED`` + schedule the pass). Distinguishes CAPTURE-readiness (evidence
acquisition; a positive external classifier is NOT required to capture) from WITHDRAWAL-SURFACING-readiness (which
additionally needs a certified positive classifier + a genuine external-withdrawal sample)."""
from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand


def _present(name: str) -> bool:
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name, "")
    return bool(str(v or "").strip())


class Command(BaseCommand):
    help = "Read-only readiness report for dual-broker Gmail ingestion (checks preconditions; never enables or polls)."

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerProfile, ConnectedMailbox
        from broker_intelligence.broker_senders import sender_allowlist
        from broker_intelligence.flags import broker_intelligence_ingest_enabled
        from broker_intelligence import parsers
        from broker_intelligence.parsers_tradersway import register_default_parsers
        from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS

        rows = []

        def chk(name, ok, detail=""):
            rows.append((name, bool(ok), detail))

        # 1) ≥1 CONNECTED mailbox (the single acquisition source).
        mbs = ConnectedMailbox.objects.filter(status=ConnectedMailbox.Status.CONNECTED)
        chk("connected_mailbox", mbs.exists(), f"{mbs.count()} CONNECTED")

        # 2) Both deterministic parsers register (content-based broker routing from ONE stream). Idempotent.
        register_default_parsers()
        pnames = {getattr(p, "name", "") for p in parsers.get_parsers()}
        chk("parsers_registered", {"tradersway", "is6"}.issubset({n.lower() for n in pnames}),
            "registered: " + ",".join(sorted(pnames)))

        # 3) ≥2 active broker profiles with approved sender domains + an auth policy.
        active = list(BrokerProfile.objects.filter(is_active=True))
        chk("broker_profiles_active", len(active) >= 2 and all(p.approved_domains() for p in active),
            ",".join(f"{p.slug}[{p.auth_policy}]" for p in active))

        # 4) Non-empty sender allowlist (fail-closed if empty → nothing would be retained).
        allow = sender_allowlist()
        chk("sender_allowlist", bool(allow), ",".join(sorted(allow)) or "(empty — fail-closed)")

        # 5) OAuth client credentials present (PRESENCE only — never the values).
        chk("oauth_client_credentials", _present("GMAIL_OAUTH_CLIENT_ID") and _present("GMAIL_OAUTH_CLIENT_SECRET"),
            "client id+secret present" if _present("GMAIL_OAUTH_CLIENT_ID") and _present("GMAIL_OAUTH_CLIENT_SECRET")
            else "MISSING")

        capture_ready = all(ok for _, ok, _ in rows)

        self.stdout.write(self.style.SUCCESS("Dual-broker ingestion readiness (read-only; nothing was enabled):"))
        for name, ok, detail in rows:
            mark = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
            self.stdout.write(f"  [{mark}] {name}: {detail}")

        # Current arming state (reported, never changed).
        self.stdout.write("  ingestion flag (BROKER_INTELLIGENCE_INGEST_ENABLED): " +
                          ("ON" if broker_intelligence_ingest_enabled() else "OFF (DARK)"))
        # A positive external classifier is NOT required to CAPTURE evidence (§6). Report its state separately.
        self.stdout.write("  external-withdrawal classifier certified brokers: " +
                          (",".join(sorted(CERTIFIED_BROKERS)) if CERTIFIED_BROKERS else "NONE (capture-only; no "
                           "withdrawal surfaced until a genuine external-withdrawal sample certifies one)"))

        verdict = ("CAPTURE-READY (all capture preconditions met; ingestion still DARK)" if capture_ready
                   else "NOT CAPTURE-READY (see FAIL rows above)")
        self.stdout.write(self.style.SUCCESS("verdict: " + verdict) if capture_ready
                          else self.style.ERROR("verdict: " + verdict))
        self.stdout.write("NOTE: this report ENABLES NOTHING. Arming continuous ingestion is a separate Sponsor act "
                          "(see docs/WITHDRAWAL_V1_DUAL_BROKER_ACTIVATION.md).")
