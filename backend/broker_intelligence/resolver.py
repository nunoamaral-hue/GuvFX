"""WP3 — To:-alias → BrokerAccount resolver (read-only).

Maps an inbound message's recipient addresses to the ``BrokerEmailAlias`` (and, if the alias is ACTIVE/bound, its
``TradingAccount``). This is the ONLY coupling from ingestion to the trading domain and it is strictly READ-ONLY —
a lookup, never a write, never an order. An unknown/unmatched address → (None, None); a PENDING (Journey-B, unbound)
or RETIRED alias resolves the alias but NOT an account (so evidence is still captured + attributed, but a removed/
unbound account is never treated as live).
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

from .models import BrokerEmailAlias, BrokerEmailIdentity


def _local_part(address: str) -> str:
    """The opaque local-part of an email address, lower-cased and stripped of any display-name/angle brackets."""
    a = _bare_address(address)
    if "@" not in a:
        return ""
    return a.rsplit("@", 1)[0].strip().lower()


def _bare_address(address: str) -> str:
    """The bare ``local@domain`` from a header value, dropping any display name / angle brackets. Lower-cased."""
    a = (address or "").strip().lower()
    if "<" in a and ">" in a:                      # 'Name <local@domain>' -> 'local@domain'
        a = a[a.rfind("<") + 1:a.rfind(">")].strip()
    return a.strip("<>").strip()


# Gmail (and its legacy alias googlemail.com) address the SAME mailbox regardless of dots in the local-part, a '+tag'
# suffix, or which of the two domains is used. R2: canonicalise ONLY these Google forms for equivalence matching, so a
# broker email addressed to nrfda1111@googlemail.com matches an identity registered as n.rfda1111+tw@gmail.com (and
# vice-versa). Non-Google domains are returned unchanged (only Gmail ignores dots/plus) — never broaden matching for
# any other provider. Pure; the STORED registration address is preserved — this is only used to COMPARE.
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def canonical_address(address: str) -> str:
    """The equivalence-canonical form of an address. For gmail.com/googlemail.com: unify domain→gmail.com, strip dots
    from the local-part, drop a '+tag' suffix. Any other domain → the bare lowercased address unchanged."""
    bare = _bare_address(address)
    if "@" not in bare:
        return bare
    local, _, domain = bare.rpartition("@")
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def resolve_alias(to_addresses: Iterable[str], *, owner_user=None) -> Optional[BrokerEmailAlias]:
    """The first address whose opaque local-part matches a ``BrokerEmailAlias.alias_local`` (globally unique),
    or None. Match is on the opaque local-part only — never the display name. Read-only.

    ``owner_user`` (SECURITY — cross-user attribution firewall): recipient headers (To/Delivered-To/X-Original-To)
    are attacker-controllable, so when a message is ingested from a specific owner's mailbox the resolution MUST be
    scoped to that owner — a spoofed header carrying another member's opaque alias then cannot attribute the message
    to that other member. When set, only an alias bound to one of ``owner_user``'s accounts can match (an unbound
    alias has no owner and is excluded, fail-closed)."""
    for addr in to_addresses or ():
        lp = _local_part(addr)
        if not lp:
            continue
        qs = BrokerEmailAlias.objects.filter(alias_local=lp)
        if owner_user is not None:
            qs = qs.filter(user=owner_user)   # the alias's own owner (covers unbound Journey-B aliases too)
        alias = qs.first()
        if alias is not None:
            return alias
    return None


def resolve_account(alias: Optional[BrokerEmailAlias]):
    """The bound ``TradingAccount`` for an alias, but ONLY when the alias is ACTIVE and bound. A PENDING (unbound)
    or RETIRED (tombstoned) alias returns None — evidence is still attributed to the alias, but no live account is
    implied. Read-only."""
    if alias is None:
        return None
    if alias.status != BrokerEmailAlias.Status.ACTIVE:
        return None
    return alias.trading_account


def identities_for(to_addresses: Iterable[str], *, owner_user=None):
    """ALL ``BrokerEmailIdentity`` rows matching any of the given bare addresses (case-insensitive), de-duplicated.
    One email legitimately maps to MANY rows (unique is per email+broker), so this returns a list, never a single
    pk-ordered pick. Read-only. ``owner_user`` scopes to that member's own identities (cross-user firewall — see
    ``resolve_alias``)."""
    # Build the de-duplicated set of target addresses (both bare and gmail-canonical forms) from the headers.
    bare_targets, canon_targets = set(), set()
    for addr in to_addresses or ():
        bare = _bare_address(addr)
        if not bare:
            continue
        bare_targets.add(bare)
        canon_targets.add(canonical_address(bare))
    if not bare_targets:
        return []

    out, seen_pk = [], set()
    if owner_user is not None:
        # Owner-scoped path (the real ingestion path always passes owner_user): scan the owner's OWN identities (few)
        # and match by gmail-CANONICAL equality, so a header in any equivalent Google form matches an identity stored
        # in another. Owner-scoping is the cross-user firewall; canonicalisation never broadens beyond this owner.
        for identity in BrokerEmailIdentity.objects.filter(trading_account__user=owner_user):
            if canonical_address(identity.email) in canon_targets and identity.pk not in seen_pk:
                seen_pk.add(identity.pk)
                out.append(identity)
        return out
    # Legacy/global path (no owner): keep the exact bare-address match (no canonicalisation, no full-table scan).
    for bare in bare_targets:
        for identity in BrokerEmailIdentity.objects.filter(email__iexact=bare):
            if identity.pk not in seen_pk:
                seen_pk.add(identity.pk)
                out.append(identity)
    return out


def resolve_identity(to_addresses: Iterable[str]) -> Optional[BrokerEmailIdentity]:
    """A single matching identity for attribution/display (the first match). NOT used for account resolution — that
    is broker-blind and unsafe for a shared address; use :func:`resolve` which fails closed on ambiguity."""
    matches = identities_for(to_addresses)
    return matches[0] if matches else None


def resolve(to_addresses: Iterable[str], *, owner_user=None) -> Tuple[Optional[BrokerEmailAlias], object]:
    """Read-only resolution to (alias-or-None, account-or-None). The GuvFX opaque alias is tried first (ACTIVE ⇒ its
    account). Otherwise the account is resolved from broker-registration identities, FAIL-CLOSED on ambiguity:

    ``owner_user`` (SECURITY): when a message is ingested from a known mailbox, pass that mailbox's owner so BOTH the
    alias and the identity lookups are scoped to that member — attacker-controllable recipient headers (To/
    Delivered-To/X-Original-To) can then NEVER attribute the message to a different member's account.
      * gather the distinct accounts carried by the VERIFIED identities for these addresses;
      * EXACTLY ONE distinct verified account ⇒ resolve it;
      * ZERO ⇒ no account (nothing verified-and-bound yet);
      * MORE THAN ONE ⇒ UNRESOLVED → no account (never guess, never pick by pk, no cross-user leak).
    Resolution is deliberately broker-BLIND-SAFE: it never relies on row order and never lets a PENDING/other-broker
    row for the same address shadow or mis-select a verified one. A PENDING/UNRESOLVED/RETIRED identity and a
    PENDING/RETIRED alias both yield no account (routing/ownership must be verified first, §4)."""
    alias = resolve_alias(to_addresses, owner_user=owner_user)
    if alias is not None:
        return alias, resolve_account(alias)
    verified = [i for i in identities_for(to_addresses, owner_user=owner_user)
                if i.status == BrokerEmailIdentity.Status.VERIFIED and i.trading_account_id is not None]
    distinct_accounts = {i.trading_account_id for i in verified}
    if len(distinct_accounts) == 1:
        return None, verified[0].trading_account        # the unique verified account — safe
    return None, None                                    # 0 ⇒ none; >1 ⇒ ambiguous, fail closed
