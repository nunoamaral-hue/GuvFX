"""Synthetic-only tests for scripts/tim_winter_tz_probe.py (GFX-TIM-WINTER-TIMEZONE-HOST-PROBE-009).

Standard-library unittest only (the governance CI job installs no dependencies). No network,
no broker, no MT5, no credentials: all I/O is mocked and every token value is synthetic.
"""

import calendar
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from urllib.error import HTTPError, URLError

_HERE = os.path.dirname(os.path.abspath(__file__))
_WRAPPER = os.path.join(_HERE, "..", "scripts", "tim_winter_tz_probe.py")

_spec = importlib.util.spec_from_file_location("tim_winter_tz_probe", _WRAPPER)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# Synthetic token: neutral variable name + a placeholder value (secret-scan safe).
SYNTH_TOKEN = "placeholder-synthetic-agent-value-0000"
APPROVED_SERVER = "TradersWay-Demo"


def _synth_token_provider():
    return SYNTH_TOKEN


def _rates(forming_start, cadence=probe.BAR_SECONDS, n_completed=probe.REQUIRED_COMPLETED):
    """Build a valid rates payload: n_completed completed bars + one forming bar (data[-1])."""
    starts = [forming_start - cadence * (n_completed - i) for i in range(n_completed)]
    starts.append(forming_start)  # forming bar last
    data = [{"time": s, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "tick_volume": 1}
            for s in starts]
    return {"ok": True, "symbol": "EURUSD", "timeframe": "M1", "count": len(data), "data": data}


def _account(trade_mode=0, server=APPROVED_SERVER, **extra):
    acct = {"trade_mode": trade_mode, "account_server": server}
    acct.update(extra)
    return acct


class FakeTransport:
    def __init__(self, account=None, rates=None, account_error=None, rates_error=None,
                 token_sink=None):
        self.account = account
        self.rates = rates          # dict, or list consumed per rates call
        self.account_error = account_error
        self.rates_error = rates_error
        self.token_sink = token_sink
        self._ridx = 0

    def get_json(self, path, token_provider):
        tok = token_provider()      # exercises the in-process token read
        if self.token_sink is not None:
            self.token_sink.append(tok)
        if path.startswith(probe.ACCOUNT_PATH):
            if self.account_error:
                raise self.account_error
            return self.account
        if path.startswith(probe.RATES_PATH):
            if self.rates_error:
                raise self.rates_error
            if isinstance(self.rates, list):
                item = self.rates[self._ridx]
                self._ridx += 1
                if isinstance(item, Exception):
                    raise item
                return item
            return self.rates
        raise AssertionError("unexpected path: %s" % path)


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_body(body, header_sink=None):
    def _o(req, timeout=None):
        if header_sink is not None:
            header_sink.append(req.headers)
        return _FakeResp(body)
    return _o


def _opener_raises(exc):
    def _o(req, timeout=None):
        raise exc
    return _o


# A fixed-clock UTC function (two reads identical -> zero capture window).
def _utc_fixed(value):
    return lambda: float(value)


class TestDemoClassification(unittest.TestCase):
    def test_demo_accepted(self):
        self.assertEqual(probe.classify_demo(_account(trade_mode=0)), "DEMO")

    def test_live_rejected(self):
        with self.assertRaises(probe.DemoClassificationError):
            probe.classify_demo(_account(trade_mode=2))

    def test_contest_rejected(self):
        with self.assertRaises(probe.DemoClassificationError):
            probe.classify_demo(_account(trade_mode=1))

    def test_missing_trade_mode_rejected(self):
        with self.assertRaises(probe.DemoClassificationError):
            probe.classify_demo({"account_server": APPROVED_SERVER})

    def test_ambiguous_trade_mode_rejected(self):
        for bad in ("0", None, True, 0.0, [0]):
            with self.assertRaises(probe.DemoClassificationError):
                probe.classify_demo({"trade_mode": bad})

    def test_server_mismatch_rejected(self):
        with self.assertRaises(probe.DemoClassificationError):
            probe.confirm_server(_account(server="SomeOther-Live"), APPROVED_SERVER)

    def test_server_match_returns_approved_label(self):
        self.assertEqual(probe.confirm_server(_account(server=APPROVED_SERVER), APPROVED_SERVER),
                         APPROVED_SERVER)


