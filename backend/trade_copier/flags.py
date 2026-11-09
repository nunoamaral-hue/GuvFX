"""trade_copier.flags — DARK-by-default feature gates for the MT5 Master/Follower Copier.

Idiom B (settings-first-then-env, tolerant parser, read LIVE) — identical shape to
``hosted_workspace.flags`` / ``billing.beta``. Every accessor defaults OFF (empty-string default is
falsey) and is read live so a Sponsor can toggle without a restart. The whole copier ships DARK:
nothing observes, replicates, or executes until a Sponsor flips the lever.

LIVE follower execution is NOT governed here. It stays blocked by the execution subsystem's existing
five-layer LIVE wall (``execution.live_authz.live_execution_permitted``) and the demo-only bridge
close/modify handlers (ADR-0051). No copier flag can enable a real-money order: the copier only ever
*observes* (PR A) or creates SHADOW/DEMO ``ExecutionJob`` rows (PR B) that those gates continue to
govern. A copier flag is always ANDed with — never ORs past — the execution gates.
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    """Settings override wins, else env; empty/falsey default = OFF. Tolerant token set."""
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def copier_enabled() -> bool:
    """Master gate for the whole copier subsystem. DEFAULT OFF."""
    return _flag("COPIER_ENABLED")


def copier_observer_enabled() -> bool:
    """Gate for the read-only master observation pass (PR A). DEFAULT OFF.

    ANDed with the master gate: the observer runs only when BOTH ``COPIER_ENABLED`` and
    ``COPIER_OBSERVER_ENABLED`` are on, so the subsystem cannot half-activate."""
    return copier_enabled() and _flag("COPIER_OBSERVER_ENABLED")


def copier_replication_enabled() -> bool:
    """Gate for the replication INTENT engine (PR B). DEFAULT OFF, ANDed with the master gate.

    This gates recording of replication INTENTS only — PR B creates no ExecutionJobs and performs no
    execution. Any future gated DEMO execution-job creation sits behind a SEPARATE, additional flag
    and is never enabled by this one."""
    return copier_enabled() and _flag("COPIER_REPLICATION_ENABLED")


def copier_ui_enabled() -> bool:
    """Gate for the customer-facing read-only Copy Trading UI + its read API (PR C). DEFAULT OFF.

    Independent of the observer/replication gates so the monitoring UI can be shown (read-only) without
    arming any observation or execution. When OFF the read API reports enabled:false and returns no
    rows, and the frontend hides the Copy Trading entry — nothing is exposed in production until armed."""
    return _flag("COPIER_UI_ENABLED")


def copier_ui_cohort_allowlist() -> frozenset:
    """Founder/operator COHORT allowlist for the read-only Copy Trading UI (default-DENY). Lowercased
    emails parsed from ``COPIER_UI_COHORT_ALLOWLIST`` (comma-separated), settings-override-then-env.
    Plain config, never a secret/token. Empty/unset ⇒ no user is in the cohort (UI dark for everyone)."""
    raw = getattr(settings, "COPIER_UI_COHORT_ALLOWLIST", None)
    if raw is None:
        raw = os.getenv("COPIER_UI_COHORT_ALLOWLIST", "")
    return frozenset(e.strip().lower() for e in str(raw).split(",") if e.strip())


def copier_ui_enabled_for(user) -> bool:
    """OWNER/FOUNDER-SCOPED gate for the read-only Copy Trading UI — the SOLE authorization boundary,
    default-DENY. A user sees the monitoring UI ONLY when their email is in
    ``copier_ui_cohort_allowlist()``. There is deliberately NO global-flag path: the legacy global
    ``copier_ui_enabled()`` is NOT consulted here, so a global boolean can never expose the UI to the
    whole member base (the broker_intelligence multi-mailbox idiom). An empty/unset allowlist therefore
    denies everyone, and a missing/anonymous user is denied (fail-closed). This gate governs VISIBILITY of
    read-only monitoring data only — it grants NO observation/replication/execution authority whatsoever."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (getattr(user, "email", "") or "").strip().lower() in copier_ui_cohort_allowlist()


