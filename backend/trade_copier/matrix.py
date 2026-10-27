"""trade_copier.matrix — directional master→follower instrument mapping matrix.

A READ-ONLY discovery layer. For an ORDERED ``(master, follower)`` account pair it enumerates the master's
ELIGIBLE source instruments (enabled, tradable, not expired — a strict subset of the raw catalogue),
resolves each to a canonical economic identity, finds the follower's instrument(s) of the SAME canonical,
and classifies the candidate:

  VERIFIED   — an explicit VERIFIED ``CopierSymbolMap`` row exists for the exact broker+symbols (execution
               authority). The matrix REFLECTS it; it never authors it.
  UNVERIFIED — a single follower match whose canonical IDENTITY matches and whose hard dimensions (quote
               currency, settlement currency, contract type) are compatible, but no verified map yet.
  AMBIGUOUS  — more than one follower match, OR a material compatibility warning on a HARD dimension
               (settlement/quote currency or contract type). FAIL CLOSED — never auto-verified. Note a
               settlement-currency warning means "verify carefully", NOT "the canonical identity is wrong".
  STALE      — the master or follower catalogue is older than the staleness threshold.
  INELIGIBLE — the follower symbol(s) of this canonical are not tradable (disabled / close-only / expired).
  ABSENT     — the follower has no instrument of the master's canonical (or the master canonical is
               unresolved, so no counterpart can be formed).

Counts reconcile deterministically: the six classifications sum to ``eligible_source`` (every eligible
master instrument gets exactly one). ``raw_catalogue_count`` (the full broker catalogue, incl. disabled/
non-tradable/expired) is reported SEPARATELY and is never the coverage denominator.

Boundaries (packet + ``.claude/rules/architecture``): NOT on the execution path (translation stays in
``symbols.resolve_follower_symbol`` / ``CopierSymbolMap``); ``VERIFIED`` is reflected from ``CopierSymbolMap``,
never authored; directionality is explicit (reverse never inferred as verified); candidates are COMPUTED on
read (no write) — the ``InstrumentMapping`` model is the durable audit record, populated only by the refresh
command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

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

# HARD dimensions whose master/follower difference makes a pairing incompatible → fail closed (AMBIGUOUS).
# Settlement/quote currency and contract type are economic incompatibilities. Contract size and margin
# currency differences are MATERIAL WARNINGS (surfaced, exposure-relevant) but do not by themselves block,
# because they are routinely reconciled by per-follower sizing — never by silently equating the symbols.
_HARD_INCOMPATIBILITY_DIMENSIONS = ("quote_currency", "settlement_currency", "contract_type")


def is_safe_exact(candidate) -> bool:
    """The governed SAFE-EXACT predicate for reviewer-approvable discovery persistence. DETERMINISTIC and
    FAIL-CLOSED: true ONLY for a single, clean, exact cross-broker match.

    Requires ALL of:
      * ``classification == UNVERIFIED`` — which already guarantees a SINGLE tradable follower match whose
        canonical identity matches and whose HARD dimensions (quote/settlement currency, contract type) are
        compatible, and that the pair is not VERIFIED/AMBIGUOUS/STALE/INELIGIBLE/ABSENT;
      * ``compatibility_warnings`` is EMPTY — no SOFT material warning either (e.g. a contract_size or
        margin_currency difference), so the economic exposure matches exactly;
      * ``identity_confidence == CONF_HIGH`` — the canonical identity was read with high confidence;
      * a non-empty ``follower_symbol`` — a concrete counterpart exists.

    Any ambiguity, hard-dimension conflict, material warning, ineligibility, absence or stale data makes this
    FALSE. It is purely a function of the computed candidate — it reads no DB and grants no authority."""
    return (candidate.classification == UNVERIFIED
            and not candidate.compatibility_warnings
            and candidate.identity_confidence == CONF_HIGH
            and bool(candidate.follower_symbol))


def _economics(res) -> dict:
    cs = res.contract_size
    return {
        "underlying": res.underlying,
        "quote_currency": res.quote_currency,
        "settlement_currency": res.settlement_currency,
        "margin_currency": res.margin_currency,
        "contract_type": res.contract_type,
        "contract_size": (str(cs) if cs is not None else None),
        "expiry": res.expiry,
        "identity_confidence": res.identity_confidence,
    }


def _one_diff(m, f) -> dict:
    return {"master": m, "follower": f, "differs": bool(m is not None and f is not None and m != f)}


def _material_differences(mres, fres):
    """Per-dimension master↔follower comparison + the compatibility-warning list. Economic dimensions are
    kept SEPARATE: a settlement-currency difference is a warning, not a claim that the canonical is wrong."""
    mc = str(mres.contract_size) if mres.contract_size is not None else None
    fc = str(fres.contract_size) if fres.contract_size is not None else None
    diffs = {
        "quote_currency": _one_diff(mres.quote_currency, fres.quote_currency),
        "settlement_currency": _one_diff(mres.settlement_currency, fres.settlement_currency),
        "margin_currency": _one_diff(mres.margin_currency, fres.margin_currency),
        "contract_type": _one_diff(mres.contract_type, fres.contract_type),
        "contract_size": _one_diff(mc, fc),
    }
    warnings = [dim for dim, d in diffs.items() if d["differs"]]
    return diffs, warnings


def _is_hard_incompatible(diffs) -> bool:
    return any(diffs[d]["differs"] for d in _HARD_INCOMPATIBILITY_DIMENSIONS)


@dataclass
class Candidate:
    master_symbol: str
    follower_symbol: str
    canonical_slug: Optional[str]
    canonical_display: str
    classification: str
    reason: str
    identity_confidence: str
    master_economics: dict = field(default_factory=dict)
    follower_economics: dict = field(default_factory=dict)
    material_differences: dict = field(default_factory=dict)
    compatibility_warnings: list = field(default_factory=list)
    verified_at: Optional[str] = None
    verified_source: Optional[str] = None


@dataclass
class DirectionMatrix:
    master_account_id: int
    master_broker: str
    follower_account_id: int
    follower_broker: str
    raw_catalogue_count: int
    eligible_source: int
    counts: dict
    coverage_pct: Optional[float]
    candidates: list


@dataclass
class MatrixView:
    accounts: list
    directions: list


def _verified_map_row(mkey: str, msym: str, fkey: str):
    return (CopierSymbolMap.objects
            .filter(master_broker__iexact=mkey, master_symbol__iexact=msym,
                    follower_broker__iexact=fkey, verified=True)
            .first())


def _eligible(mi) -> bool:
    return bool(mi.enabled) and is_tradable(mi.metadata) and not is_expired(mi.metadata)


def generate_candidates(master_account, follower_account) -> DirectionMatrix:
    """Compute the ordered ``master → follower`` candidate list + reconciling counts. Read-only."""
    mkey, fkey = broker_key(master_account), broker_key(follower_account)
    master_stale = catalogue_state(master_account)["state"] == "stale"
    follower_stale = catalogue_state(follower_account)["state"] == "stale"

    master_all = list(BrokerInstrument.objects.filter(account=master_account))
    follower_instruments = list(BrokerInstrument.objects.filter(account=follower_account))
    master_eligible = [mi for mi in master_all if _eligible(mi)]

    follower_by_canon: dict = {}
    for fi in follower_instruments:
        fres = resolve_canonical(fi.broker_symbol, fi.metadata)
        if fres.key is not None:
            follower_by_canon.setdefault(fres.key.slug, []).append((fi, fres))

    counts = {k: 0 for k in _CLASSIFICATIONS}
    candidates: list = []

    def add(c: Candidate):
        candidates.append(c)
        counts[c.classification] += 1

    for mi in master_eligible:
        msym = mi.broker_symbol
        mres = resolve_canonical(msym, mi.metadata)
        meco = _economics(mres)

        # 1) Execution authority first — reflect a verified CopierSymbolMap row exactly.
        vrow = _verified_map_row(mkey, msym, fkey)
        if vrow is not None:
            fi = next((x for x in follower_instruments if x.broker_symbol == vrow.follower_symbol), None)
            fres = resolve_canonical(vrow.follower_symbol, fi.metadata if fi else {})
            diffs, warnings = _material_differences(mres, fres)
            add(Candidate(
                master_symbol=msym, follower_symbol=vrow.follower_symbol,
                canonical_slug=(mres.key.slug if mres.key else None), canonical_display=mres.display_name,
                classification=VERIFIED, reason="verified_symbol_map", identity_confidence=mres.identity_confidence,
                master_economics=meco, follower_economics=_economics(fres),
                material_differences=diffs, compatibility_warnings=warnings,
                verified_at=(vrow.verified_at.isoformat() if vrow.verified_at else None),
                verified_source=(str(vrow.verified_by_id) if vrow.verified_by_id else None)))
            continue

        # 2) Stale catalogue — cannot trust constraints → fail closed.
        if master_stale or follower_stale:
            add(Candidate(msym, "", (mres.key.slug if mres.key else None), mres.display_name, STALE,
                          ("master_catalogue_stale" if master_stale else "follower_catalogue_stale"),
                          mres.identity_confidence, master_economics=meco))
            continue

        # 3) Master canonical unresolved → no counterpart can be formed.
        if mres.key is None:
            add(Candidate(msym, "", None, mres.display_name, ABSENT, "master_canonical_unresolved",
                          mres.identity_confidence, master_economics=meco))
            continue

        matches = follower_by_canon.get(mres.key.slug, [])
        if not matches:
            add(Candidate(msym, "", mres.key.slug, mres.display_name, ABSENT,
                          "follower_has_no_canonical_match", mres.identity_confidence, master_economics=meco))
            continue

        tradable = [(fi, fres) for (fi, fres) in matches
                    if is_tradable(fi.metadata) and not is_expired(fi.metadata)]
        if not tradable:
            fi0 = matches[0][0]
            add(Candidate(msym, fi0.broker_symbol, mres.key.slug, mres.display_name, INELIGIBLE,
                          "follower_symbol_not_tradable_or_expired", mres.identity_confidence,
                          master_economics=meco))
            continue

        if len(tradable) > 1:
            add(Candidate(msym, "|".join(sorted(fi.broker_symbol for fi, _ in tradable)), mres.key.slug,
                          mres.display_name, AMBIGUOUS, "multiple_follower_matches",
                          mres.identity_confidence, master_economics=meco))
            continue

        fi, fres = tradable[0]
        diffs, warnings = _material_differences(mres, fres)
        feco = _economics(fres)
        # Canonical IDENTITY matches (same key) — the cross-pair decision is a SEPARATE compatibility
        # question. A hard-dimension difference (settlement/quote currency, contract type) fails closed as
        # a compatibility WARNING; it does not mean the XAU/USD identity is wrong.
        if _is_hard_incompatible(diffs):
            add(Candidate(msym, fi.broker_symbol, mres.key.slug, mres.display_name, AMBIGUOUS,
                          "material_compatibility_warning", mres.identity_confidence,
                          master_economics=meco, follower_economics=feco,
                          material_differences=diffs, compatibility_warnings=warnings))
            continue

        add(Candidate(msym, fi.broker_symbol, mres.key.slug, mres.display_name, UNVERIFIED,
                      "needs_explicit_verification", mres.identity_confidence,
                      master_economics=meco, follower_economics=feco,
                      material_differences=diffs, compatibility_warnings=warnings))

    eligible = len(master_eligible)
    assert sum(counts.values()) == eligible, "classification counts must reconcile to eligible_source"
    coverage = round(100.0 * counts[VERIFIED] / eligible, 1) if eligible else None
    return DirectionMatrix(
        master_account_id=master_account.id, master_broker=mkey,
        follower_account_id=follower_account.id, follower_broker=fkey,
        raw_catalogue_count=len(master_all), eligible_source=eligible,
        counts=counts, coverage_pct=coverage, candidates=candidates,
    )


def compute_matrix(accounts) -> MatrixView:
    """Compute every ORDERED pair among ``accounts`` (owner-scoped by the caller). Read-only."""
    acct_list = list(accounts)
    account_rows = [{"account_id": a.id, "broker": broker_key(a), "catalogue": catalogue_state(a)}
                    for a in acct_list]
    directions = [generate_candidates(m, f) for m in acct_list for f in acct_list if m.id != f.id]
    return MatrixView(accounts=account_rows, directions=directions)
