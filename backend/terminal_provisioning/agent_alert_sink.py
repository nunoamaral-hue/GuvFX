"""Minimum-hardening WS-F — validation-agent alert DELIVERY sink (connector abstraction).

RR-11 (the Critical adversarial finding): an ``agent_down`` metric that pages nobody REPRODUCES the Aug-5
outage — dark for hours, discovered by a customer. This module is the delivery boundary: computed alerts
(``agent_monitoring.Alert``) terminate at a NAMED human, not a metric.

The Min-Hardening packet shipped the ABSTRACTION plus the safe, non-external sinks (``NullAlertSink``,
``LoggingAlertSink``). The Monitoring-Runner packet ADDS the first EXTERNAL delivery sinks —
``TelegramAlertSink`` (a DEDICATED ops chat, never the customer channel) and ``EmailAlertSink`` (a
fail-closed fallback) — but they remain DARK in the repository default (``AGENT_ALERT_SINK='null'``, no
Telegram/email vars set). Selecting a live recipient is a SEPARATE, Sponsor-gated deploy step.

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

    def _debounce_peek(self, key: str, now: float) -> bool:
        """True if ``key`` was recorded within the window (should suppress). Does NOT record."""
        last = self._last.get(key)
        return last is not None and (float(now) - last) < self.debounce_seconds

    def _debounce_commit(self, key: str, now: float) -> None:
        self._last[key] = float(now)

    def _debounced(self, key: str, now: float) -> bool:
        """Peek-then-commit-if-fresh. Used by sinks whose delivery cannot fail transiently (logging).
        A network sink must instead peek before the attempt and commit ONLY on success, so a FAILED
        send is not swallowed as a duplicate on the next probe."""
        if self._debounce_peek(key, now):
            return True
        self._debounce_commit(key, now)
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


class TelegramAlertSink(_DebounceMixin, AlertSink):
    """EXTERNAL delivery to a DEDICATED operations Telegram chat via the existing
    ``execution.notifications.real_transport.RealTelegramTransport`` primitive (``_send`` text path — no
    trade CARD, no ``NotificationCandidate``). This is the first sink in the estate that leaves the box, so
    the whole safety burden lives here:

      - **Never the customer channel.** The destination is a caller-supplied ops ``chat_id`` distinct from the
        customer ``TELEGRAM_CHAT_ID``; ``build_alert_sink`` refuses to construct this sink if the two match.
      - **Own credential, fail closed.** Requires its OWN bot token (``VALIDATION_AGENT_TELEGRAM_BOT_TOKEN``);
        it NEVER silently borrows the customer ``TELEGRAM_BOT_TOKEN`` (security RULE 3). Missing token or
        chat_id => construction refused (fail closed), not a silent no-op that looks healthy.
      - **Secret-safe.** The token appears only inside ``RealTelegramTransport``'s URL and is never logged,
        returned, or placed in a ``DeliveryResult``. The chat_id is never emitted either. A failure carries
        only an HTTP status code / API error_code / exception TYPE — never a raw body.
      - **Never raises.** A delivery failure is reported (``delivered=False`` + sanitised reason); the monitor
        keeps running. Bounded retry with capped backoff; a failed send is NOT debounced (so the next probe
        retries), only a SUCCESS commits the debounce window.
      - **Bounded.** One short, ASCII, length-capped message per alert; no web preview.
    """

    channel = "telegram"
    TEST = "manage.py test_agent_alert_delivery --correlation-id <id> (marked TEST; no broker/customer/state)"
    RETRY = "bounded: attempts with capped exponential backoff; a failure is surfaced, never raised/looped"
    ON_FAILURE = "returns delivered=False with a sanitised reason (HTTP code / api error_code / exc type)"
    ACK = "operator acknowledges by actioning the named runbook; no in-band ack channel"

    MAX_TEXT = 3500                 # Telegram hard cap is 4096; stay well under after prefixing
    DEFAULT_ATTEMPTS = 3
    DEFAULT_BACKOFF_BASE_S = 0.5
    DEFAULT_BACKOFF_CAP_S = 4.0
    _ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _:.,%()->/=+#@")

    def __init__(self, *, owner: str, chat_id: str, token: str = "", transport=None,
                 debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS, attempts: int = DEFAULT_ATTEMPTS,
                 backoff_base_s: float = DEFAULT_BACKOFF_BASE_S, backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
                 sleep_fn=None):
        _DebounceMixin.__init__(self, debounce_seconds=debounce_seconds)
        if not owner or owner.strip().upper() in ("", "UNASSIGNED", "NONE"):
            raise ValueError("TelegramAlertSink requires a NAMED owner (RR-11: alerts terminate at a human)")
        cid = str(chat_id or "").strip()
        if not cid:
            raise ValueError("TelegramAlertSink requires a destination chat_id (fail closed — no channel)")
        # ``transport`` (a callable(text)->dict) is injected in tests; in production it is built from an
        # OWN token. Refuse construction with neither — never fall through to the customer bot token.
        self._transport = transport
        self._token = str(token or "")
        if self._transport is None and not self._token:
            raise ValueError("TelegramAlertSink requires its OWN bot token (no customer-token fallback)")
        self.owner = owner.strip()
        self._chat_id = cid
        self._attempts = max(1, int(attempts))
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_cap_s = float(backoff_cap_s)
        self._sleep = sleep_fn if sleep_fn is not None else _real_sleep

    # ── message construction (sanitised, bounded, ASCII) ──
    def _sanitise(self, s: str) -> str:
        return "".join(c for c in str(s) if c in self._ALLOWED)

    def _format(self, alert, correlation_id: str) -> str:
        name = self._sanitise(getattr(alert, "name", "unknown"))
        severity = self._sanitise(getattr(alert, "severity", "MEDIUM"))
        state = self._sanitise(getattr(alert, "detects_state", ""))
        runbook = self._sanitise(getattr(alert, "runbook", ""))
        detail = self._sanitise(getattr(alert, "detail", ""))
        corr = self._sanitise(correlation_id or "")
        head = "[GuvFX validation-agent]"
        recovery = getattr(alert, "severity", "") == "RECOVERY" or name == "agent_recovered"
        tag = "RECOVERED" if recovery else f"ALERT {severity}"
        lines = [f"{head} {tag}: {name}",
                 f"state={state} runbook={runbook} owner={self._sanitise(self.owner)}"]
        if detail:
            lines.append(f"detail: {detail}")
        if corr:
            lines.append(f"corr={corr}")
        return "\n".join(lines)[:self.MAX_TEXT]

    def _send(self, text: str) -> dict:
        if self._transport is not None:
            return self._transport(text)
        # Build the real transport lazily so this module imports without ``execution`` in pure tests.
        from execution.notifications.real_transport import RealTelegramTransport  # noqa: PLC0415
        return RealTelegramTransport(token=self._token, chat_id=self._chat_id)._send(text)

    def deliver(self, alert, *, now: float, correlation_id: str = "") -> DeliveryResult:
        name = getattr(alert, "name", "unknown")
        key = f"{name}:{getattr(alert, 'detail', '')}"
        if self._debounce_peek(key, now):
            return DeliveryResult(delivered=False, channel=self.channel, suppressed=True, reason="debounced")
        text = self._format(alert, correlation_id)
        reason = "not_attempted"
        for attempt in range(1, self._attempts + 1):
            try:
                payload = self._send(text)
            except Exception as exc:  # noqa: BLE001 — a network failure must never crash the monitor
                reason = _sanitise_send_error(exc)
            else:
                if isinstance(payload, dict) and payload.get("ok"):
                    self._debounce_commit(key, now)    # commit the window ONLY on a real success
                    return DeliveryResult(delivered=True, channel=self.channel)
                reason = (f"api_error_{payload.get('error_code')}" if isinstance(payload, dict)
                          else "bad_response")
            if attempt < self._attempts:
                self._sleep(min(self._backoff_cap_s, self._backoff_base_s * (2 ** (attempt - 1))))
        return DeliveryResult(delivered=False, channel=self.channel, reason=reason)


class EmailAlertSink(_DebounceMixin, AlertSink):
    """Fail-closed EMAIL fallback (WS-H). Delivers a short, secret-safe alert to a NAMED operations mailbox
    via Django's configured email backend. It is a FALLBACK for ``TelegramAlertSink`` — the monitor tries it
    only when the primary external delivery failed — and, like every sink, NEVER raises. If no recipient is
    configured the factory returns ``None`` (delivery is explicitly *not configured*, never a silent drop).
    The message carries only sanitised alert codes; no credential, chat id, or token is ever included."""

    channel = "email"
    TEST = "manage.py test_agent_alert_delivery --channel email (marked TEST; no broker/customer/state)"
    RETRY = "none at this layer — the email backend owns transport retry; a failure is surfaced, not looped"
    ON_FAILURE = "returns delivered=False with a sanitised reason (exc type); monitor keeps running"
    ACK = "operator acknowledges by actioning the named runbook"

    def __init__(self, *, owner: str, recipient: str, send_fn=None,
                 debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS):
        _DebounceMixin.__init__(self, debounce_seconds=debounce_seconds)
        if not owner or owner.strip().upper() in ("", "UNASSIGNED", "NONE"):
            raise ValueError("EmailAlertSink requires a NAMED owner")
        if not recipient or "@" not in recipient:
            raise ValueError("EmailAlertSink requires a recipient mailbox (fail closed)")
        self.owner = owner.strip()
        self._recipient = recipient.strip()
        self._send_fn = send_fn                      # injectable(subject, body, recipient)->None; default below

    def deliver(self, alert, *, now: float, correlation_id: str = "") -> DeliveryResult:
        name = getattr(alert, "name", "unknown")
        key = f"{name}:{getattr(alert, 'detail', '')}"
        if self._debounce_peek(key, now):
            return DeliveryResult(delivered=False, channel=self.channel, suppressed=True, reason="debounced")
        severity = getattr(alert, "severity", "MEDIUM")
        subject = f"[GuvFX validation-agent] {severity}: {name}"
        body = (f"state={getattr(alert, 'detects_state', '')} runbook={getattr(alert, 'runbook', '')} "
                f"owner={self.owner} corr={correlation_id or ''}\ndetail: {getattr(alert, 'detail', '')}")
        try:
            (self._send_fn or _default_email_send)(subject, body, self._recipient)
        except Exception as exc:  # noqa: BLE001 — delivery must never crash the monitor
            return DeliveryResult(delivered=False, channel=self.channel,
                                  reason=f"email_error_{type(exc).__name__}")
        self._debounce_commit(key, now)
        return DeliveryResult(delivered=True, channel=self.channel)


def _default_email_send(subject: str, body: str, recipient: str) -> None:  # pragma: no cover — thin wrapper
    from django.conf import settings as dj_settings
    from django.core.mail import send_mail
    sender = getattr(dj_settings, "DEFAULT_FROM_EMAIL", "") or "alerts@guvfx.local"
    send_mail(subject, body, sender, [recipient], fail_silently=False)


def build_fallback_email_sink(*, settings_obj=None):
    """Return an ``EmailAlertSink`` when ``VALIDATION_AGENT_ALERT_FALLBACK_EMAIL`` + ``AGENT_ALERT_OWNER`` are
    set, else ``None`` (fallback explicitly not configured — the monitor then surfaces the delivery failure
    rather than pretending an alert was handled). Never raises."""
    if settings_obj is None:
        from django.conf import settings as settings_obj  # noqa: PLC0415
    recipient = str(getattr(settings_obj, "VALIDATION_AGENT_ALERT_FALLBACK_EMAIL", "") or "").strip()
    owner = str(getattr(settings_obj, "AGENT_ALERT_OWNER", "") or "").strip()
    if not recipient:
        return None
    try:
        return EmailAlertSink(owner=owner, recipient=recipient)
    except ValueError:
        log.error("VALIDATION_AGENT_ALERT_FALLBACK_EMAIL set but owner/recipient invalid — no email fallback")
        return None


def _real_sleep(seconds: float) -> None:  # pragma: no cover — trivial; injected out in tests
    import time
    time.sleep(seconds)


def _sanitise_send_error(exc: Exception) -> str:
    """Reduce any transport exception to a short, secret-free token. NEVER include the message/body (it may
    echo the URL, hence the token). HTTP errors keep only the numeric status."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{getattr(exc, 'code', '?')}"
    if isinstance(exc, urllib.error.URLError):
        return f"network_{type(getattr(exc, 'reason', exc)).__name__}"
    return f"send_error_{type(exc).__name__}"