def copier_execute_enabled() -> bool:
    """Master gate for the B-exec DEMO ExecutionJob PRODUCER. DEFAULT OFF, ANDed with the master gate.

    BUILD != ARM: enabling this lets the producer run, but it creates SUPPRESSED PLACE_ORDER_SHADOW
    jobs (no order) unless ``copier_execute_demo_armed()`` is ALSO on. Even then, DEMO followers only —
    never LIVE, never bypassing the demo-only bridge close/modify walls, kill switch, pin or readiness."""
    return copier_enabled() and _flag("COPIER_EXECUTE_ENABLED")


def copier_execute_demo_armed() -> bool:
    """When on (AND execute enabled), the producer creates REAL DEMO ``PLACE_ORDER`` jobs instead of
    suppressed SHADOW jobs — DEMO followers ONLY. DEFAULT OFF. Arming is a separate operator-gated step:
    building the producer does NOT authorize arming it, and it never reaches a LIVE destination."""
    return copier_execute_enabled() and _flag("COPIER_EXECUTE_DEMO_ARMED")


def copier_live_execute_enabled() -> bool:
    """The explicit LIVE-copier ARM flag (ADR-0057). DEFAULT OFF, ANDed with the execute master gate.

    This is the ONLY flag that can let the DEMO-only producer admit a LIVE (non-demo) follower, and even
    then ONLY through the complete fail-closed LIVE-copier authorization admission
    (``trade_copier.live_execution.admit_open_live``) — never as a broad "allow live" bypass. It is ANDed
    with ``copier_execute_enabled()`` (hence with the master gate), so the whole stack
    (``COPIER_ENABLED`` + ``COPIER_EXECUTE_ENABLED`` + ``COPIER_LIVE_EXECUTE_ENABLED``) must be on before a
    single LIVE order is even considered, and it is distinct from the DEMO arm
    (``COPIER_EXECUTE_DEMO_ARMED``): a LIVE destination is never reachable by arming DEMO, and vice versa.
    Flipping it OFF is one of the two independent emergency-disarm paths (the other is the durable
    ``CopierLiveKillSwitch``)."""
    return copier_execute_enabled() and _flag("COPIER_LIVE_EXECUTE_ENABLED")


def copier_reconcile_enabled() -> bool:
    """Gate for the follower-ticket reconciliation pass (ADR-0052). DEFAULT OFF, ANDed with the master gate.

    Reconciliation is READ-ONLY: it reads a follower's live positions through the identity firewall and
    records which broker ticket a copied OPEN produced (``CopierReplication.follower_ticket``). It never
    places, closes or modifies an order. It is the prerequisite for any follower-position-targeting
    lifecycle op (CLOSE/MODIFY), which must never act by symbol resemblance."""
    return copier_enabled() and _flag("COPIER_RECONCILE_ENABLED")


def copier_lifecycle_enabled() -> bool:
    """Gate for the per-master-ticket lifecycle projection (ADR-0071 hybrid change propagation). DEFAULT OFF,
    ANDed with the master gate.

    The lifecycle projection is a READ-ONLY, DARK decision/record layer built ON TOP of the existing
    ``MasterEvent`` ledger and the reconcile output. It derives the explicit per-master-ticket state machine
    (OBSERVED_OPEN → ENTRY_PENDING → LINKED → MODIFY/CLOSE_PENDING → CLOSED, with ENTRY_FAILED_FINAL /
    CONTAINED_FAILURE terminal), enforces the entry-admissibility deadline (no late-recovery entry), the
    ordering contract (close supersedes a queued modify) and stale/out-of-order rejection, and records
    latency/failure signals. It creates NO ExecutionJob and places/modifies/closes NO order: it is deliberately
    SEPARATE from — and never ORs past — the execute/arm gates (``copier_execute_enabled`` /
    ``copier_execute_demo_armed`` / ``copier_live_execute_enabled``), which continue to be the sole, independent
    authority for any broker contact. Enabling this projects lifecycle rows only."""
    return copier_enabled() and _flag("COPIER_LIFECYCLE_ENABLED")


