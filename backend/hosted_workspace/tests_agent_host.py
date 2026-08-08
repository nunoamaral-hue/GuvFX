"""ADR-0034 / M3b-2 — the reference Mt5WorkspaceHost adapter (M1-only, read-only, never login/launch).

A spy ``mt5`` records every call so the tests can PROVE the security-critical invariants directly:
- the adapter reaches MT5 only through the injected M1 ``guarded_initialize`` (never ``mt5.initialize`` /
  ``mt5.login`` directly);
- it passes ONLY ``path`` to the guarded attach (no login/password/server) — no credential replay;
- it never launches a terminal and never places/modifies/closes an order;
- reads are read-only and fail closed to None; ``release`` calls ``mt5.shutdown`` once;
- wired end-to-end through the pure agent + certified producer, a healthy workspace yields a positive
  WorkspaceObservation and a wrong account is denied — with no login/order call on the spy.
"""
from django.test import SimpleTestCase

from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.agent import WorkspaceSpec, observe_workspace
from hosted_workspace.agent_host import Mt5WorkspaceHost


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SpyMt5:
    """Records calls. ``login``/``initialize`` are present but MUST never be called by the adapter."""

    def __init__(self, *, connected=True, trade_allowed=True, login=12345, server="Demo", trade_mode=0,
                 positions=(), orders=(), tick=True, account=True, terminal=True):
        self._connected, self._trade_allowed = connected, trade_allowed
        self._login, self._server, self._trade_mode = login, server, trade_mode
        self._positions, self._orders, self._tick = positions, orders, tick
        self._account, self._terminal = account, terminal
        self.calls = []

    def login(self, *a, **k):
        self.calls.append("login")
        raise AssertionError("mt5.login must never be called by the workspace agent")

    def initialize(self, *a, **k):
        self.calls.append("initialize")
        raise AssertionError("mt5.initialize must only be called via the injected M1 guarded_initialize")

    def terminal_info(self):
        self.calls.append("terminal_info")
        return _Obj(connected=self._connected, trade_allowed=self._trade_allowed) if self._terminal else None

    def account_info(self):
        self.calls.append("account_info")
        return _Obj(login=self._login, server=self._server, trade_mode=self._trade_mode) \
            if self._account else None

    def positions_get(self):
        self.calls.append("positions_get")
        return self._positions

    def orders_get(self):
        self.calls.append("orders_get")
        return self._orders

    def symbol_info_tick(self, symbol):
        self.calls.append("symbol_info_tick")
        return _Obj(bid=1.0, ask=1.0) if self._tick else None

    def shutdown(self):
        self.calls.append("shutdown")


def _spec(**kw):
    base = dict(workspace_id="ws-1", expected_login="12345", expected_server="Demo",
                target_path="C:/w/terminal64.exe", freshness_limit_seconds=60.0, tick_symbol="EURUSD")
    base.update(kw)
    return WorkspaceSpec(**base)


class _FakeM1:
    """Mirrors the M1 guarded_initialize contract enough to assert the adapter's use of it: refuses
    credential keys, records the init_kwargs it received, returns a configurable ok."""

    def __init__(self, ok=True):
        self.ok = ok
        self.received = None
        self.calls = 0

    def guarded_initialize(self, mt5, init_kwargs, *, probe=None):
        self.calls += 1
        self.received = dict(init_kwargs)
        if any(k in init_kwargs for k in ("login", "password", "server")):
            raise AssertionError("credential keys are forbidden on the guarded attach path")
        return self.ok


def _running(path):
    return True


def _not_running(path):
    return False


def _host(mt5, *, m1=None, probe=_running):
    m1 = m1 or _FakeM1(ok=True)
    return Mt5WorkspaceHost(mt5, guarded_initialize=m1.guarded_initialize,
                            terminal_process_running=probe), m1


class LocateTests(SimpleTestCase):
    def test_running(self):
        host, _ = _host(SpyMt5())
        self.assertTrue(host.locate(_spec()).running)

    def test_not_running(self):
        host, _ = _host(SpyMt5(), probe=_not_running)
        self.assertFalse(host.locate(_spec()).running)

    def test_no_path(self):
        host, _ = _host(SpyMt5())
        probe = host.locate(_spec(target_path=None))
        self.assertFalse(probe.running)
        self.assertEqual(probe.reason, "no_path")


class AttachTests(SimpleTestCase):
    def test_attach_uses_m1_with_path_only(self):  # no credential replay
        spy = SpyMt5()
        host, m1 = _host(spy)
        out = host.attach(_spec(expected_login="12345"))
        self.assertTrue(out.ok and out.ipc_available)
        self.assertEqual(m1.received, {"path": "C:/w/terminal64.exe"})  # ONLY path — no login/pwd/server
        self.assertNotIn("login", spy.calls)
        self.assertNotIn("initialize", spy.calls)

    def test_attach_refused_maps_to_not_ok(self):
        host, _ = _host(SpyMt5(), m1=_FakeM1(ok=False))
        out = host.attach(_spec())
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "guarded_attach_refused")


class ReadStateTests(SimpleTestCase):
    def test_reads_are_read_only(self):
        spy = SpyMt5(positions=(1, 2), orders=(3,), tick=True)
        host, _ = _host(spy)
        state = host.read_state(_spec())
        self.assertEqual(state.terminal, {"connected": True, "trade_allowed": True})
        self.assertEqual(state.account, {"login": 12345, "server": "Demo", "trade_mode": 0})
        self.assertEqual((state.position_count, state.order_count, state.tick_present), (2, 1, True))
        self.assertNotIn("login", spy.calls)

    def test_account_none_when_unavailable(self):
        host, _ = _host(SpyMt5(account=False))
        self.assertIsNone(host.read_state(_spec()).account)

    def test_tick_not_probed_without_symbol(self):
        host, _ = _host(SpyMt5())
        self.assertIsNone(host.read_state(_spec(tick_symbol=None)).tick_present)

    def test_release_calls_shutdown_once(self):
        spy = SpyMt5()
        host, _ = _host(spy)
        host.release()
        self.assertEqual(spy.calls.count("shutdown"), 1)


class EndToEndTests(SimpleTestCase):
    def test_healthy_workspace_positive_observation_no_login(self):
        spy = SpyMt5(login=12345, server="Demo", trade_mode=0)
        host, _ = _host(spy)
        obs = observe_workspace(host, _spec(), clock=lambda: 1000.0, previous_state=str(S.CONNECTED))
        self.assertEqual(
            (obs.process_running, obs.ipc_available, obs.connected, obs.account_match, obs.trade_allowed,
             obs.fresh),
            (True, True, True, True, True, True))
        self.assertNotIn("login", spy.calls)
        self.assertNotIn("initialize", spy.calls)

    def test_wrong_account_denied_read_only(self):
        spy = SpyMt5(login=99999, server="Demo", trade_mode=0)
        host, _ = _host(spy)
        obs = observe_workspace(host, _spec(expected_login="12345"), clock=lambda: 1000.0,
                                previous_state=str(S.CONNECTED))
        self.assertFalse(obs.account_match)
        self.assertNotIn("login", spy.calls)

    def test_attach_refused_reads_never_happen(self):
        spy = SpyMt5()
        host, _ = _host(spy, m1=_FakeM1(ok=False))
        obs = observe_workspace(host, _spec(), clock=lambda: 1000.0, previous_state=str(S.CONNECTED))
        self.assertFalse(obs.ipc_available)
        self.assertNotIn("account_info", spy.calls)  # no read after a refused attach
        self.assertEqual(spy.calls.count("shutdown"), 1)  # released
