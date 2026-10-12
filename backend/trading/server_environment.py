"""trading.server_environment — AUTHORITATIVE broker-server environment resolution (governance correction 2026-10-12).

Fixes the first-registration environment-poisoning defect: ``BrokerServer.environment`` used to be set from the FIRST
registering account's ``account_type`` (defaulting to DEMO), so a server named "…-Live" first touched by a demo
account was pinned DEMO for the whole estate, blocking legitimate LIVE registration.

Governance rules enforced here (Sponsor, 2026-10-12):
  1. A server's environment is NEVER established from the first registering user's account_type.
  2. Server-name SUFFIXES ("-Live"/"-Demo"/"-Trade") are HINTS for an operator, never authoritative proof — this
     module does NOT classify by suffix.
  3. The verified ``BrokerServerCatalogue`` is the authoritative source for known servers.
  4. An UNKNOWN / unverified server environment FAILS CLOSED (resolves to None) — callers must refuse, never default
     to DEMO.
  5. Environment VALUES are normalised (case/whitespace) so "DEMO" and "demo" cannot diverge.
  6. Case-insensitive server-name collisions are DETECTABLE (``server_name_collisions``) — e.g. "TradersWay-Live" vs
     "Tradersway-Live" — without silently merging identities.

Pure/read-only helpers; the only DB read is the catalogue lookup. No mutation, no I/O.
"""
from __future__ import annotations

from typing import Optional, Tuple

CATALOGUE = "catalogue"   # resolved from the verified catalogue (authoritative)
UNKNOWN = "unknown"       # not in the catalogue → caller fails closed


def normalise_server_name(name) -> str:
    """Canonical surface form of a server name: trimmed, internal whitespace collapsed. Case is PRESERVED (broker
    server strings are case-sensitive to MT5); collision detection uses the case-folded key below."""
    return " ".join(str(name or "").split())


def server_name_key(name) -> str:
    """Case-insensitive lookup/collision key for a server name (normalised + casefolded). Empty string for blank."""
    return normalise_server_name(name).casefold()


def normalise_environment(env) -> Optional[str]:
    """Normalise an environment value to the canonical lowercase 'demo'/'live', or None if unrecognised (fail
    closed — an unrecognised environment is never silently treated as live OR demo)."""
    from trading.models import BrokerServer
    v = str(env or "").strip().lower()
    return v if v in (BrokerServer.DEMO, BrokerServer.LIVE) else None


def resolve_verified_environment(server_name) -> Tuple[Optional[str], str]:
    """Resolve a server name to its AUTHORITATIVE environment via the verified catalogue.

    Returns ``(environment, CATALOGUE)`` when a VERIFIED catalogue entry exists (case-insensitive match on the
    name key), else ``(None, UNKNOWN)`` — the fail-closed signal. NEVER infers from a suffix or from any account."""
    from trading.models import BrokerServerCatalogue
    key = server_name_key(server_name)
    if not key:
        return (None, UNKNOWN)
    entry = BrokerServerCatalogue.objects.filter(name_key=key, is_verified=True).first()
    if entry is None:
        return (None, UNKNOWN)
    return (entry.environment, CATALOGUE)


def server_name_suffix_hint(server_name) -> Optional[str]:
    """A non-authoritative HINT (for operator review / the reconciliation report only) derived from the server-name
    suffix. NEVER used by provisioning to classify a server — returned purely to help a human verify the catalogue."""
    from trading.models import BrokerServer
    key = server_name_key(server_name)
    if key.endswith("-demo") or key.endswith("demo"):
        return BrokerServer.DEMO
    if key.endswith("-live") or key.endswith("-real") or key.endswith("-trade") or key.endswith("live"):
        return BrokerServer.LIVE
    return None


def server_name_collisions():
    """READ-ONLY: map each case-folded key that is shared by MORE THAN ONE distinct BrokerServer.server_name to the
    list of those distinct surface names — i.e. case-insensitive collisions (e.g. 'TradersWay-Live' vs
    'Tradersway-Live'). Empty dict when there are none. Does not merge or mutate anything."""
    from collections import defaultdict
    from trading.models import BrokerServer
    groups = defaultdict(set)
    for name in BrokerServer.objects.values_list("server_name", flat=True):
        groups[server_name_key(name)].add(name)
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}
