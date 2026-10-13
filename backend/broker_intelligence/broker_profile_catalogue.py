"""PR3 — BrokerProfile ↔ BrokerServerCatalogue reconciliation (read-only; the catalogue stays AUTHORITATIVE).

The trading-side ``BrokerServerCatalogue`` (governance correction 2026-10-12) is the SINGLE authoritative source for
a broker SERVER's verified demo/live environment and canonical name. A ``broker_intelligence.BrokerProfile`` carries
per-broker EMAIL/auth/parser config and may NAME the broker's server(s); this module cross-checks those names against
the catalogue WITHOUT creating a competing authority. It is strictly read-only: it NEVER writes the catalogue, never
mutates a ``BrokerServer`` row, never touches ``servers.dat`` or an MT5 install. It only REPORTS agreement /
divergence so an operator can curate the catalogue (the authoritative action stays a governed catalogue edit).
"""
from __future__ import annotations

from trading.server_environment import CATALOGUE, resolve_verified_environment


def reconcile_profile(profile) -> dict:
    """Cross-check one BrokerProfile's declared server names against the verified catalogue. Read-only.

    For each declared server name: resolve its AUTHORITATIVE environment from the catalogue (case-insensitive,
    fail-closed). A name the catalogue does not verify is reported as ``in_catalogue=False`` (flagged for operator
    curation) — never auto-added, never trusted. Returns a report, mutates nothing."""
    servers = []
    for name in profile.server_name_list():
        env, source = resolve_verified_environment(name)
        servers.append({
            "server_name": name,
            "in_catalogue": source == CATALOGUE,
            "verified_environment": env,                 # 'demo' / 'live' (authoritative) or None (unknown)
        })
    unknown = [s["server_name"] for s in servers if not s["in_catalogue"]]
    return {
        "slug": profile.slug,
        "broker": profile.name,
        "declared_servers": len(servers),
        "servers": servers,
        # True only when the profile names ≥1 server and EVERY one is verified in the catalogue.
        "all_in_catalogue": bool(servers) and not unknown,
        "unknown_servers": unknown,                      # declared by the profile but NOT catalogue-verified
    }


def reconcile_all() -> list:
    """Reconciliation report for every ACTIVE BrokerProfile. Read-only; for a staff/operator diagnostic view."""
    from .models import BrokerProfile
    return [reconcile_profile(p) for p in BrokerProfile.objects.filter(is_active=True).order_by("name")]


def category_capability_violations(profile) -> list:
    """Defensive structural check: the categories a profile DECLARES it supports must be real TransactionCategory
    values. Returns any declared tokens that are NOT valid categories (a config typo that could let the UI promise a
    category the system does not model). Read-only; pure. (The parser-emits-⊆-declared guarantee is asserted by the
    test-suite against the actual parsers; this guards the config value itself.)"""
    from .models import TransactionCategory
    valid = {c.value for c in TransactionCategory}
    return [tok for tok in profile.supported_category_list() if tok not in valid]
