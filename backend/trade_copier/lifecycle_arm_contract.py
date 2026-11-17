"""trade_copier.lifecycle_arm_contract — the single, fail-closed source of truth for the governed
OPEN+MODIFY+FULL_CLOSE live-demo arming safety contract (GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148,
ADR-0114).

pkt-147 proved two blockers for the #46->#48 live demo:

  1. The account-46 immediate emitter has no ingest signing key, so the <=1s primary path cannot operate.
  2. ``provision_authorized_live_copier_bridge`` enables the OPEN flag stack but NOT the per-operation LIVE
     MODIFY/FULL_CLOSE execution flags, so a copied OPEN could be left with no executable CLOSE — a stranded
     real position on the live follower.

This module expresses the corrected contract as PURE, flag-/authority-level evaluation so the two consumers can
never disagree:

  * ``copier_live_demo_preflight`` (read-only, PRE-arm, execution flags still OFF) — the ``arm_readiness`` section
    verifies the AUTHORITY-layer contract + the readiness of the host/human gates, WITHOUT requiring the
    execution flags to be on yet (flags-off is the expected pre-arm posture).
  * ``trade_copier.lifecycle_arming`` (the atomic arm operation, ARM time, flags ON) — enforces the FLAG-layer
    coherence as well: a per-op LIVE order is armable ONLY when the authorization's per-op authority AND the per-op
    execution flag AND the mutation dispatch+transport all agree, mirroring ``trade_copier.live_execution`` parity.

Creates nothing, contacts no host/broker, holds/reads no secret. Every ambiguous case fails closed (refuse).

THE CENTRAL INVARIANT — no OPEN without an executable CLOSE. The bounded entry allowance (``max_orders``) always
grants entry, so to guarantee the follower can never be stranded the authorization MUST also carry
``full_close_authority`` (and, at arm time, the FULL_CLOSE execution path must be armed). OPEN is never permitted
for this live-demo route unless the authorized MODIFY and FULL_CLOSE lifecycle is also safely executable.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ── stable, machine-readable blocker codes ───────────────────────────────────────────────────────────────────
# authority layer (flag-independent; safe to evaluate pre-arm)
MODIFY_AUTHORITY_MISSING = "MODIFY_AUTHORITY_MISSING"
FULL_CLOSE_AUTHORITY_MISSING = "FULL_CLOSE_AUTHORITY_MISSING"
PARTIAL_CLOSE_NOT_PERMITTED = "PARTIAL_CLOSE_NOT_PERMITTED"
OPEN_WITHOUT_CLOSE_REFUSED = "OPEN_WITHOUT_CLOSE_REFUSED"
VOLUME_EXCEEDS_CAP = "VOLUME_EXCEEDS_CAP"
MAX_ORDERS_NOT_ONE = "MAX_ORDERS_NOT_ONE"
ROUTE_MISMATCH = "ROUTE_MISMATCH"
SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
# flag layer (requires the operator to have enabled the execution flags; evaluated at ARM time only)
MODIFY_EXECUTION_NOT_ARMABLE = "MODIFY_EXECUTION_NOT_ARMABLE"
FULL_CLOSE_EXECUTION_NOT_ARMABLE = "FULL_CLOSE_EXECUTION_NOT_ARMABLE"
OPEN_EXECUTION_NOT_ARMABLE = "OPEN_EXECUTION_NOT_ARMABLE"
PARTIAL_CLOSE_FLAG_ENABLED = "PARTIAL_CLOSE_FLAG_ENABLED"
LIFECYCLE_AUTHORITY_FLAG_MISMATCH = "LIFECYCLE_AUTHORITY_FLAG_MISMATCH"
# host / emitter / human gates
EMITTER_KEY_ABSENT = "EMITTER_KEY_ABSENT"
EMITTER_KEY_IDENTITY_INVALID = "EMITTER_KEY_IDENTITY_INVALID"
EMITTER_TASK_NOT_DISABLED = "EMITTER_TASK_NOT_DISABLED"
HOST_ADMISSION_NOT_OFF = "HOST_ADMISSION_NOT_OFF"
FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED = "FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED"
TEARDOWN_NOT_PROVEN = "TEARDOWN_NOT_PROVEN"
VERIFIER_WINDOW_NOT_READY = "VERIFIER_WINDOW_NOT_READY"   # ADR-0117: no OPEN ADR-0101 window covering INGEST for the whole authz window
AUTHZ_NONE = "AUTHZ_NONE"

# The bounded live-demo caps (preserved invariants — never widened by this packet).
DEMO_MAX_VOLUME = Decimal("0.01")
DEMO_MAX_ORDERS = 1

# The teardown command that must be installed for an arm to be safe to grant (so every arm has a proven path back
# to DARK). Verified by name only (presence), never executed, here.
TEARDOWN_COMMAND = "teardown_copier_live_dark"


def _as_decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def authority_contract_blockers(authz, *, source_symbol: str | None = None,
                                follower_symbol: str | None = None,
                                master_account_id: int | None = None,
                                follower_account_id: int | None = None) -> list[str]:
    """Flag-INDEPENDENT authority-layer contract (safe to evaluate pre-arm, execution flags OFF).

    The entry allowance always exists, so to prevent a stranded follower the authorization MUST carry
    ``full_close_authority`` (close the bound position) and ``modify_authority`` (SL/TP), and MUST NOT carry
    partial-close. Also enforces the bounded caps (<=0.01 volume, exactly one order) and, when the expected route
    / symbols are supplied, that the authorization matches them exactly. Returns a sorted, de-duplicated list of
    blocker codes ([] = authority contract satisfied). Fail-closed: a missing/None authz yields AUTHZ_NONE."""
    if authz is None:
        return [AUTHZ_NONE]
    b: list[str] = []
    if not bool(getattr(authz, "modify_authority", False)):
        b.append(MODIFY_AUTHORITY_MISSING)
    if not bool(getattr(authz, "full_close_authority", False)):
        # entry authority with no close authority == the stranded-position risk this packet exists to close.
        b.append(FULL_CLOSE_AUTHORITY_MISSING)
        b.append(OPEN_WITHOUT_CLOSE_REFUSED)
    if bool(getattr(authz, "partial_close_authority", False)):
        b.append(PARTIAL_CLOSE_NOT_PERMITTED)
    vol = _as_decimal(getattr(authz, "max_volume", None))
    if vol is None or vol > DEMO_MAX_VOLUME or vol <= 0:
        b.append(VOLUME_EXCEEDS_CAP)
    if int(getattr(authz, "max_orders", 0) or 0) != DEMO_MAX_ORDERS:
        b.append(MAX_ORDERS_NOT_ONE)
    if master_account_id is not None or follower_account_id is not None:
        if (int(getattr(authz, "master_account_id", 0) or 0) != int(master_account_id or 0)
                or int(getattr(authz, "follower_account_id", 0) or 0) != int(follower_account_id or 0)):
            b.append(ROUTE_MISMATCH)
    if source_symbol is not None or follower_symbol is not None:
        if (str(getattr(authz, "source_symbol", "")) != str(source_symbol or "")
                or str(getattr(authz, "follower_symbol", "")) != str(follower_symbol or "")):
            b.append(SYMBOL_MISMATCH)
    return sorted(set(b))


def execution_flag_blockers(authz) -> list[str]:
    """Flag-LAYER coherence — valid ONLY at ARM time, after the operator has enabled the execution flags.

    Mirrors ``trade_copier.live_execution`` parity exactly: a per-op LIVE order needs BOTH the authorization's
    per-op authority AND the per-op execution flag AND the mutation dispatch+transport. Verifies authority<->flag
    agreement explicitly (never infers one from the other), enforces the partial-close exclusion at the flag
    layer, and enforces the central no-OPEN-without-executable-CLOSE invariant. [] = coherent and armable."""
    from trade_copier import flags as cf
    if authz is None:
        return [AUTHZ_NONE]
    b: list[str] = []
    dispatch = bool(cf.copier_mutation_dispatch_enabled())
    transport = bool(cf.copier_mutation_transport_enabled())
    open_armed = bool(cf.copier_live_execute_enabled())
    modify_flag = bool(cf.copier_live_modify_enabled())
    full_close_flag = bool(cf.copier_live_full_close_enabled())
    partial_flag = bool(cf.copier_live_partial_close_enabled())
    modify_armed = modify_flag and dispatch and transport
    full_close_armed = full_close_flag and dispatch and transport

    if not open_armed:
        b.append(OPEN_EXECUTION_NOT_ARMABLE)
    # authority present => the matching execution PATH must be fully armed.
    if bool(authz.modify_authority) and not modify_armed:
        b.append(MODIFY_EXECUTION_NOT_ARMABLE)
    if bool(authz.full_close_authority) and not full_close_armed:
        b.append(FULL_CLOSE_EXECUTION_NOT_ARMABLE)
    # a flag enabled WITHOUT the matching authority is an incoherent, unaudited arm (refuse, never infer).
    if modify_flag and not bool(authz.modify_authority):
        b.append(LIFECYCLE_AUTHORITY_FLAG_MISMATCH)
    if full_close_flag and not bool(authz.full_close_authority):
        b.append(LIFECYCLE_AUTHORITY_FLAG_MISMATCH)
    # partial-close is excluded for this demo — neither the authority nor the flag may be on.
    if partial_flag:
        b.append(PARTIAL_CLOSE_FLAG_ENABLED)
    # THE central invariant at the flag layer: OPEN executable while FULL_CLOSE is not == stranded-position risk.
    if open_armed and not full_close_armed:
        b.append(OPEN_WITHOUT_CLOSE_REFUSED)
    return sorted(set(b))


def teardown_available() -> bool:
    """True iff the proven DARK teardown command is installed (presence by name only, never executed)."""
    try:
        from django.core.management import get_commands
        return TEARDOWN_COMMAND in get_commands()
    except Exception:  # noqa: BLE001 — fail closed
        return False


def evaluate_arm_readiness(authz, *, emitter_key_ready, follower_flat_confirmed,
                           host_admission_off, emitter_task_disabled,
                           require_flag_coherence: bool, verifier_window_ready=None,
                           source_symbol: str | None = None, follower_symbol: str | None = None,
                           master_account_id: int | None = None,
                           follower_account_id: int | None = None) -> dict:
    """Aggregate the whole arming-readiness contract into a single fail-closed verdict.

    ``emitter_key_ready`` / ``host_admission_off`` / ``emitter_task_disabled`` / ``follower_flat_confirmed`` are
    TRI-STATE: ``True`` = verified good, ``False`` = verified bad, ``None`` = unverified (host-probe/human-gated).
    Only an explicit ``True`` clears the gate; ``False`` and ``None`` both keep ``arm_ready`` False (fail-closed),
    so neither a bad reading nor an absent reading can ever yield ARM-ready.

    ``require_flag_coherence`` is False for the read-only pre-arm preflight (flags legitimately OFF) and True for
    the atomic arm operation (flags must be ON and coherent)."""
    blockers: list[str] = []
    blockers += authority_contract_blockers(
        authz, source_symbol=source_symbol, follower_symbol=follower_symbol,
        master_account_id=master_account_id, follower_account_id=follower_account_id)
    if require_flag_coherence and authz is not None:
        blockers += execution_flag_blockers(authz)
        # ADR-0117: at ARM time the signed emitter's events are verifiable only inside an OPEN ADR-0101 window
        # covering INGEST that outlives the authorization. A tri-state (True clears; False/None keep it blocked).
        if verifier_window_ready is not True:
            blockers.append(VERIFIER_WINDOW_NOT_READY)
    if emitter_key_ready is not True:
        blockers.append(EMITTER_KEY_ABSENT if emitter_key_ready is None or emitter_key_ready is False
                        else EMITTER_KEY_IDENTITY_INVALID)
    if follower_flat_confirmed is not True:
        blockers.append(FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED)
    if host_admission_off is not True:
        blockers.append(HOST_ADMISSION_NOT_OFF)
    if emitter_task_disabled is not True:
        blockers.append(EMITTER_TASK_NOT_DISABLED)
    if not teardown_available():
        blockers.append(TEARDOWN_NOT_PROVEN)
    blockers = sorted(set(blockers))
    return {"arm_ready": not blockers, "blockers": blockers,
            "evaluated": {"require_flag_coherence": bool(require_flag_coherence),
                          "emitter_key_ready": emitter_key_ready,
                          "follower_flat_confirmed": follower_flat_confirmed,
                          "host_admission_off": host_admission_off,
                          "emitter_task_disabled": emitter_task_disabled,
                          "verifier_window_ready": verifier_window_ready,
                          "teardown_available": teardown_available()}}
