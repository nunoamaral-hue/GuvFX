"""Minimum-hardening WS-F — validation-agent alert DELIVERY sink (connector abstraction).

RR-11 (the Critical adversarial finding): an ``agent_down`` metric that pages nobody REPRODUCES the Aug-5
outage — dark for hours, discovered by a customer. This module is the delivery boundary: computed alerts
(``agent_monitoring.Alert``) terminate at a NAMED human, not a metric.

This packet ships the ABSTRACTION and safe, non-external sinks ONLY. It performs NO live production
notification: there is no approved external alert channel wired in the estate yet
(``OPERATIONS_DASHBOARD`` missing_alerts is a red finding), so selecting a live recipient is a SEPARATE,
Sponsor-gated step. Until then the sanctioned delivery is the ``LoggingAlertSink`` (durable, local, inert)
plus an explicitly scheduled human dashboard-poll with a named owner (documented in the runbooks).

Contract each concrete sink documents (design §): channel, owner, test procedure, retry policy, failure
handling, acknowledgement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("guvfx.validation_agent.alerts")

# Debounce: do not re-deliver the SAME (name, detail) within this window — an alert that fires every probe
# must not become a pager storm (itself an outage-of-attention). The caller passes a monotonic ``now``.
DEFAULT_DEBOUNCE_SECONDS = 900


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    channel: str
    suppressed: bool = False       # debounced (a duplicate within the window), not a failure
    reason: str = ""

    def as_dict(self) -> dict:
        return {"delivered": self.delivered, "channel": self.channel, "suppressed": self.suppressed,
                "reason": self.reason}


class AlertSink:
    """Delivery boundary. A concrete sink MUST be secret-safe (an alert carries only sanitised codes) and
    MUST NOT raise to the caller — a delivery failure is reported, never crashes the monitor."""

    channel = "abstract"
    owner = "UNASSIGNED"           # a NAMED human/rota — never left UNASSIGNED for a production sink

    def deliver(self, alert, *, now: float, correlation_id: str = "") -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError

    # documentation contract (surfaced by tests + the deployment package)
    def describe(self) -> dict:
        return {"channel": self.channel, "owner": self.owner,
                "test": self.TEST, "retry": self.RETRY, "on_failure": self.ON_FAILURE, "ack": self.ACK}

    TEST = "n/a"
    RETRY = "n/a"
    ON_FAILURE = "n/a"
    ACK = "n/a"


class _DebounceMixin:
    def __init__(self, *, debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS):
        self.debounce_seconds = int(debounce_seconds)
        self._last: dict[str, float] = {}

    def _debounced(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and (float(now) - last) < self.debounce_seconds:
            return True
        self._last[key] = float(now)
        return False


class NullAlertSink(AlertSink):
    """No-op sink (records nothing, sends nothing). The SAFE default when no delivery channel is approved —
    it makes the absence of delivery EXPLICIT rather than pretending an alert was handled."""

    channel = "null"
    owner = "NONE"
    TEST = "instantiate; deliver() returns delivered=False, reason=no_channel_configured"
    RETRY = "none (nothing is sent)"
    ON_FAILURE = "n/a — never sends"
    ACK = "n/a"

    def deliver(self, alert, *, now: float, correlation_id: str = "") -> DeliveryResult:
        return DeliveryResult(delivered=False, channel=self.channel, reason="no_channel_configured")


class LoggingAlertSink(_DebounceMixin, AlertSink):
    """Durable, LOCAL, inert delivery: writes a structured, secret-safe alert line to the
    ``guvfx.validation_agent.alerts`` logger (which the platform's log pipeline retains). No external call,
    so it is safe to enable now; it is the interim sanctioned sink pending an approved on-call channel.
    Paired in ops with a NAMED human dashboard-poll cadence (the human is the real terminus)."""

    channel = "logging"
    TEST = "deliver() emits one WARNING/ERROR log line; assert the record + level in a caplog test"
    RETRY = "none — a local log write does not fail transiently; a logging error is swallowed, not retried"
    ON_FAILURE = "swallowed (never raises to the monitor); the monitor keeps running"
    ACK = "operator acknowledges by actioning the runbook; no in-band ack channel"

    def __init__(self, *, owner: str, debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS):
        _DebounceMixin.__init__(self, debounce_seconds=debounce_seconds)
        if not owner or owner.strip().upper() in ("", "UNASSIGNED", "NONE"):
            raise ValueError("LoggingAlertSink requires a NAMED owner (RR-11: alerts terminate at a human)")
        self.owner = owner.strip()

    def deliver(self, alert, *, now: float, correlation_id: str = "") -> DeliveryResult:
        name = getattr(alert, "name", "unknown")
        severity = getattr(alert, "severity", "MEDIUM")
        key = f"{name}:{getattr(alert, 'detail', '')}"
        if self._debounced(key, now):
            return DeliveryResult(delivered=False, channel=self.channel, suppressed=True, reason="debounced")
        try:
            level = logging.ERROR if severity == "HIGH" else logging.WARNING
            log.log(level, "ALERT name=%s severity=%s state=%s runbook=%s owner=%s corr=%s detail=%s",
                    name, severity, getattr(alert, "detects_state", ""), getattr(alert, "runbook", ""),
                    self.owner, correlation_id or "", getattr(alert, "detail", ""))
            return DeliveryResult(delivered=True, channel=self.channel)
        except Exception:  # noqa: BLE001 — delivery must never crash the monitor
            return DeliveryResult(delivered=False, channel=self.channel, reason="log_write_failed")


def build_alert_sink(*, settings_obj=None):
    """Factory: choose the sink from settings, defaulting to the SAFE ``NullAlertSink``. A production channel
    (``AGENT_ALERT_SINK='logging'`` + ``AGENT_ALERT_OWNER='<named human/rota>'``) is opt-in and, for any
    EXTERNAL channel, Sponsor-gated (not shipped in this packet). Never raises: a misconfiguration falls back
    to Null and logs, so monitoring still runs and the absence of delivery is explicit."""
    if settings_obj is None:
        from django.conf import settings as settings_obj  # noqa: PLC0415
    kind = str(getattr(settings_obj, "AGENT_ALERT_SINK", "") or "null").strip().lower()
    owner = str(getattr(settings_obj, "AGENT_ALERT_OWNER", "") or "").strip()
    if kind == "logging":
        try:
            return LoggingAlertSink(owner=owner)
        except ValueError:
            log.error("AGENT_ALERT_SINK=logging but AGENT_ALERT_OWNER is unset — falling back to NullAlertSink "
                      "(RR-11: an alert must terminate at a NAMED human)")
            return NullAlertSink()
    return NullAlertSink()


def deliver_alerts(sink: AlertSink, alerts, *, now: float, correlation_id: str = "") -> list[dict]:
    """Deliver a list of alerts through the sink; return per-alert results. Never raises."""
    out = []
    for a in alerts or []:
        try:
            out.append(sink.deliver(a, now=now, correlation_id=correlation_id).as_dict())
        except Exception:  # noqa: BLE001 — belt-and-braces: one bad alert never stops the rest
            out.append(DeliveryResult(delivered=False, channel=getattr(sink, "channel", "?"),
                                      reason="deliver_raised").as_dict())
    return out