def copier_event_ingestion_enabled() -> bool:
    """Gate for the SIGNED, account-scoped fast-path event-ingestion endpoint (ADR-0074). DEFAULT OFF,
    ANDed with the master gate.

    The endpoint only ever VERIFIES a signed bridge emission and PERSISTS normalised position-state
    observations (immutable ``CopierIngestedRevision``) + a sanitised detection classification. It creates NO
    ``ExecutionJob``, writes NO ``MasterEvent`` into the authoritative poll ledger, and contacts NO broker: it
    is the fast half of the hybrid detector, recorded as evidence. When OFF the endpoint fails closed (HTTP
    503, nothing persisted) so the bridge can never push into a disarmed backend."""
    return copier_enabled() and _flag("COPIER_EVENT_INGESTION_ENABLED")


def copier_lifecycle_dispatch_enabled() -> bool:
    """Gate for the DARK lifecycle→producer-boundary SHADOW dispatch pass (ADR-0074). DEFAULT OFF, ANDed with
    the master gate.

    The dispatch pass re-checks the EXISTING producer-boundary authority (``execution_producer.admit_*``,
    which create nothing) for each actionable lifecycle decision and records a SYNTHETIC, append-only
    ``CopierLifecycleDispatch`` row (op, deterministic idempotency key, admission verdict, would_execute). It
    NEVER calls the ``produce_*`` functions and therefore creates NO ``ExecutionJob`` — it proves the wiring
    is correct and safe without activating execution. It is deliberately SEPARATE from — and never ORs past —
    the execute/arm gates, which remain the sole authority for any order."""
    return copier_enabled() and _flag("COPIER_LIFECYCLE_DISPATCH_ENABLED")


def copier_alerts_enabled() -> bool:
    """Gate for routing copier terminal-failure signals to the governed operator alert channel
    (``reliability.AlertEvent``) — ADR-0074. DEFAULT OFF, ANDed with the master gate.

    When OFF the terminal failure is still durably recorded as append-only lifecycle-transition evidence; the
    flag only governs whether a de-duplicated operator ``AlertEvent`` is ALSO raised. No new notification
    channel is created and nothing is pushed outward (a directly-created ``AlertEvent`` is surfaced on the
    read-only operations summary and the alerts API; it is not auto-delivered to Telegram). Alerting never
    recovers a trade — it only exposes operator action."""
    return copier_enabled() and _flag("COPIER_ALERTS_ENABLED")


def copier_lifecycle_guard_enabled() -> bool:
    """Backend reflection of the BRIDGE DARK flag ``COPIER_LIFECYCLE_GUARD_ENABLED`` (ADR-0085). DEFAULT OFF,
    ANDed with the master gate.

    The actual enforcement flag lives on the HOST bridge process (read by
    ``scripts/mt5_copier_lifecycle_guard.guard_enabled``); this predicate lets the backend emit the typed copier
    command to the shared partial-close worker ONLY when the guard would act, so with it OFF the authorize-send
    response is byte-identical to the pre-ADR-0085 instruction shape. It governs NO broker contact and widens NO
    authority: the guard only ADDS a fail-closed pre-state/revision/deadline check around a send every existing
    gate (demo/live wall, identity pin, authorization) already permits — it never opens a position, never
    bypasses those gates, and makes NO new order path reachable. Separate from — never ORs past — the execute/arm
    gates, which remain the sole authority for any order."""
    return copier_enabled() and _flag("COPIER_LIFECYCLE_GUARD_ENABLED")


