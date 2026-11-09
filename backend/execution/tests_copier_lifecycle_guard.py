"""Synthetic tests for the pure copier lifecycle guard (ADR-0084): pre-state / revision / deadline /
idempotency admission + explicit outcome classification. The guard is pure (no MT5, no DB); it is loaded from
scripts/ exactly as the other bridge tests load mt5_signal_bridge. Nothing here contacts a broker.
"""
import importlib.util
import os

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_GUARD_PATH = os.path.join(_REPO, "scripts", "mt5_copier_lifecycle_guard.py")


def _load():
    spec = importlib.util.spec_from_file_location("mt5_copier_lifecycle_guard_under_test", _GUARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load()

# A valid command deadline well in the future (epoch); "now" is fixed below.
NOW = 1_000_000.0
FUTURE = NOW + 600.0
PAST = NOW - 1.0


def _cmd(op, **over):
    c = {
        "copier_operation": op,
        "ticket": 777,
        "symbol": "XAUUSD.std",
        "require_identity_pin": True,
        "expected_login": "51763831",
        "expected_server": "IS6Technologies-Live",
        "master_revision": 20,
        "authorization_expires_at": FUTURE,
        "expected_pre_state": {"ticket": 777, "symbol": "XAUUSD.std", "position_open": True},
        "requested_post_state": {},
    }
    c.update(over)
    return c


def _snap(**over):
    s = {"exists": True, "ticket": 777, "symbol": "XAUUSD.std", "volume": 0.02, "sl": 4000.0, "tp": 3970.0,
         "login": "51763831", "server": "IS6Technologies-Live"}
    s.update(over)
    return s


def _admit(cmd, snap, **kw):
    kw.setdefault("now_epoch", NOW)
    kw.setdefault("last_applied_revision", 10)
    kw.setdefault("point", 0.01)
    kw.setdefault("volume_step", 0.01)
    return G.admit_mutation(cmd, snap, **kw)


class GuardDarkTests(SimpleTestCase):
    def test_default_off(self):
        self.assertFalse(G.guard_enabled(getenv=lambda k, d="": ""))
        self.assertTrue(G.guard_enabled(getenv=lambda k, d="": "1"))


class AdmitModifyTests(SimpleTestCase):
    def test_proceed(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap(sl=4000.0, tp=3970.0)), (G.PROCEED, "ok"))

    def test_already_applied(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "4000", "tp": "3970"})
        self.assertEqual(_admit(cmd, _snap(sl=4000.0, tp=3970.0))[0], G.ALREADY_APPLIED)

    def test_deadline_exceeded(self):
        cmd = _cmd(G.OP_MODIFY, authorization_expires_at=PAST, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap())[0], G.DEADLINE_EXCEEDED)

    def test_stale_revision(self):
        cmd = _cmd(G.OP_MODIFY, master_revision=10, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap(), last_applied_revision=10)[0], G.STALE_REVISION)

    def test_position_not_found(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, {"exists": False})[0], G.POSITION_NOT_FOUND)

    def test_login_mismatch_is_pre_state(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        out, reason = _admit(cmd, _snap(login="99999999"))
        self.assertEqual(out, G.PRE_STATE_MISMATCH)
        self.assertEqual(reason, "login_mismatch")

    def test_symbol_mismatch_is_pre_state(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap(symbol="EURUSD"))[0], G.PRE_STATE_MISMATCH)

    def test_missing_deadline_contained(self):
        cmd = _cmd(G.OP_MODIFY, authorization_expires_at=None, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap())[0], G.CONTAINED_FAILURE)

    def test_iso_deadline_parsed(self):
        cmd = _cmd(G.OP_MODIFY, authorization_expires_at="2099-01-01T00:00:00+00:00",
                   requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap())[0], G.PROCEED)

    def test_add_sl_to_unprotected(self):
        # position has no SL/TP; add SL -> proceed (not already-applied).
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": None})
        self.assertEqual(_admit(cmd, _snap(sl=0.0, tp=0.0))[0], G.PROCEED)


class AdmitPartialTests(SimpleTestCase):
    def _pc(self, **over):
        base = dict(
            expected_pre_state={"ticket": 777, "symbol": "XAUUSD.std", "position_open": True,
                                "expected_current_volume": "0.02"},
            requested_post_state={"reduce_by": "0.01", "expected_remaining": "0.01"})
        base.update(over)
        return _cmd(G.OP_PARTIAL, **base)

    def test_proceed(self):
        self.assertEqual(_admit(self._pc(), _snap(volume=0.02)), (G.PROCEED, "ok"))

    def test_already_reduced(self):
        self.assertEqual(_admit(self._pc(), _snap(volume=0.01))[0], G.ALREADY_APPLIED)

    def test_volume_pre_state_mismatch(self):
        self.assertEqual(_admit(self._pc(), _snap(volume=0.03))[0], G.PRE_STATE_MISMATCH)

    def test_reduction_not_strictly_reducing(self):
        cmd = self._pc(requested_post_state={"reduce_by": "0.02", "expected_remaining": "0.00"})
        self.assertEqual(_admit(cmd, _snap(volume=0.02))[0], G.PRE_STATE_MISMATCH)

    def test_missing_partial_volumes_contained(self):
        cmd = self._pc(requested_post_state={"reduce_by": None, "expected_remaining": None})
        self.assertEqual(_admit(cmd, _snap(volume=0.02))[0], G.CONTAINED_FAILURE)

    def test_volume_step_tolerance(self):
        # a float-noise volume within half a step still matches the expected current.
        self.assertEqual(_admit(self._pc(), _snap(volume=0.020000001))[0], G.PROCEED)


