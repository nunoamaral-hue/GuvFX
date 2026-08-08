"""ADR-0034 / M3b-2 Integration — the disposable-host certification composition (pure, secret-free, DARK).

This is the ONE place the certified modules are composed end-to-end for a disposable-host certification run:

    M1 Guarded Attach  ->  M3b-2 host adapter  ->  RawWorkspaceSnapshot  ->  M3b-1 producer
      ->  WorkspaceObservation  ->  M3a Workspace Manager  ->  WorkspaceDecision

It adds NO architecture and takes NO action: it observes (via the injected ``host``), derives a decision
(the pure M3a engine), and returns a SECRET-FREE certification dict. It never launches, never logs in, never
persists, never emits telemetry, never places an order. The real ``host`` (bound to M1 + a live ``mt5``) is
constructed by the operator command; this module is pure so it is unit-testable with a mock host + fake M1.

The emitted dict carries ONLY safe fields (booleans, the non-secret ``observed_trade_mode`` int, canonical
state/reason strings, and a *classification* of the target path — never the raw path, login, server, or any
credential).
"""
from hosted_workspace.agent import build_agent_snapshot
from hosted_workspace.manager import derive_workspace_decision
from hosted_workspace.producer import (
    DEFAULT_CLOCK_TOLERANCE_SECONDS,
    _is_number,
    build_workspace_observation,
)
from hosted_workspace.state_machine import WorkspaceReason

# The certification dict is an allow-list: ONLY these keys may ever be emitted (a test enforces it). No
# login / server / password / token / keyring / raw path can appear.
SAFE_FIELDS = (
    "workspace_id", "target_path_classification", "process_running", "ipc_available", "connected",
    "account_match", "trade_allowed", "fresh", "observed_trade_mode", "canonical_state", "reason",
    "transition_required", "execution_ready", "recovery_required",
)


def _path_segments(value):
    """Case-folded, separator-normalised path segments (Windows or POSIX), dropping empty and ``.`` segments.
    Kept token-based so classification never string-matches across a directory boundary."""
    norm = str(value).strip().lower().replace("\\", "/")
    return [seg for seg in norm.split("/") if seg not in ("", ".")]


def classify_target_path(path, *, allowed_prefixes):
    """Classify (never echo) a target terminal path for the certification output. Fail-closed:
    - ``forbidden`` if the path is missing/blank OR contains any ``..`` traversal segment (it could escape
      the disposable tree and resolve to a production / Customer-Zero terminal);
    - ``disposable_authorised`` only if some allow-listed prefix is a true ANCESTOR DIRECTORY of the path,
      matched segment-by-segment — a merely shared textual prefix (``disp`` vs ``disp-customer-zero`` or
      ``disposable_prod``) does NOT authorise;
    - ``unclassified`` otherwise (the command refuses to run on anything not ``disposable_authorised``).

    Matching is case-insensitive and separator-normalised so Windows/POSIX inputs classify identically.
    There is no separate denylist: the allow-list plus traversal rejection is the whole mechanism."""
    if not path or not str(path).strip():
        return "forbidden"
    segments = _path_segments(path)
    if ".." in segments:
        return "forbidden"  # never trust or OS-resolve an upward-escaping path
    for pref in (allowed_prefixes or []):
        pref_segments = _path_segments(pref)
        if not pref_segments or ".." in pref_segments:
            continue  # an empty prefix would match everything; a traversal prefix is not a trustworthy ancestor
        if segments[:len(pref_segments)] == pref_segments:
            return "disposable_authorised"
    return "unclassified"


def _safe_trade_mode(value):
    """Emit the observed demo/live classification int (0=DEMO, 2=REAL — NON-secret), else None. Rejects
    bool (mirrors the producer) and anything non-int so the field is never a misleading positive."""
    return value if (isinstance(value, int) and not isinstance(value, bool)) else None


def run_certification(host, spec, *, clock, previous_state, target_path_classification):
    """Compose the certified chain for a disposable-host certification run and return a SECRET-FREE dict.

    Observe (M3b-2 agent over the injected host + M1) -> WorkspaceObservation (M3b-1 producer) -> decide
    (M3a Manager). NO action is taken on the decision. This inlines ``agent.observe_workspace``'s exact two
    steps (a SINGLE guarded attach) so the intermediate ``RawWorkspaceSnapshot``'s non-secret
    ``observed_trade_mode`` can be reported as evidence — it must never trigger a second attach. The result
    contains only ``SAFE_FIELDS``.
    """
    snapshot = build_agent_snapshot(host, spec, clock=clock)
    now = snapshot.observed_at if _is_number(snapshot.observed_at) else 0.0
    observation = build_workspace_observation(
        snapshot, now=now, previous_state=str(previous_state), previous_reason=WorkspaceReason.NONE,
        clock_tolerance_seconds=DEFAULT_CLOCK_TOLERANCE_SECONDS)
    decision = derive_workspace_decision(observation)
    return {
        "workspace_id": str(getattr(spec, "workspace_id", "") or ""),
        "target_path_classification": str(target_path_classification),
        "process_running": bool(observation.process_running),
        "ipc_available": bool(observation.ipc_available),
        "connected": bool(observation.connected),
        "account_match": bool(observation.account_match),
        "trade_allowed": bool(observation.trade_allowed),
        "fresh": bool(observation.fresh),
        "observed_trade_mode": _safe_trade_mode(snapshot.observed_trade_mode),
        "canonical_state": str(decision.next_state),
        "reason": str(decision.reason),
        "transition_required": bool(decision.transition_required),
        "execution_ready": bool(decision.execution_ready),
        "recovery_required": bool(decision.recovery_required),
    }