def copier_event_projection_enabled() -> bool:
    """Gate for promoting accepted fast-path ``CopierIngestedRevision`` evidence into the AUTHORITATIVE
    ``MasterEvent`` ledger (ADR-0074 Phase-4 completion). DEFAULT OFF, ANDed with the master gate.

    This is the single, deterministic bridge between the fast EVENT path and the ONE authoritative ordering
    the lifecycle consumes (``MasterEvent.sequence``). The projection folds a master's new ingested revisions,
    OVERLAYS them onto the shared poll baseline (``CopierMaster.last_poll``), diffs with the EXISTING pure diff
    engine and appends ``MasterEvent`` rows (``source='EVENT'``, carrying the true ``broker_event_time``) under
    the SAME per-master row lock + monotone sequence the observer uses — so poll and event share one ledger,
    one sequence space and one baseline (no competing event truth). It creates NO ``ExecutionJob`` and contacts
    NO broker; a late/stale OPEN is ruled inadmissible by the UNCHANGED lifecycle state machine (the deadline is
    measured from ``broker_event_time``), never entered. Separate from — never ORs past — the execute/arm gates."""
    return copier_enabled() and _flag("COPIER_EVENT_PROJECTION_ENABLED")


def copier_alert_dispatch_enabled() -> bool:
    """Gate for the governed copier alert-DELIVERY dispatcher pass (ADR-0074 Phase-9 completion). DEFAULT OFF,
    ANDed with the master gate.

    Enqueuing a delivery is governed by ``copier_alerts_enabled`` (an alert row raised while that flag is on
    also enqueues an immutable ``CopierAlertDelivery`` outbox row). This SEPARATE flag governs whether the
    dispatcher pass may attempt to SEND a queued delivery through its transport. The transport is selected by
    ``COPIER_ALERT_TRANSPORT`` and DEFAULTS to an inert dry-run/NULL transport with NO real destination
    configured, so even an enabled dispatcher sends nowhere real until an operator both arms this flag AND
    configures a real transport. Delivery state (PENDING/SENT/FAILED/DEAD), the attempt counter and failures are
    durable and surfaced read-only on the operations summary. Nothing here recovers a trade."""
    return copier_enabled() and _flag("COPIER_ALERT_DISPATCH_ENABLED")


def copier_operator_view_enabled() -> bool:
    """Gate for the READ-ONLY operator lifecycle/failure view (ADR-0083 Phase-3). DEFAULT OFF, ANDed with the
    master gate.

    The view is staff-only, read-only, redacted and paginated: it surfaces the per-lifecycle state + failure
    reason + data-quality label for GuvFX operators. It performs NO retry/recover/modify/close/arm/authorization
    action, exposes NO secret/credential/raw-login, and NEVER converts missing/unreadable data into a zero,
    closed or successful state. When OFF the endpoint reports ``enabled:false`` and returns no rows."""
    return copier_enabled() and _flag("COPIER_OPERATOR_VIEW_ENABLED")


def copier_attestation_verify_enabled() -> bool:
    """Gate for the NON-LIFECYCLE host-emitter identity-ATTESTATION verification surface (ADR-0077). DEFAULT OFF,
    ANDed with the master gate.

    The endpoint ``POST /api/copy-trading/attest-verify/`` verifies a signed emitter attestation envelope. It
    DB-cross-checks the authoritative identity — account, broker login, broker server, the observed live login
    (the terminal the emitter actually reached, when probed), and the bridge/terminal route when a
    ``HostedExecutionEndpoint`` exists; the signed host id / build SHA are tamper-bound by the signature and
    recorded, but are not DB-cross-checked here (the backend holds no authoritative value for them). Verifying
    proves identity ONLY — it creates NO lifecycle state, ``MasterEvent``, ``ExecutionJob``, alert or broker
    contact, and is fully independent of ingestion/projection/dispatch/alert/execution. When OFF the endpoint
    returns 503 and verifies nothing."""
    return copier_enabled() and _flag("COPIER_ATTESTATION_VERIFY_ENABLED")


