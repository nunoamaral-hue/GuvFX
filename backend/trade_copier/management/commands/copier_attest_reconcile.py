"""copier_attest_reconcile - READ-ONLY authoritative reconciliation of an AMBIGUOUS one-shot attestation POST
(ADR-0077 / GFX-COPIER-ATTEST-TRANSPORT-RECONCILIATION-139).

The operator orchestration calls this over the existing operator SSH channel after a transport-ambiguous
attestation POST, to decide - WITHOUT a blind retry - whether the backend actually processed that request.
It prints a sanitised JSON classification and does NOTHING else: it creates no state, reads no key, enables
no flag, opens no window, contacts no host or broker. ``--nonce`` is the non-secret attestation envelope
nonce the emitter returned in its sanitised result.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Classify an ambiguous one-shot attestation POST from authoritative durable state (read-only): "
            "PROCESSED_VERIFIED / PROCESSED_REJECTED / NOT_PROCESSED / AMBIGUOUS.")

    def add_arguments(self, parser):
        parser.add_argument("--nonce", required=True, help="The attestation envelope nonce (non-secret).")
        parser.add_argument("--correlation-id", default="", help="Scope the window lookup to this operation.")
        parser.add_argument("--purpose", default="ATTEST", choices=["ATTEST", "BOTH"])

    def handle(self, *args, **opts):
        from trade_copier.attest_reconcile import reconcile_attestation
        out = reconcile_attestation(nonce=opts["nonce"], correlation_id=opts["correlation_id"],
                                    purpose=opts["purpose"])
        self.stdout.write(json.dumps(out, default=str, indent=1))
