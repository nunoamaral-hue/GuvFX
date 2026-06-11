"""
RX-2 Reliability Core — constants and thresholds.

Phase 1: Detection + Visibility + Alerting + Recovery Recommendations ONLY.
No automatic recovery execution. All values are read-only thresholds.
"""
import os


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


# Master gate. When False, reliability_tick is a no-op (dormant deploy).
RELIABILITY_CORE_ENABLED = _flag("RELIABILITY_CORE_ENABLED", "false")


# ─── Components (units of TRADING capability, not process health) ───
class Component:
    MT5_TERMINAL = "MT5_TERMINAL"
    MT5_BROKER = "MT5_BROKER"
    SNAPSHOT_FEED = "SNAPSHOT_FEED"
    INGEST_WORKER = "INGEST_WORKER"
    VALIDATE_WORKER = "VALIDATE_WORKER"
    SCHEDULER_H1 = "SCHEDULER_H1"
    SCHEDULER_H4 = "SCHEDULER_H4"
    SCHEDULER_M5 = "SCHEDULER_M5"
    EXECUTION_PIPELINE = "EXECUTION_PIPELINE"
    BACKEND_DB = "BACKEND_DB"

    CHOICES = [
        (MT5_TERMINAL, "MT5 terminal"),
        (MT5_BROKER, "MT5 broker connection"),
        (SNAPSHOT_FEED, "Market snapshot feed"),
        (INGEST_WORKER, "Trade ingest worker"),
        (VALIDATE_WORKER, "Validate worker"),
        (SCHEDULER_H1, "H1 scheduler"),
        (SCHEDULER_H4, "H4 scheduler"),
        (SCHEDULER_M5, "M5 scheduler"),
        (EXECUTION_PIPELINE, "Execution pipeline"),
        (BACKEND_DB, "Backend database"),
    ]


# Critical components gate `can_trade`. Supporting components degrade only.
CRITICAL_COMPONENTS = {
    Component.MT5_TERMINAL,
    Component.MT5_BROKER,
    Component.EXECUTION_PIPELINE,
    Component.BACKEND_DB,
}

# Components scoped to a terminal/account (vs global infrastructure).
TERMINAL_SCOPED_COMPONENTS = {
    Component.MT5_TERMINAL,
    Component.MT5_BROKER,
    Component.SNAPSHOT_FEED,
    Component.EXECUTION_PIPELINE,
}


class HealthStatus:
    OK = "OK"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CHOICES = [(OK, OK), (STALE, STALE), (DEGRADED, DEGRADED), (FAILED, FAILED), (UNKNOWN, UNKNOWN)]


class TradingState:
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    IMPAIRED = "IMPAIRED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
    CHOICES = [(HEALTHY, HEALTHY), (DEGRADED, DEGRADED), (IMPAIRED, IMPAIRED), (DOWN, DOWN), (UNKNOWN, UNKNOWN)]


class Scope:
    GLOBAL = "GLOBAL"
    TERMINAL = "TERMINAL"
    ACCOUNT = "ACCOUNT"
    CHOICES = [(GLOBAL, GLOBAL), (TERMINAL, TERMINAL), (ACCOUNT, ACCOUNT)]


# ─── Thresholds (seconds) ───
HEARTBEAT_GRACE_MULTIPLIER = 2.5      # source stale if older than expected_interval * this
SNAPSHOT_STALE_SECONDS = 300          # market snapshot tick older than 5 min => STALE
SNAPSHOT_FAILED_SECONDS = 900         # older than 15 min => FAILED
MT5_TICK_STALE_SECONDS = 300
EXECUTION_LEASE_TTL_SECONDS = 300     # a RUNNING job must finish within this lease
DEBOUNCE_FAILURES = 1                 # consecutive failures before a non-OK transition is "confirmed"

# Expected heartbeat intervals per source (seconds). Schedulers/workers beat ~every minute/loop.
HEARTBEAT_EXPECTED_INTERVAL = {
    Component.SCHEDULER_H1: 60,
    Component.SCHEDULER_H4: 60,
    Component.SCHEDULER_M5: 60,
    Component.INGEST_WORKER: 60,
    Component.VALIDATE_WORKER: 60,
}