class TestTokenHandling(unittest.TestCase):
    def test_token_unavailable(self):
        old = os.environ.pop(probe.TOKEN_ENV, None)
        try:
            with self.assertRaises(probe.TokenUnavailableError):
                probe.default_token_provider()
        finally:
            if old is not None:
                os.environ[probe.TOKEN_ENV] = old

    def test_no_token_argument_in_cli(self):
        parser = probe.build_arg_parser()
        option_strings = []
        for action in parser._actions:
            option_strings.extend(action.option_strings)
        for opt in option_strings:
            self.assertNotIn("token", opt.lower())
        # An unknown --token flag must be rejected (never silently accepted).
        with self.assertRaises(SystemExit):
            parser.parse_args(["--expect-server", APPROVED_SERVER, "--token", SYNTH_TOKEN])

    def test_token_absent_from_success_output(self):
        transport = FakeTransport(account=_account(), rates=_rates(1007180))
        result = probe.run_probe(transport, _synth_token_provider, APPROVED_SERVER,
                                 samples=2, utc_fn=_utc_fixed(1000000), sleep_fn=lambda *_: None)
        self.assertNotIn(SYNTH_TOKEN, json.dumps(result))

    def test_token_absent_from_exceptions(self):
        transport = FakeTransport(account=_account(trade_mode=2), rates=_rates(1007180),
                                  token_sink=[])
        try:
            probe.run_probe(transport, _synth_token_provider, APPROVED_SERVER,
                            samples=1, utc_fn=_utc_fixed(1000000), sleep_fn=lambda *_: None)
            self.fail("expected DemoClassificationError")
        except probe.ProbeError as exc:
            self.assertNotIn(SYNTH_TOKEN, str(exc))
            self.assertNotIn(SYNTH_TOKEN, json.dumps(probe.failure_result(exc,
                                                                          utc_fn=_utc_fixed(1000000))))


class TestTransportLoopbackAndAuth(unittest.TestCase):
    def test_non_loopback_host_refused(self):
        with self.assertRaises(probe.NonLoopbackError):
            probe.LoopbackBridgeTransport("http://10.0.0.5:8788")

    def test_non_http_scheme_refused(self):
        with self.assertRaises(probe.NonLoopbackError):
            probe.LoopbackBridgeTransport("https://127.0.0.1:8788")

    def test_loopback_hosts_accepted(self):
        for url in ("http://127.0.0.1:8788", "http://localhost:8788"):
            probe.LoopbackBridgeTransport(url)  # no raise

    def test_endpoint_unreachable(self):
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788",
                                          opener=_opener_raises(URLError("down")))
        with self.assertRaises(probe.EndpointUnreachableError):
            t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)

    def test_auth_rejected_401(self):
        err = HTTPError("http://127.0.0.1/x", 401, "unauthorized", {}, None)
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788", opener=_opener_raises(err))
        with self.assertRaises(probe.AuthRejectedError):
            t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)

    def test_auth_rejected_403(self):
        err = HTTPError("http://127.0.0.1/x", 403, "forbidden", {}, None)
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788", opener=_opener_raises(err))
        with self.assertRaises(probe.AuthRejectedError):
            t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)

    def test_malformed_payload_non_json(self):
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788",
                                          opener=_opener_body(b"<<not json>>"))
        with self.assertRaises(probe.MalformedPayloadError):
            t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)

    def test_malformed_payload_missing_ok(self):
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788",
                                          opener=_opener_body(b'{"data": []}'))
        with self.assertRaises(probe.MalformedPayloadError):
            t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)

    def test_token_delivered_via_header_not_argv(self):
        sink = []
        t = probe.LoopbackBridgeTransport("http://127.0.0.1:8788",
                                          opener=_opener_body(b'{"ok": true}', header_sink=sink))
        t.get_json(probe.ACCOUNT_PATH, _synth_token_provider)
        # the synthetic token was delivered as a header value, nowhere else
        self.assertTrue(any(SYNTH_TOKEN in str(v) for v in sink[0].values()))