# ───────────────────── LIVE lifecycle completion (ADR-0083) — per-operation DARK arms ─────────────────────
# ADR-0057 implemented the LIVE follower OPEN only; LIVE modify / partial-close / full-close stayed fail-closed.
# ADR-0083 completes them behind THESE additional, independent, default-OFF arms. Each is ANDed with the whole
# LIVE stack (``copier_live_execute_enabled`` → ``copier_execute_enabled`` → ``copier_enabled``), so a LIVE
# lifecycle order is reachable ONLY when the master gate, the execute gate, the LIVE arm AND the specific
# per-operation arm are all on — never by one flag, and never for a pair without a complete
# ``CopierLiveAuthorization`` carrying the matching per-operation authority (see ``trade_copier.live_execution``).
# Arming a per-op flag never ORs past the kill switch, the authorization window, identity pins or the runtime
# ``MT5_ALLOW_LIVE`` boundary. None of these can OPEN or increase exposure.


def copier_live_modify_enabled() -> bool:
    """DARK arm for LIVE follower SL/TP MODIFY propagation (ADR-0083). DEFAULT OFF.

    ANDed with ``copier_live_execute_enabled`` (hence the whole master stack). A MODIFY only ever changes a
    LINKED follower position's protection (SL/TP); it can never open or increase exposure, and a stop weaken /
    removal stays fail-closed independently (``execution_producer._sl_weakening_reason``). Requires the
    authorization's ``modify_authority`` as well — a separate, auditable grant from the one-shot entry
    authority."""
    return copier_live_execute_enabled() and _flag("COPIER_LIVE_MODIFY_ENABLED")


def copier_live_partial_close_enabled() -> bool:
    """DARK arm for LIVE follower PARTIAL-CLOSE propagation (ADR-0083). DEFAULT OFF.

    ANDed with ``copier_live_execute_enabled``. A partial close only ever REDUCES a LINKED follower position's
    volume (never increases/reopens); the reduction fraction is derived from the master's confirmed
    before/after volume and normalised to broker min-volume/step, fail-closed on any ambiguity. Requires the
    authorization's ``partial_close_authority``."""
    return copier_live_execute_enabled() and _flag("COPIER_LIVE_PARTIAL_CLOSE_ENABLED")


def copier_live_full_close_enabled() -> bool:
    """DARK arm for LIVE follower FULL-CLOSE propagation (ADR-0083). DEFAULT OFF.

    ANDed with ``copier_live_execute_enabled``. A full close only ever CLOSES the one bound follower position
    (never reopens). Requires the authorization's ``full_close_authority``."""
    return copier_live_execute_enabled() and _flag("COPIER_LIVE_FULL_CLOSE_ENABLED")


def copier_live_close_only_after_expiry_enabled() -> bool:
    """DARK policy arm for CLOSE-ONLY containment AFTER the authorization entry window expires (ADR-0083). DEFAULT OFF.

    The entry authority always lapses at ``expires_at`` (no new entry may ever follow — ADR-0057). This
    SEPARATE, explicit, Sponsor-set policy decides whether a SAFETY-REDUCING close/partial-close
    (``full_close``/``partial_close`` only) may still be propagated to an ALREADY-BOUND follower position after
    that window, so a master close is not stranded. It can only REDUCE or close existing exposure — never
    create, reopen or increase it, and never a MODIFY. OFF (the default) means a post-expiry close is NOT
    auto-propagated and is surfaced for manual containment. Still ANDed with the matching per-op arm +
    authority."""
    return copier_live_execute_enabled() and _flag("COPIER_LIVE_CLOSE_ONLY_AFTER_EXPIRY_ENABLED")


