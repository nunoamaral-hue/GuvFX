"""trade_copier.matrix — directional master→follower instrument mapping matrix.

A READ-ONLY discovery layer. For an ORDERED ``(master, follower)`` account pair it enumerates the
master's eligible source instruments (from the synced catalogue), resolves each to a canonical economic
identity, finds the follower's instrument(s) of the SAME canonical, and classifies the candidate:

  VERIFIED   — an explicit VERIFIED ``CopierSymbolMap`` row exists for the exact broker+symbols (the
               execution authority). The matrix REFLECTS it; it never authors it.
  UNVERIFIED — a single, compatible follower match exists but no verified map row yet (needs human verify).
  AMBIGUOUS  — more than one follower match, OR a material incompatibility (settlement-currency or
               contract-type mismatch, or a low-confidence canonical). FAIL CLOSED — never auto-verified.
  STALE      — the master or follower catalogue is older than the staleness threshold.
  INELIGIBLE — the follower symbol(s) of this canonical are not tradable (disabled / close-only / expired).
  ABSENT     — the follower has NO instrument of the master's canonical (or the master symbol's canonical
               could not be resolved, so no counterpart can be formed).

Boundaries (packet + ``.claude/rules/architecture``):
  * This layer is NOT on the execution path. Execution symbol translation stays in
    ``trade_copier.symbols.resolve_follower_symbol`` (``CopierSymbolMap`` only). ``VERIFIED`` here is
    derived from ``CopierSymbolMap`` — a discovery mirror, never a second write-path for execution.
  * Directionality is explicit: ``46→48`` and ``48→46`` are computed independently; a reverse direction is
    NEVER inferred as verified from its forward counterpart.
  * Candidates are COMPUTED from stored catalogue + maps on read (no write). The ``InstrumentMapping`` model
    is the durable audit record, populated only by ``refresh_instrument_mappings`` (a later, gated phase).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from django.utils import timezone

from execution.models import BrokerInstrument
from trade_copier.canonical import CONF_HIGH, resolve_canonical
from trade_copier.catalogue import catalogue_state, is_expired, is_tradable
from trade_copier.models import CopierSymbolMap
from trade_copier.symbols import broker_key

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
AMBIGUOUS = "AMBIGUOUS"
STALE = "STALE"
INELIGIBLE = "INELIGIBLE"
ABSENT = "ABSENT"

_CLASSIFICATIONS = (VERIFIED, UNVERIFIED, AMBIGUOUS, STALE, INELIGIBLE, ABSENT)


@dataclass
class Candidate:
    master_symbol: str
    follower_symbol: str
    canonical_slug: Optional[str]
    canonical_display: str
    classification: str
    reason: str
    confidence: str
    material_differences: dict = field(default_factory=dict)
    verified_at: Optional[str] = None
    verified_source: Optional[str] = None  # the verifier (id), audit-only


@dataclass
class DirectionMatrix:
    master_account_id: int
    master_broker: str
    follower_account_id: int
    follower_broker: str
    eligible_source: int
    counts: dict
    coverage_pct: Optional[float]
    candidates: list


@dataclass
class MatrixView:
    accounts: list      # [{account_id, broker, catalogue:{...}}]
    directions: list    # [DirectionMatrix, ...]


def _material_differences(mres, fres, m_meta: dict, f_meta: dict) -> dict:
    """Surface the economic differences between a master symbol and its follower counterpart. Preserved,
    not folded away — a 1.0 lot of XAUUSD (contract 100) ≠ 1.0 lot of XAUUSD_SB (contract 1)."""
    out: dict = {}
    mc, fc = mres.contract_size, fres.contract_size
    out["contract_size"] = {"master": (str(mc) if mc is not None else None),
                            "follower": (str(fc) if fc is not None else None),
                            "differs": bool(mc is not None and fc is not None and mc != fc)}
    ms, fs = mres.settlement_currency, fres.settlement_currency
    out["settlement_currency"] = {"master": ms, "follower": fs, "differs": bool(ms and fs and ms != fs)}
    out["quote_currency"] = {"master": mres.quote_currency, "follower": fres.quote_currency,
                            "differs": bool(mres.quote_currency and fres.quote_currency
                                            and mres.quote_currency != fres.quote_currency)}
    out["contract_type"] = {"master": mres.contract_type, "follower": fres.contract_type,
                            "differs": bool(mres.contract_type != fres.contract_type)}
    return out


def _verified_map_row(mkey: str, msym: str, fkey: str):
    """The execution-authority verified ``CopierSymbolMap`` row for this (master_broker, master_symbol,
    follower_broker), if any. The matrix reflects it exactly (same follower_symbol)."""
    return (CopierSymbolMap.objects
            .filter(master_broker__iexact=mkey, master_symbol__iexact=msym,
                    follower_broker__iexact=fkey, verified=True)
            .first())


def generate_candidates(master_account, follower_account) -> DirectionMatrix:
    """Compute the ordered ``master → follower`` candidate list + classification counts. Read-only."""
    mkey, fkey = broker_key(master_account), broker_key(follower_account)
    m_state = catalogue_state(master_account)
    f_state = catalogue_state(follower_account)
    master_stale = m_state["state"] == "stale"
    follower_stale = f_state["state"] == "stale"

    master_instruments = list(BrokerInstrument.objects.filter(account=master_account, enabled=True))
    follower_instruments = list(BrokerInstrument.objects.filter(account=follower_account))

    # Pre-resolve the follower catalogue by canonical slug for O(1) lookup.
    follower_by_canon: dict = {}
    for fi in follower_instruments:
        fres = resolve_canonical(fi.broker_symbol, fi.metadata)
        if fres.key is not None:
            follower_by_canon.setdefault(fres.key.slug, []).append((fi, fres))

    counts = {k: 0 for k in _CLASSIFICATIONS}
    candidates: list = []

    for mi in master_instruments:
        msym = mi.broker_symbol
        mres = resolve_canonical(msym, mi.metadata)

        # 1) Execution authority first: a verified CopierSymbolMap row is reflected as VERIFIED exactly.
        vrow = _verified_map_row(mkey, msym, fkey)
        if vrow is not None:
            fi = next((x for x in follower_instruments if x.broker_symbol == vrow.follower_symbol), None)
            fres = resolve_canonical(vrow.follower_symbol, fi.metadata if fi else {})
            md = _material_differences(mres, fres, mi.metadata or {}, (fi.metadata if fi else {}) or {})
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol=vrow.follower_symbol,
                canonical_slug=(mres.key.slug if mres.key else None), canonical_display=mres.display_name,
                classification=VERIFIED, reason="verified_symbol_map", confidence=mres.confidence,
                material_differences=md,
                verified_at=(vrow.verified_at.isoformat() if vrow.verified_at else None),
                verified_source=(str(vrow.verified_by_id) if vrow.verified_by_id else None)))
            counts[VERIFIED] += 1
            continue

        # 2) Stale catalogue — cannot trust constraints → fail closed as STALE.
        if master_stale or follower_stale:
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol="",
                canonical_slug=(mres.key.slug if mres.key else None), canonical_display=mres.display_name,
                classification=STALE, reason=("master_catalogue_stale" if master_stale else "follower_catalogue_stale"),
                confidence=mres.confidence))
            counts[STALE] += 1
            continue

        # 3) Master canonical unresolved → no counterpart can be formed.
        if mres.key is None:
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol="", canonical_slug=None,
                canonical_display=mres.display_name, classification=ABSENT,
                reason="master_canonical_unresolved", confidence=mres.confidence))
            counts[ABSENT] += 1
            continue

        matches = follower_by_canon.get(mres.key.slug, [])
        if not matches:
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol="", canonical_slug=mres.key.slug,
                canonical_display=mres.display_name, classification=ABSENT,
                reason="follower_has_no_canonical_match", confidence=mres.confidence))
            counts[ABSENT] += 1
            continue

        tradable = [(fi, fres) for (fi, fres) in matches
                    if is_tradable(fi.metadata) and not is_expired(fi.metadata)]
        if not tradable:
            fi0 = matches[0][0]
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol=fi0.broker_symbol, canonical_slug=mres.key.slug,
                canonical_display=mres.display_name, classification=INELIGIBLE,
                reason="follower_symbol_not_tradable_or_expired", confidence=mres.confidence))
            counts[INELIGIBLE] += 1
            continue

        if len(tradable) > 1:
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol="|".join(sorted(fi.broker_symbol for fi, _ in tradable)),
                canonical_slug=mres.key.slug, canonical_display=mres.display_name, classification=AMBIGUOUS,
                reason="multiple_follower_matches", confidence=mres.confidence))
            counts[AMBIGUOUS] += 1
            continue

        fi, fres = tradable[0]
        md = _material_differences(mres, fres, mi.metadata or {}, fi.metadata or {})
        incompatible = (
            mres.confidence != CONF_HIGH or fres.confidence != CONF_HIGH
            or md["settlement_currency"]["differs"] or md["contract_type"]["differs"]
            or md["quote_currency"]["differs"]
        )
        if incompatible:
            candidates.append(Candidate(
                master_symbol=msym, follower_symbol=fi.broker_symbol, canonical_slug=mres.key.slug,
                canonical_display=mres.display_name, classification=AMBIGUOUS,
                reason="material_incompatibility", confidence=mres.confidence, material_differences=md))
            counts[AMBIGUOUS] += 1
            continue

        # Clean, compatible, single match — but still requires EXPLICIT verification before execution.
        candidates.append(Candidate(
            master_symbol=msym, follower_symbol=fi.broker_symbol, canonical_slug=mres.key.slug,
            canonical_display=mres.display_name, classification=UNVERIFIED,
            reason="needs_explicit_verification", confidence=mres.confidence, material_differences=md))
        counts[UNVERIFIED] += 1

    eligible = len(master_instruments)
    coverage = round(100.0 * counts[VERIFIED] / eligible, 1) if eligible else None
    return DirectionMatrix(
        master_account_id=master_account.id, master_broker=mkey,
        follower_account_id=follower_account.id, follower_broker=fkey,
        eligible_source=eligible, counts=counts, coverage_pct=coverage, candidates=candidates,
    )


def compute_matrix(accounts) -> MatrixView:
    """Compute every ORDERED pair among ``accounts`` (a list of TradingAccount). Read-only.

    ``accounts`` is expected to be already owner-scoped by the caller (the API passes the caller's own
    current live accounts), so this never crosses a tenant boundary."""
    acct_list = list(accounts)
    account_rows = [{
        "account_id": a.id, "broker": broker_key(a), "catalogue": catalogue_state(a),
    } for a in acct_list]
    directions = [generate_candidates(m, f) for m in acct_list for f in acct_list if m.id != f.id]
    return MatrixView(accounts=account_rows, directions=directions)