def build_alert_sink(*, settings_obj=None):
    """Factory: choose the sink from settings, defaulting to the SAFE ``NullAlertSink``.

    - ``AGENT_ALERT_SINK='logging'`` + ``AGENT_ALERT_OWNER='<named human/rota>'`` → durable local logging sink.
    - ``AGENT_ALERT_SINK='telegram'`` + owner + ``VALIDATION_AGENT_TELEGRAM_CHAT_ID`` +
      ``VALIDATION_AGENT_TELEGRAM_BOT_TOKEN`` → the EXTERNAL ops Telegram sink. This is Sponsor-gated at
      deploy time; the repository default keeps it OFF (sink=null, no Telegram vars set).

    Never raises: any misconfiguration (missing owner/chat/token, or a chat_id that collides with the
    CUSTOMER channel) falls back to ``NullAlertSink`` and logs the reason, so monitoring still runs and the
    absence of delivery is EXPLICIT (never a customer-channel page, never a silent healthy-looking no-op)."""
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
    if kind == "telegram":
        ops_chat = str(getattr(settings_obj, "VALIDATION_AGENT_TELEGRAM_CHAT_ID", "") or "").strip()
        ops_token = str(getattr(settings_obj, "VALIDATION_AGENT_TELEGRAM_BOT_TOKEN", "") or "").strip()
        # The CUSTOMER channel/token live in OS env (execution.notifications.real_transport reads them via
        # os.getenv), NOT as Django settings — so the collision guard MUST consult the same source, else it is
        # dead in every real deployment. Honour an override_settings value first (for tests), then env.
        customer_chat = _customer_value(settings_obj, "TELEGRAM_CHAT_ID")
        customer_token = _customer_value(settings_obj, "TELEGRAM_BOT_TOKEN")
        if customer_chat and ops_chat and ops_chat == customer_chat:
            log.error("AGENT_ALERT_SINK=telegram but VALIDATION_AGENT_TELEGRAM_CHAT_ID equals the CUSTOMER "
                      "TELEGRAM_CHAT_ID — refusing (an ops page must never hit the customer channel); "
                      "falling back to NullAlertSink")
            return NullAlertSink()
        if customer_token and ops_token and ops_token == customer_token:
            log.error("AGENT_ALERT_SINK=telegram but VALIDATION_AGENT_TELEGRAM_BOT_TOKEN equals the CUSTOMER "
                      "TELEGRAM_BOT_TOKEN — refusing (security RULE 3: the ops sink needs its OWN secret); "
                      "falling back to NullAlertSink")
            return NullAlertSink()
        try:
            return TelegramAlertSink(owner=owner, chat_id=ops_chat, token=ops_token)
        except ValueError as exc:
            log.error("AGENT_ALERT_SINK=telegram misconfigured (%s) — falling back to NullAlertSink "
                      "(fail closed: no owner/chat/token => no delivery, made explicit)", type(exc).__name__)
            return NullAlertSink()
    return NullAlertSink()


def _customer_value(settings_obj, name: str) -> str:
    """Resolve a CUSTOMER-notification value the same way ``RealTelegramTransport`` does: a Django setting if
    one is defined (supports override_settings in tests), otherwise the OS env var (the real deployment)."""
    import os
    val = getattr(settings_obj, name, None)
    if val is None:
        val = os.getenv(name, "")
    return str(val or "").strip()


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