class TestBarSelection(unittest.TestCase):
    def test_valid_three_completed(self):
        completed = probe.select_completed_bars(_rates(1007180))
        self.assertEqual(completed, [1007000, 1007060, 1007120])
        self.assertNotIn(1007180, completed)  # forming bar excluded

    def test_fewer_than_three_rejected(self):
        payload = {"ok": True, "data": [{"time": 1007060}, {"time": 1007120}, {"time": 1007180}]}
        with self.assertRaises(probe.BarSelectionError):
            probe.select_completed_bars(payload)

    def test_cadence_break_rejected(self):
        payload = {"ok": True, "data": [{"time": 1007000}, {"time": 1007060},
                                        {"time": 1007180}, {"time": 1007240}]}
        with self.assertRaises(probe.BarSelectionError):
            probe.select_completed_bars(payload)

    def test_forming_bar_noncontiguous_rejected(self):
        payload = {"ok": True, "data": [{"time": 1007000}, {"time": 1007060},
                                        {"time": 1007120}, {"time": 1007240}]}
        with self.assertRaises(probe.BarSelectionError):
            probe.select_completed_bars(payload)

    def test_non_ascending_rejected(self):
        payload = {"ok": True, "data": [{"time": 1007120}, {"time": 1007060},
                                        {"time": 1007000}, {"time": 1006940}]}
        with self.assertRaises(probe.MalformedPayloadError):
            probe.select_completed_bars(payload)

    def test_bar_missing_time_rejected(self):
        payload = {"ok": True, "data": [{"time": 1007000}, {"open": 1.0},
                                        {"time": 1007120}, {"time": 1007180}]}
        with self.assertRaises(probe.MalformedPayloadError):
            probe.select_completed_bars(payload)


class TestOffset(unittest.TestCase):
    def test_summer_positive_control_utc3(self):
        # RULE 11 positive control: reproduce the recorded summer UTC+3 (raw 10776 -> +3h, resid 24).
        offset, residual = probe.compute_offset(1010776, 1000000)
        self.assertEqual(offset, 10800)
        self.assertEqual(residual, 24)

    def test_winter_offset_utc2(self):
        offset, residual = probe.compute_offset(1007180, 1000000)
        self.assertEqual(offset, 7200)
        self.assertEqual(residual, 20)

    def test_residual_out_of_bound_rejected(self):
        with self.assertRaises(probe.OffsetInconsistentError):
            probe.compute_offset(1000000 + 7200 - 400, 1000000)  # residual 400 > 180

    def test_offset_out_of_range_rejected(self):
        with self.assertRaises(probe.OffsetInconsistentError):
            probe.compute_offset(1000000 + 20 * 3600, 1000000)  # +20h out of [-12,+14]


class TestDst(unittest.TestCase):
    def test_dst_boundary_ambiguity_rejected(self):
        # 2026-10-25 01:00:00 UTC = EU autumn transition.
        transition = calendar.timegm((2026, 10, 25, 1, 0, 0, 0, 0, 0))
        with self.assertRaises(probe.DstAmbiguityError):
            probe.assert_not_dst_ambiguous(transition + 60)

    def test_far_from_dst_ok(self):
        safe = calendar.timegm((2026, 11, 18, 10, 17, 41, 0, 0, 0))
        probe.assert_not_dst_ambiguous(safe)  # no raise