class AdmitFullCloseTests(SimpleTestCase):
    def test_proceed(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(_admit(cmd, _snap()), (G.PROCEED, "ok"))

    def test_already_closed_idempotent(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(_admit(cmd, {"exists": False})[0], G.ALREADY_APPLIED)

    def test_unknown_op_contained(self):
        self.assertEqual(_admit(_cmd("SCALE_IN"), _snap())[0], G.CONTAINED_FAILURE)

    def test_ticket_mismatch_is_pre_state(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(_admit(cmd, _snap(ticket=888))[0], G.PRE_STATE_MISMATCH)


class AdversarialFixTests(SimpleTestCase):
    """Regression tests for the confirmed adversarial findings (ADR-0084 review)."""

    def test_nan_deadline_fails_closed(self):
        # HIGH-1: a NaN/Inf deadline must NOT pass as a never-expiring authorization.
        for bad in (float("nan"), "nan", float("inf")):
            cmd = _cmd(G.OP_MODIFY, authorization_expires_at=bad,
                       requested_post_state={"sl": "3990", "tp": "3960"})
            self.assertEqual(_admit(cmd, _snap())[0], G.CONTAINED_FAILURE, bad)

    def test_nan_volume_not_proceed(self):
        # HIGH-1: a NaN follower volume must never admit a partial close (fail-closed, not PROCEED).
        cmd = _cmd(G.OP_PARTIAL,
                   expected_pre_state={"ticket": 777, "symbol": "XAUUSD.std", "position_open": True},
                   requested_post_state={"reduce_by": "0.01", "expected_remaining": "0.01"})
        self.assertNotEqual(_admit(cmd, _snap(volume=float("nan")))[0], G.PROCEED)

    def test_modify_empty_post_state_fails_closed(self):
        # HIGH-2: a degenerate MODIFY with no numeric target must NOT proceed (would strip protection)...
        cmd = _cmd(G.OP_MODIFY, requested_post_state={})
        self.assertEqual(_admit(cmd, _snap(sl=4000.0, tp=3970.0))[0], G.CONTAINED_FAILURE)
        # ...and must NOT be a false ALREADY_APPLIED against an unprotected follower (silent skip of a real modify).
        self.assertEqual(_admit(cmd, _snap(sl=0.0, tp=0.0))[0], G.CONTAINED_FAILURE)

    def test_naive_iso_deadline_is_utc(self):
        # MEDIUM-3: a tz-less ISO deadline is interpreted as UTC, not host-local.
        self.assertEqual(G._deadline_epoch({"authorization_expires_at": "2099-01-01T00:00:00"}),
                         G._deadline_epoch({"authorization_expires_at": "2099-01-01T00:00:00+00:00"}))

    def test_non_integer_revision_fails_closed(self):
        # MEDIUM-4: a non-integer/garbage revision returns a terminal outcome, never raises.
        for bad in ("20.5", "abc", [1], None):
            cmd = _cmd(G.OP_MODIFY, master_revision=bad, requested_post_state={"sl": "3990", "tp": "3960"})
            self.assertEqual(_admit(cmd, _snap())[0], G.CONTAINED_FAILURE, bad)

    def test_last_applied_none_does_not_raise(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(_admit(cmd, _snap(), last_applied_revision=None)[0], G.PROCEED)

    def test_missing_identity_or_symbol_fails_closed(self):
        # LOW-5: identity + symbol are MANDATORY for a copier mutation.
        for over in ({"require_identity_pin": False}, {"expected_login": ""}, {"expected_server": ""},
                     {"symbol": "", "expected_pre_state": {"ticket": 777, "position_open": True}}):
            cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True}, **over)
            self.assertEqual(_admit(cmd, _snap())[0], G.CONTAINED_FAILURE, over)


class ClassifyResultTests(SimpleTestCase):
    def _c(self, cmd, **kw):
        kw.setdefault("point", 0.01)
        kw.setdefault("volume_step", 0.01)
        return G.classify_result(cmd, **kw)

    def test_full_close_applied(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot={"exists": False})[0], G.APPLIED)

    def test_full_close_not_reflected_contained(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot=_snap())[0], G.CONTAINED_FAILURE)

    def test_modify_applied(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot=_snap(sl=3990.0, tp=3960.0))[0], G.APPLIED)

    def test_partial_applied(self):
        cmd = _cmd(G.OP_PARTIAL, requested_post_state={"reduce_by": "0.01", "expected_remaining": "0.01"})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot=_snap(volume=0.01))[0], G.APPLIED)

    def test_broker_rejected(self):
        cmd = _cmd(G.OP_MODIFY, requested_post_state={"sl": "3990", "tp": "3960"})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=True,
                                 post_snapshot=_snap())[0], G.BROKER_REJECTED)

    def test_indeterminate_is_unknown(self):
        # Timeout / lost connection -> NEVER success; must reconcile.
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(self._c(cmd, send_determinate=False, broker_rejected=False,
                                 post_snapshot=_snap())[0], G.UNKNOWN_OUTCOME)

    def test_no_post_snapshot_is_unknown(self):
        cmd = _cmd(G.OP_FULL, requested_post_state={"closed": True})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot=None)[0], G.UNKNOWN_OUTCOME)

    def test_partial_position_gone_contained(self):
        # A PARTIAL that left no position is ambiguous (over-close/external) -> contain, never "applied".
        cmd = _cmd(G.OP_PARTIAL, requested_post_state={"reduce_by": "0.01", "expected_remaining": "0.01"})
        self.assertEqual(self._c(cmd, send_determinate=True, broker_rejected=False,
                                 post_snapshot={"exists": False})[0], G.CONTAINED_FAILURE)
