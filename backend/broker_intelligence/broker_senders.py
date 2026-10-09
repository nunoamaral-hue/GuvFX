"""WP3b privacy remediation (R1/R3a) — broker-sender allowlist + EXACT-domain matching.

Connecting a member's PERSONAL mailbox means the ingestion pipeline must NEVER acquire or retain mail that is not
from a configured, verified broker sender. This module is the single source of truth for "is this From: an
allowlisted broker sender?".

Two load-bearing properties:
* **Exact registrable-domain / subdomain match — never a substring.** ``eviltradersway.com`` can NOT pass a
  ``tradersway.com`` allowlist, and ``mail.tradersway.com`` CAN. (The old parser used a substring test; R3a replaces
  it.) A From header is NOT trusted on its own — authenticity (Authentication-Results, R3b) is checked separately
  before an event is created; this module only decides allowlist membership.
* **Fail-closed.** An empty/unset allowlist matches NOTHING. So with no verified broker configured, the retention
  gate stores nothing and the acquisition filter fetches nothing — a personal inbox cannot leak into evidence.

The allowlist is OPERATOR CONFIG (settings-then-env ``BROKER_INTELLIGENCE_SENDER_ALLOWLIST``), never derived from the
message itself. Pilot value: ``tradersway.com`` (the verified TradersWay sender domain) — and only that.
"""
from __future__ import annotations

import os
import re
from typing import FrozenSet, Optional


def _domain_of(address: Optional[str]) -> str:
    """The lowercased domain of an email address, dropping any display-name/angle brackets. '' if unparseable."""
    a = (address or "").strip().lower()
    if "<" in a and ">" in a:                      # 'Name <local@domain>' -> 'local@domain'
        a = a[a.rfind("<") + 1:a.rfind(">")]
    a = a.strip().strip("<>").strip()
    if "@" not in a:
        return ""
    return a.rsplit("@", 1)[1].strip()


def domain_matches(domain: str, allowed: str) -> bool:
    """True iff ``domain`` is EXACTLY ``allowed`` or a subdomain of it — never a substring. Both lowercased."""
    domain, allowed = (domain or "").strip().lower(), (allowed or "").strip().lower()
    if not domain or not allowed:
        return False
    return domain == allowed or domain.endswith("." + allowed)


def sender_allowlist() -> FrozenSet[str]:
    """Configured broker sender domains (settings-then-env ``BROKER_INTELLIGENCE_SENDER_ALLOWLIST``, comma/space
    separated), lowercased. EMPTY by default → fail-closed (nothing matches). NEVER derived from a message header."""
    from django.conf import settings
    raw = getattr(settings, "BROKER_INTELLIGENCE_SENDER_ALLOWLIST", None)
    if raw is None:
        raw = os.getenv("BROKER_INTELLIGENCE_SENDER_ALLOWLIST", "")
    parts = re.split(r"[,\s]+", str(raw or "").strip().lower())
    return frozenset(p for p in parts if p)


def is_allowlisted_broker_sender(from_address: Optional[str]) -> bool:
    """True iff the From domain is EXACTLY an allowlisted broker domain (or a subdomain of one). Fail-closed: an
    empty allowlist, or an unparseable/empty From, → False. This is the single gate used by BOTH the acquisition
    filter (don't even fetch non-broker mail) and the retention gate (never store non-broker mail)."""
    dom = _domain_of(from_address)
    if not dom:
        return False
    allow = sender_allowlist()
    if not allow:
        return False
    return any(domain_matches(dom, d) for d in allow)
