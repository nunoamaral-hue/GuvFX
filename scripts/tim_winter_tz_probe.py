#!/usr/bin/env python3
"""TIM winter broker-timezone probe — host-local operator wrapper (GFX-TIM-WINTER-TIMEZONE-HOST-PROBE-009).

Repository tooling: NOT installed, NOT scheduled, NOT run by any agent, NOT deployed.
A human operator runs it in their own console ON the Windows MT5 bridge host, after a
separate Sponsor GO. This file never performs a real broker read by itself: the real
read requires BOTH ``--execute`` AND the on-host confirmation env var, and is intended to
be issued only by the human operator on the bridge host.

What it does (read-only): calls the LOCAL (loopback) already-authenticated GuvFX bridge to

  1. classify the connected account as DEMO (fail closed unless ``trade_mode == 0``);
  2. confirm the connected server is the approved target;
  3. select exactly three COMPLETED M1 bars (the currently-forming bar is excluded);
  4. capture an independent UTC timestamp (host NTP-synced clock) around each read;
  5. compute the whole-hour broker offset for each sample and require agreement;
  6. emit a sanitized, value-blind JSON result.

Security contract (enforced + unit-tested with synthetic fixtures only):
  * The bridge credential is read ONLY inside the process via ``os.getenv("GUVFX_AGENT_TOKEN")``
    (see ``default_token_provider``). It is NEVER echoed, serialized, hashed, logged, placed
    in argv, or placed in a child-process command line. There are no child processes.
  * HTTP is restricted to loopback (``127.0.0.1`` / ``localhost`` / ``::1``); any other host
    is refused.
  * Fails closed on: token unavailable, endpoint unreachable, 401/403, malformed payload,
    missing/ambiguous trade mode, non-DEMO account, wrong server, fewer/more than three
    completed bars, open/incomplete bar, independent UTC unavailable, inconsistent offsets,
    DST-boundary ambiguity.
  * The emitted JSON contains ONLY allow-listed keys: no login, account number, token,
    balance, equity, currency, positions, orders or message bodies.
  * The module imports only the standard library. It has no trading / order / history-export /
    file-copy / NAS capability and cannot modify MT5 or broker state.

This wrapper implements the already-governed, read-only timezone-evidence method documented
in ``docs/evidence/broker_timezone_evidence_v1.md`` (ADR-0070 / ADR-0076 producer boundary).
It introduces no new authority and no new network surface; it does not weaken the bridge
authentication boundary and adds no unauthenticated endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# --- constants ---------------------------------------------------------------

TOKEN_ENV = "GUVFX_AGENT_TOKEN"            # name only; value read via os.getenv at call time
AUTH_HEADER = "X-GuvFX-Agent-Token"       # request header the bridge requires
ON_HOST_ENV = "TIM_WINTER_TZ_PROBE_ON_HOST"  # must equal "1" for --execute to proceed

ACCOUNT_PATH = "/mt5/snapshots/account"
RATES_PATH = "/mt5/snapshots/rates"
SYMBOL = "EURUSD"
TIMEFRAME = "M1"
BAR_SECONDS = 60
REQUIRED_COMPLETED = 3
# Fetch one extra bar so the currently-forming bar (data[-1]) can be identified and EXCLUDED,
# leaving exactly three COMPLETED bars for the measurement.
RATES_COUNT = REQUIRED_COMPLETED + 1

DEMO_TRADE_MODE = 0                        # MT5: 0=DEMO, 1=CONTEST, 2=REAL
OFFSET_MIN_S = -12 * 3600
OFFSET_MAX_S = 14 * 3600
RESIDUAL_MAX_S = 180                       # bar-freshness / clock-skew tolerance
CAPTURE_WINDOW_MAX_S = 2.0                 # max span of the independent-UTC capture per sample
DEFAULT_SAMPLES = 3
DST_GUARD_S = 3600                         # reject observations within 1h of an EU DST change
HTTP_TIMEOUT_S = 8

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

ALLOWED_OUTPUT_KEYS = frozenset({
    "schema_version", "ok", "assessment_status", "reason_code",
    "demo_classification", "broker_server_identifier",
    "completed_bar_server_epochs", "independent_utc_epochs",
    "implied_offsets_seconds", "residuals_seconds",
    "consistency", "offset_hours", "correlation_id",
    "observation_time_utc", "samples", "method", "limitations",
})
# Defence-in-depth: these substrings must never appear in a key of the emitted JSON.
FORBIDDEN_OUTPUT_KEY_SUBSTRINGS = (
    "token", "password", "secret", "credential", "login", "balance",
    "equity", "currency", "position", "order", "deal", "margin", "profit",
)

SCHEMA_VERSION = "tim-winter-tz-probe/1.0"
METHOD_REF = ("read-only loopback /mt5/snapshots/{account,rates}; three completed M1 bars "
              "(forming bar excluded); server-anchor vs independent NTP UTC; whole-hour offset")


# --- errors (all messages are value-blind by construction) -------------------

class ProbeError(Exception):
    reason_code = "probe_error"


class TokenUnavailableError(ProbeError):
    reason_code = "token_unavailable"


class NonLoopbackError(ProbeError):
    reason_code = "non_loopback_target"


class EndpointUnreachableError(ProbeError):
    reason_code = "endpoint_unreachable"


class AuthRejectedError(ProbeError):
    reason_code = "auth_rejected"


class MalformedPayloadError(ProbeError):
    reason_code = "malformed_payload"


class DemoClassificationError(ProbeError):
    reason_code = "not_demo_or_ambiguous"


class BarSelectionError(ProbeError):
    reason_code = "bar_selection_failed"


class IndependentUtcUnavailableError(ProbeError):
    reason_code = "independent_utc_unavailable"


class OffsetInconsistentError(ProbeError):
    reason_code = "offset_inconsistent"


class DstAmbiguityError(ProbeError):
    reason_code = "dst_boundary_ambiguity"


# --- token provider ----------------------------------------------------------

def default_token_provider():
    """Read the bridge credential from the process environment ONLY.

    Returns the token string, or raises TokenUnavailableError if it is absent/blank.
    The value is returned to the caller (the transport) and must never be stored on an
    object, logged, or serialized. It is never read from argv.
    """
    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        raise TokenUnavailableError("bridge credential absent from process environment")
    return token


# --- loopback transport ------------------------------------------------------

class LoopbackBridgeTransport:
    """Minimal GET client restricted to the loopback bridge. Stdlib urllib only."""

    def __init__(self, base_url, opener=None):
        parsed = urlparse(base_url)
        if parsed.scheme != "http":
            raise NonLoopbackError("loopback bridge must be plain http")
        if parsed.hostname not in LOOPBACK_HOSTS:
            raise NonLoopbackError("target host is not loopback")
        self._base = base_url.rstrip("/")
        self._opener = opener or urlopen

    def get_json(self, path, token_provider):
        token = token_provider()  # in-process read; never stored on self
        try:
            req = Request(self._base + path, headers={AUTH_HEADER: token}, method="GET")
            try:
                with self._opener(req, timeout=HTTP_TIMEOUT_S) as resp:
                    raw = resp.read()
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise AuthRejectedError("bridge rejected the credential")
                raise EndpointUnreachableError("bridge returned an error status")
            except (URLError, OSError):
                raise EndpointUnreachableError("bridge loopback unreachable")
        finally:
            token = None  # drop the reference promptly
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            raise MalformedPayloadError("bridge response is not valid JSON")
        if not isinstance(obj, dict) or obj.get("ok") is not True:
            raise MalformedPayloadError("bridge response missing ok=true")
        return obj


# --- pure core ---------------------------------------------------------------

def classify_demo(account_snapshot):
    """Fail closed unless the broker reports a DEMO account. Reads ONLY ``trade_mode``."""
    if not isinstance(account_snapshot, dict):
        raise DemoClassificationError("account snapshot absent")
    trade_mode = account_snapshot.get("trade_mode")
    if trade_mode is None:
        raise DemoClassificationError("trade_mode absent")
    if isinstance(trade_mode, bool) or not isinstance(trade_mode, int):
        raise DemoClassificationError("trade_mode ambiguous")
    if trade_mode != DEMO_TRADE_MODE:
        raise DemoClassificationError("account is not DEMO")
    return "DEMO"


def confirm_server(account_snapshot, expected_server):
    """Confirm the connected server equals the approved target. Returns the approved label."""
    if not isinstance(expected_server, str) or not expected_server.strip():
        raise DemoClassificationError("approved server label not provided")
    server = account_snapshot.get("account_server") or account_snapshot.get("server")
    if not isinstance(server, str) or not server.strip():
        raise DemoClassificationError("connected server identifier absent")
    if server.strip().lower() != expected_server.strip().lower():
        raise DemoClassificationError("connected server is not the approved target")
    return expected_server.strip()


def select_completed_bars(rates_payload):
    """Return exactly three COMPLETED M1 bar start-epochs (ascending).

    The bridge returns bars oldest-first with the currently-forming bar last (``data[-1]``).
    The forming bar is EXCLUDED. We require the forming bar to be exactly one minute after
    the latest completed bar, and the three completed bars to be 60 s apart — otherwise the
    series is rejected (no open/incomplete bar is ever used in the measurement).
    """
    if not isinstance(rates_payload, dict):
        raise MalformedPayloadError("rates payload absent")
    data = rates_payload.get("data")
    if not isinstance(data, list):
        raise MalformedPayloadError("rates payload missing data array")
    if len(data) < REQUIRED_COMPLETED + 1:
        raise BarSelectionError("fewer than three completed bars plus a forming bar")
    times = []
    for bar in data:
        if not isinstance(bar, dict) or "time" not in bar:
            raise MalformedPayloadError("bar missing time field")
        t = bar["time"]
        if isinstance(t, bool) or not isinstance(t, int):
            raise MalformedPayloadError("bar time is not an integer epoch")
        times.append(t)
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            raise MalformedPayloadError("bars are not strictly ascending")
    forming = times[-1]
    completed = times[-(REQUIRED_COMPLETED + 1):-1]  # drop the forming bar
    if len(completed) != REQUIRED_COMPLETED:
        raise BarSelectionError("could not isolate exactly three completed bars")
    for i in range(1, len(completed)):
        if completed[i] - completed[i - 1] != BAR_SECONDS:
            raise BarSelectionError("completed bars are not at 60 s cadence")
    if forming - completed[-1] != BAR_SECONDS:
        raise BarSelectionError("forming bar not contiguous with the completed series")
    return completed


def compute_offset(server_anchor_epoch, utc_epoch):
    """Compute the whole-hour broker offset.

    ``server_anchor_epoch`` is the latest completed bar's END (= forming-bar start), the
    broker server-clock minute boundary. The raw difference against independent UTC is
    rounded to the nearest whole hour (FX offsets are integer hours); the residual is the
    freshness/skew term and must fall in [0, RESIDUAL_MAX_S]. Returns (offset_s, residual_s).
    """
    if isinstance(server_anchor_epoch, bool) or not isinstance(server_anchor_epoch, int):
        raise MalformedPayloadError("server anchor is not an integer epoch")
    if isinstance(utc_epoch, bool) or not isinstance(utc_epoch, int):
        raise IndependentUtcUnavailableError("independent UTC is not an integer epoch")
    raw = server_anchor_epoch - utc_epoch
    offset = int(round(raw / 3600.0)) * 3600
    residual = offset - raw
    if not (OFFSET_MIN_S <= offset <= OFFSET_MAX_S):
        raise OffsetInconsistentError("offset outside the plausible range")
    if not (0 <= residual <= RESIDUAL_MAX_S):
        raise OffsetInconsistentError("residual outside bound (stale bar or clock skew)")
    return offset, residual


def eu_dst_transitions_utc(year):
    """Return the two EU DST transition instants (UTC epoch) for ``year``.

    EU DST starts the last Sunday of March and ends the last Sunday of October, both at
    01:00 UTC. Pure arithmetic (no zoneinfo dependency) using a Gregorian weekday formula.
    """
    def last_sunday(y, month):
        # day-of-week for the last day of the month, then step back to Sunday.
        if month == 12:
            next_month_first = (y + 1, 1, 1)
        else:
            next_month_first = (y, month + 1, 1)
        # last day of this month = day before the first of next month
        import datetime
        last_day = datetime.date(*next_month_first) - datetime.timedelta(days=1)
        # weekday(): Monday=0 .. Sunday=6
        offset_to_sunday = (last_day.weekday() - 6) % 7
        sunday = last_day - datetime.timedelta(days=offset_to_sunday)
        return sunday

    import calendar
    march_sunday = last_sunday(year, 3)
    october_sunday = last_sunday(year, 10)
    spring = calendar.timegm((march_sunday.year, march_sunday.month, march_sunday.day, 1, 0, 0, 0, 0, 0))
    autumn = calendar.timegm((october_sunday.year, october_sunday.month, october_sunday.day, 1, 0, 0, 0, 0, 0))
    return spring, autumn


def assert_not_dst_ambiguous(utc_epoch):
    """Raise DstAmbiguityError if the observation is within DST_GUARD_S of a EU DST change."""
    import datetime
    year = datetime.datetime.fromtimestamp(utc_epoch, datetime.timezone.utc).year
    for y in (year - 1, year, year + 1):
        for transition in eu_dst_transitions_utc(y):
            if abs(utc_epoch - transition) < DST_GUARD_S:
                raise DstAmbiguityError("observation too close to a DST transition")


def consistency_verdict(observations):
    """Require at least one observation and identical whole-hour offsets across all samples."""
    if not observations:
        raise OffsetInconsistentError("no observations")
    offsets = {obs["implied_offset_seconds"] for obs in observations}
    if len(offsets) != 1:
        raise OffsetInconsistentError("samples disagree on the offset")
    return {"consistent": True, "offset_seconds": next(iter(offsets)), "samples": len(observations)}


# --- observation driver ------------------------------------------------------

def _read_utc(utc_fn):
    """Read the independent UTC clock, converting any failure to a value-blind error."""
    try:
        value = utc_fn()
    except Exception:
        raise IndependentUtcUnavailableError("independent UTC source unavailable")
    if value is None:
        raise IndependentUtcUnavailableError("independent UTC source returned nothing")
    return value


def run_single_observation(transport, token_provider, expected_server, utc_fn):
    """One read: DEMO/server confirmation + three completed bars + offset against UTC."""
    t_before = _read_utc(utc_fn)
    account = transport.get_json(ACCOUNT_PATH, token_provider)
    classify_demo(account)                      # fail closed unless DEMO
    confirm_server(account, expected_server)    # fail closed unless approved target
    rates = transport.get_json(
        "%s?symbol=%s&timeframe=%s&count=%d" % (RATES_PATH, SYMBOL, TIMEFRAME, RATES_COUNT),
        token_provider,
    )
    t_after = _read_utc(utc_fn)
    window = abs(float(t_after) - float(t_before))
    if window > CAPTURE_WINDOW_MAX_S:
        raise IndependentUtcUnavailableError("independent UTC capture window too wide")
    utc_epoch = int((float(t_before) + float(t_after)) / 2.0)
    completed = select_completed_bars(rates)
    server_anchor = completed[-1] + BAR_SECONDS   # latest completed END = forming start
    offset, residual = compute_offset(server_anchor, utc_epoch)
    assert_not_dst_ambiguous(utc_epoch)
    return {
        "server_anchor_epoch_s": server_anchor,
        "utc_epoch_s": utc_epoch,
        "implied_offset_seconds": offset,
        "residual_seconds": residual,
        "completed_bars": list(completed),
    }


def run_probe(transport, token_provider, expected_server, samples=DEFAULT_SAMPLES,
              utc_fn=time.time, sleep_fn=time.sleep, inter_sample_delay_s=1.0,
              correlation_id=None):
    """Drive ``samples`` independent observations and assemble a value-blind result."""
    if samples < 1:
        raise OffsetInconsistentError("samples must be >= 1")
    observations = []
    for idx in range(samples):
        if idx:
            sleep_fn(inter_sample_delay_s)
        observations.append(run_single_observation(transport, token_provider, expected_server, utc_fn))
    verdict = consistency_verdict(observations)
    demo_label = "DEMO"
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "assessment_status": "VERIFIED",
        "demo_classification": demo_label,
        "broker_server_identifier": expected_server.strip(),
        "completed_bar_server_epochs": observations[0]["completed_bars"],
        "independent_utc_epochs": [obs["utc_epoch_s"] for obs in observations],
        "implied_offsets_seconds": [obs["implied_offset_seconds"] for obs in observations],
        "residuals_seconds": [obs["residual_seconds"] for obs in observations],
        "consistency": verdict,
        "offset_hours": verdict["offset_seconds"] // 3600,
        "samples": len(observations),
        "correlation_id": correlation_id or _new_correlation_id(utc_fn),
        "observation_time_utc": _iso_utc(observations[-1]["utc_epoch_s"]),
        "method": METHOD_REF,
        "limitations": [
            "offset is the broker server-time offset observed at this instant only",
            "host clock must be NTP-synced; UTC is the host's independent clock",
            "a single season's observation; cross-season mapping needs its own evidence",
        ],
    }
    return sanitize_result(result)


# --- sanitization & presentation ---------------------------------------------

def sanitize_result(result):
    """Enforce the value-blind output contract (allow-list keys; forbidden-substring guard)."""
    extra = set(result) - ALLOWED_OUTPUT_KEYS
    if extra:
        raise ProbeError("output contains non-allow-listed keys")
    _assert_no_forbidden_keys(result)
    return result


def _assert_no_forbidden_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            low = str(key).lower()
            for bad in FORBIDDEN_OUTPUT_KEY_SUBSTRINGS:
                if bad in low:
                    raise ProbeError("output contains a forbidden key")
            _assert_no_forbidden_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_forbidden_keys(item)


def _iso_utc(epoch_s):
    import datetime
    return datetime.datetime.fromtimestamp(
        int(epoch_s), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_correlation_id(utc_fn):
    # Opaque, non-sensitive id: not derived from any account/credential value.
    return "tim-winter-tz-%d" % int(utc_fn())


def failure_result(exc, correlation_id=None, utc_fn=time.time):
    code = getattr(exc, "reason_code", "probe_error")
    # Inconsistent/DST map to schema assessment statuses; everything else is a hard failure.
    if isinstance(exc, OffsetInconsistentError):
        status = "CONFLICT"
    elif isinstance(exc, DstAmbiguityError):
        status = "INCONCLUSIVE"
    else:
        status = None
    out = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "reason_code": code,
        "correlation_id": correlation_id or _new_correlation_id(utc_fn),
        "observation_time_utc": _iso_utc(int(utc_fn())),
    }
    if status is not None:
        out["assessment_status"] = status
    return sanitize_result(out)


def dry_run_plan(base_url, expected_server, samples):
    return sanitize_result({
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "reason_code": "dry_run_preflight",
        "method": METHOD_REF,
        "samples": samples,
        "limitations": [
            "DRY RUN: no broker read performed; no credential accessed",
            "loopback target: %s" % base_url,
            "approved server target: %s" % expected_server,
            "real read requires --execute AND %s=1 on the bridge host, under Sponsor GO" % ON_HOST_ENV,
        ],
    })


# --- CLI ---------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="TIM winter broker-timezone probe (host-local, read-only, dry-run by default).",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8788",
                        help="loopback bridge base URL (must be loopback)")
    parser.add_argument("--expect-server", required=True,
                        help="approved broker server label; the probe fails closed unless the "
                             "connected server matches it")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help="number of independent observations that must agree")
    parser.add_argument("--execute", action="store_true",
                        help="perform the real loopback read (default is dry-run/preflight). "
                             "Also requires %s=1 in the environment on the bridge host." % ON_HOST_ENV)
    # NOTE: there is intentionally NO token argument. The credential is read only from the
    # process environment; it is never accepted on the command line.
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(dry_run_plan(args.base_url, args.expect_server, args.samples), indent=2))
        return 0
    if os.getenv(ON_HOST_ENV, "") != "1":
        # Fail closed: the real read must be explicitly enabled on the bridge host.
        print(json.dumps(failure_result(ProbeError("on-host confirmation not set")), indent=2),
              file=sys.stderr)
        return 3
    try:
        transport = LoopbackBridgeTransport(args.base_url)
        result = run_probe(transport, default_token_provider, args.expect_server,
                           samples=args.samples)
        print(json.dumps(result, indent=2))
        return 0
    except ProbeError as exc:
        print(json.dumps(failure_result(exc), indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