class TestConsistencyAndIndependentUtc(unittest.TestCase):
    def test_inconsistent_offsets_rejected(self):
        # two samples whose anchors differ by >1h -> different whole-hour offsets.
        transport = FakeTransport(account=_account(),
                                  rates=[_rates(1007180), _rates(1007180 + 3600)])
        with self.assertRaises(probe.OffsetInconsistentError):
            probe.run_probe(transport, _synth_token_provider, APPROVED_SERVER,
                            samples=2, utc_fn=_utc_fixed(1000000), sleep_fn=lambda *_: None)

    def test_independent_utc_unavailable(self):
        def _bad_utc():
            raise OSError("no clock")
        transport = FakeTransport(account=_account(), rates=_rates(1007180))
        with self.assertRaises(probe.IndependentUtcUnavailableError):
            probe.run_single_observation(transport, _synth_token_provider, APPROVED_SERVER, _bad_utc)


class TestValueBlindOutput(unittest.TestCase):
    def test_happy_path_verified(self):
        transport = FakeTransport(account=_account(), rates=_rates(1007180))
        result = probe.run_probe(transport, _synth_token_provider, APPROVED_SERVER,
                                 samples=3, utc_fn=_utc_fixed(1000000), sleep_fn=lambda *_: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["assessment_status"], "VERIFIED")
        self.assertEqual(result["demo_classification"], "DEMO")
        self.assertEqual(result["offset_hours"], 2)
        self.assertEqual(result["broker_server_identifier"], APPROVED_SERVER)
        self.assertTrue(result["consistency"]["consistent"])
        self.assertEqual(result["samples"], 3)

    def test_output_excludes_sensitive_account_fields(self):
        # account payload carries balance/equity/account_login/currency; none may leak.
        # (distinctive values chosen so they cannot be substrings of the synthetic epochs.)
        acct = _account(account_login=948177331, balance=73313.55, equity=73950.10,
                        currency="USD")
        transport = FakeTransport(account=acct, rates=_rates(1007180))
        result = probe.run_probe(transport, _synth_token_provider, APPROVED_SERVER,
                                 samples=1, utc_fn=_utc_fixed(1000000), sleep_fn=lambda *_: None)
        self.assertLessEqual(set(result), probe.ALLOWED_OUTPUT_KEYS)
        blob = json.dumps(result)
        for leak in ("948177331", "73313", "73950", "balance", "equity",
                     "account_login", "currency", "USD"):
            self.assertNotIn(leak, blob)

    def test_sanitize_rejects_forbidden_key(self):
        with self.assertRaises(probe.ProbeError):
            probe.sanitize_result({"schema_version": "x", "account_login": 1})


class TestCliDryRunAndGuards(unittest.TestCase):
    def test_dry_run_is_default_and_makes_no_call(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = probe.main(["--expect-server", APPROVED_SERVER])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("dry_run_preflight", out)
        self.assertNotIn(SYNTH_TOKEN, out)

    def test_execute_requires_on_host_flag(self):
        old = os.environ.pop(probe.ON_HOST_ENV, None)
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                rc = probe.main(["--expect-server", APPROVED_SERVER, "--execute"])
            self.assertEqual(rc, 3)  # fail closed: on-host confirmation absent
        finally:
            if old is not None:
                os.environ[probe.ON_HOST_ENV] = old


class TestNoMutationCapability(unittest.TestCase):
    def test_source_has_no_trading_or_io_capability(self):
        with open(_WRAPPER, "r", encoding="utf-8") as fh:
            src = fh.read()
        forbidden = [
            "order_send", "order_check", "position_open", "copy_rates",
            "import mt5", "import MetaTrader5", "mt5.initialize",
            "import requests", "subprocess", "shutil", "os.remove", "os.rmdir",
            "mount_smbfs", "smbclient", "paramiko",
        ]
        for token in forbidden:
            self.assertNotIn(token, src, "wrapper must not reference %r" % token)

    def test_stdlib_only_imports(self):
        with open(_WRAPPER, "r", encoding="utf-8") as fh:
            src = fh.read()
        # No third-party HTTP client; loopback uses urllib (stdlib).
        self.assertIn("from urllib.request import", src)
        self.assertNotIn("requests", src)


if __name__ == "__main__":
    unittest.main()