def copier_live_contain_on_kill_switch_enabled() -> bool:
    """DARK policy arm for SAFETY-REDUCING close-only containment WHILE a kill switch is engaged (ADR-0083). DEFAULT OFF.

    The kill switch ALWAYS blocks every new entry and every MODIFY. This explicit, documented, default-OFF
    policy decides the one deliberately-chosen exception: whether an ALREADY-BOUND follower position may still
    be CLOSED / PARTIALLY CLOSED (exposure strictly reduced) while the switch is engaged — the conservative
    choice is OFF (freeze everything; the operator closes manually), but an operator may opt into
    close-to-contain. It can never open, increase or reopen exposure. Chosen rule documented in ADR-0083 and
    asserted by tests."""
    return copier_live_execute_enabled() and _flag("COPIER_LIVE_CONTAIN_ON_KILL_SWITCH_ENABLED")


def execution_status(*, note: str = "Saving configuration does not activate trading.") -> dict:
    """The copier's execution posture for the UI, DERIVED LIVE from the real execute flags — never a
    hardcoded label. While the execute producer is off (the default) this is observation/shadow and reads
    EXACTLY as the historical constant did (``mode='observation'``, ``execution='off'``, label
    'Observation mode'), so nothing in the UI changes today. It changes the instant a Sponsor arms the
    producer, so the UI can never keep asserting "EXECUTION OFF" while real DEMO orders are being produced.
    LIVE follower execution is never reachable from the copier (the execution LIVE wall governs that) and
    is therefore always reported blocked. The ``note`` is surface-specific (config vs catalogue matrix)."""
    execute = copier_execute_enabled()
    demo_armed = copier_execute_demo_armed()
    live_armed = copier_live_execute_enabled()
    if live_armed:
        # ADR-0057: LIVE copy execution is armed. Even now a LIVE order is reachable ONLY for a pair with a
        # complete, unexpired, human-created CopierLiveAuthorization — never broadly — but the UI must stop
        # asserting "EXECUTION OFF"/"blocked" the instant the LIVE arm flag is on.
        mode, execution, label, detail = (
            "live_execution", "live_armed", "LIVE copier execution armed",
            "LIVE follower orders may be produced for an explicitly authorized pair only "
            "(exact pair, bounded allowance — never a broad enable).")
    elif demo_armed:
        mode, execution, label, detail = (
            "demo_execution", "demo_armed", "DEMO execution armed",
            "DEMO follower orders are being produced (DEMO followers only — never LIVE).")
    elif execute:
        mode, execution, label, detail = (
            "shadow_jobs", "shadow_jobs", "Execution producer on — SHADOW jobs",
            "Suppressed SHADOW jobs only; no broker order is sent until DEMO is armed.")
    else:
        mode, execution, label, detail = (
            "observation", "off", "Observation mode", "Copy execution off")
    return {
        "mode": mode, "execution": execution, "label": label, "detail": detail, "note": note,
        # The live flag inputs this status is derived from (transparency/audit; plain booleans, never a secret).
        "copier_enabled": copier_enabled(),
        "execute_enabled": execute,
        "demo_armed": demo_armed,
        "live_armed": live_armed,
        # Blocked unless the LIVE arm flag is on; even armed, a real order still needs a complete per-pair
        # CopierLiveAuthorization (so this reports the ARM posture, not that any order will be placed).
        "live_follower_execution": "armed_authorized_pair" if live_armed else "blocked",
    }


def execution_banner() -> str:
    """The compact safety banner, DERIVED LIVE from the execute flags. Byte-identical to the historical
    constant while the producer is off; switches to an unambiguous armed label the moment a Sponsor arms
    the DEMO producer, so the banner can never be a stale "EXECUTION OFF" over live DEMO execution."""
    execution = execution_status()["execution"]
    if execution == "off":
        return "SHADOW — EXECUTION OFF"
    if execution == "shadow_jobs":
        return "SHADOW JOBS — NO ORDER SENT"
    if execution == "demo_armed":
        return "DEMO EXECUTION ARMED — DEMO ONLY"
    return "LIVE COPIER ARMED — AUTHORIZED PAIR ONLY"
