"""ADR-0034 Execution Engine (G3) — server-derived per-job identity pin for Hosted Workspace (Provider B).

Proves: the pin is derived server-side from the account's durable bindings; it is injected at the SINGLE
creation boundary (``ExecutionJob.save``) for every mutation job type; it is a total no-op for Provider A /
Customer Zero and while the subsystem is dark (regression-identical legacy path); it fails closed (pin still
required) when a binding value is missing; and it never clobbers a caller-supplied pin. Plus a runnable
mutation harness on the pure predicates.
"""
from __future__ import annotations

import inspect
import textwrap

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from trading.models import BrokerServer, TradingAccount

from execution import hosted_pin as HP
from execution.hosted_pin import (hosted_windows_username_for, identity_pin_for, inject_identity_pin,
                                  is_hosted_workspace_account)
from execution.models import ExecutionJob
from execution.readiness import PERSISTENT_WORKSPACE, TEMPORARY_VALIDATION
from terminal_provisioning.models import AccountProvisioning

U = get_user_model()

PIN_KEYS = {"require_identity_pin", "expected_login", "expected_server", "is_demo", "windows_username"}


def _account(provider=TEMPORARY_VALIDATION, *, login="500123", server_name="Demo-Srv", is_demo=True,
             with_server=True, provisioned_username=None, provisioning_status=None):
    user = U.objects.create_user(username=f"u{login}{provider}", email=f"{login}{provider}@x.invalid",
                                 password="x")
    server = None
    if with_server:
        server, _ = BrokerServer.objects.get_or_create(server_name=server_name)  # server_name is unique
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo,
        broker_server=server, readiness_provider=provider)
    # Optional per-account isolation profile (the authoritative Windows-tenant system of record). Defaults to
    # PROVISIONED when only a username is given; pass ``provisioning_status`` to exercise fail-closed states.
    if provisioned_username is not None:
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username=provisioned_username,
            runtime_root=f"C:\\GuvFX\\accounts\\{acct.id}",
            status=provisioning_status or AccountProvisioning.Status.PROVISIONED)
    return acct


class IdentityPinForTests(TestCase):
    def test_non_provider_b_is_empty_even_when_enabled(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            acct = _account(provider=TEMPORARY_VALIDATION)
            self.assertEqual(identity_pin_for(acct), {})
            self.assertFalse(is_hosted_workspace_account(acct))

    def test_none_account_is_empty(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            self.assertEqual(identity_pin_for(None), {})

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_pins_server_derived_identity(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", server_name="IS6-Demo", is_demo=True,
                        provisioned_username="guvfx_u_700900")
        pin = identity_pin_for(acct)
        self.assertEqual(pin, {"require_identity_pin": True, "expected_login": "700900",
                               "expected_server": "IS6-Demo", "is_demo": True,
                               "windows_username": "guvfx_u_700900"})

    def test_provider_b_dark_is_empty(self):
        # Flag OFF (default): the legacy env-pin path is unchanged — no per-job pin.
        acct = _account(provider=PERSISTENT_WORKSPACE)
        self.assertEqual(identity_pin_for(acct), {})
        self.assertFalse(is_hosted_workspace_account(acct))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_missing_server_still_requires_pin_fail_closed(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", with_server=False)
        pin = identity_pin_for(acct)
        self.assertTrue(pin["require_identity_pin"])   # pin STILL required
        self.assertEqual(pin["expected_server"], "")   # empty ⇒ the bridge fails closed, never trades unpinned
        self.assertEqual(pin["expected_login"], "700900")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_pin_carries_live_classification(self):
        live = _account(provider=PERSISTENT_WORKSPACE, login="900001", is_demo=False)
        self.assertFalse(identity_pin_for(live)["is_demo"])  # demo/live travels with the pin (PART F)


class InjectPinTests(TestCase):
    def _job(self, account, job_type=ExecutionJob.JobType.CLOSE_TRADE, payload=None):
        # CLOSE_TRADE/MODIFY are NOT gate/kill-blocked, so save() runs without the broker gate interfering.
        return ExecutionJob(job_type=job_type, account=account, payload=payload or {"ticket": 1})

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_close_job_is_pinned_via_save(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", server_name="IS6-Demo")
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertTrue(job.payload["require_identity_pin"])
        self.assertEqual(job.payload["expected_login"], "700900")
        self.assertEqual(job.payload["expected_server"], "IS6-Demo")
        self.assertEqual(job.payload["ticket"], 1)  # original payload preserved

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_modify_job_is_pinned(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct, ExecutionJob.JobType.MODIFY_POSITION)
        job.save()
        job.refresh_from_db()
        self.assertTrue(job.payload["require_identity_pin"])

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_a_job_is_not_pinned(self):  # regression: Provider A untouched
        acct = _account(provider=TEMPORARY_VALIDATION)
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    def test_dark_job_is_not_pinned(self):  # regression: subsystem dark ⇒ no payload change
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_non_mutation_type_is_not_pinned(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = ExecutionJob(job_type=ExecutionJob.JobType.SYNC_POSITIONS, account=acct, payload={})
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_inject_never_clobbers_caller_supplied_pin(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900")
        job = self._job(acct, payload={"expected_login": "CALLER_SET", "ticket": 9})
        inject_identity_pin(job)
        self.assertEqual(job.payload["expected_login"], "CALLER_SET")  # setdefault preserves the caller's
        self.assertTrue(job.payload["require_identity_pin"])           # but fills the missing keys

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_require_pin_enable_flag_cannot_be_disabled_by_payload(self):
        # The ENABLE flag is safety-critical: a payload can never turn the pin OFF for a hosted mutation.
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900")
        job = self._job(acct, payload={"require_identity_pin": False, "ticket": 3})
        inject_identity_pin(job)
        self.assertTrue(job.payload["require_identity_pin"])   # forced on, not setdefault
        self.assertEqual(job.payload["expected_login"], "700900")

    def test_inject_returns_false_when_dark(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct)
        self.assertFalse(inject_identity_pin(job))


class MutationAdequacyTests(TestCase):
    """Runnable mutation harness for the pin's pure decision. Each mutant must be KILLED by an oracle."""

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_identity_pin_for_oracle(self):
        b = _account(provider=PERSISTENT_WORKSPACE, login="1", server_name="S")
        a = _account(provider=TEMPORARY_VALIDATION, login="2", server_name="S")
        self.assertEqual(identity_pin_for(b)["expected_login"], "1")
        self.assertEqual(identity_pin_for(a), {})  # the provider comparison is load-bearing

    def test_provider_comparison_mutant_is_killed(self):
        # `== PERSISTENT_WORKSPACE` -> `!= PERSISTENT_WORKSPACE`: a Provider-B account would go empty and a
        # Provider-A account would get pinned — both wrong. Mutate the source of is_hosted_workspace_account.
        src = textwrap.dedent(inspect.getsource(HP.is_hosted_workspace_account))
        mutant_src = src.replace("== PERSISTENT_WORKSPACE", "!= PERSISTENT_WORKSPACE", 1)
        self.assertIn("!= PERSISTENT_WORKSPACE", mutant_src)
        ns = {"PERSISTENT_WORKSPACE": PERSISTENT_WORKSPACE, "_provider_b_pin_enabled": lambda: True}
        exec(compile(mutant_src, "<mutant>", "exec"), ns)
        mutant = ns["is_hosted_workspace_account"]

        class _B:  # a Provider-B-shaped object
            readiness_provider = PERSISTENT_WORKSPACE
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            self.assertTrue(is_hosted_workspace_account_via(_B()))   # real: Provider B ⇒ True
        self.assertFalse(mutant(_B()))  # mutant flips it ⇒ killed


def is_hosted_workspace_account_via(obj):
    """Call the real predicate against a shaped object (helper for the mutation oracle)."""
    return is_hosted_workspace_account(obj)


class HostedWindowsUsernameTests(TestCase):
    """ADR-0034 Option-A — the authoritative, server-derived hosted Windows tenant identity in the pin.

    Reproduces the exact account-25 topology that failed real signal plan 243: a hosted (persistent-workspace)
    account with NO legacy ``mt5_instance``, a PROVISIONED ``AccountProvisioning`` (the isolation
    system-of-record), on Node 2. Proves the pin now carries a non-empty, server-derived ``windows_username``,
    that it can never be customer-supplied, and that every not-provisioned state fails closed.
    """

    # ---- resolver: authoritative source + fail-closed states -----------------------------------------
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provisioned_resolves_authoritative_username(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        self.assertEqual(hosted_windows_username_for(acct), "guvfx_u_25")
        self.assertEqual(identity_pin_for(acct)["windows_username"], "guvfx_u_25")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_no_isolation_profile_is_empty_fail_closed(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587")  # hosted but NOT provisioned
        self.assertEqual(hosted_windows_username_for(acct), "")
        self.assertEqual(identity_pin_for(acct)["windows_username"], "")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_non_provisioned_statuses_fail_closed(self):
        for st in (AccountProvisioning.Status.PENDING, AccountProvisioning.Status.DISABLED,
                   AccountProvisioning.Status.RETIRED):
            acct = _account(provider=PERSISTENT_WORKSPACE, login=f"70{st}",
                            provisioned_username=f"guvfx_u_{st}", provisioning_status=st)
            self.assertEqual(hosted_windows_username_for(acct), "",
                             f"status={st} must NOT be dispatchable (fail-closed)")

    def test_dark_subsystem_no_resolution(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        self.assertEqual(hosted_windows_username_for(acct), "")  # flag OFF ⇒ not hosted ⇒ empty

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_admin_identity_is_refused_fail_closed(self):
        # SECURITY: a customer order must NEVER dispatch under an administrator Windows identity — even a
        # PROVISIONED profile is refused (fails closed) if it is erroneously is_admin=True.
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        acct.isolation_profile.is_admin = True
        acct.isolation_profile.save(update_fields=["is_admin"])
        self.assertEqual(hosted_windows_username_for(acct), "")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_legacy_account_gets_no_pin_windows_username(self):
        # A Provider-A / legacy account NEVER gets the hosted resolver — pin is {} (its windows_username, if
        # any, stays the legacy mt5_instance value set by _order_payload). Customer-Zero isolation.
        acct = _account(provider=TEMPORARY_VALIDATION, login="1", provisioned_username="guvfx_u_1")
        self.assertEqual(identity_pin_for(acct), {})
        self.assertEqual(hosted_windows_username_for(acct), "")

    # ---- injection: FORCED (anti-spoof, anti-legacy-null) --------------------------------------------
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_forced_over_legacy_null(self):
        # _order_payload pre-seeds windows_username=None from the (absent) mt5_instance; the pin must FORCE
        # the authoritative value over that null (a plain setdefault would leave the null the worker rejects).
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        job = ExecutionJob(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                           payload={"symbol": "XAUUSD", "side": "SELL", "lots": "0.40", "comment": "WAY1L1",
                                    "windows_username": None})
        self.assertTrue(inject_identity_pin(job))
        self.assertEqual(job.payload["windows_username"], "guvfx_u_25")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_customer_supplied_windows_username_cannot_be_spoofed(self):
        # SECURITY: a caller-supplied windows_username is OVERWRITTEN by the server-authoritative value — the
        # identity can never be chosen by (customer-controlled) payload input.
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        job = ExecutionJob(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                           payload={"symbol": "XAUUSD", "side": "SELL", "lots": "0.40", "comment": "c",
                                    "windows_username": "guvfx_u_1"})  # attempt to target Customer Zero's tenant
        inject_identity_pin(job)
        self.assertEqual(job.payload["windows_username"], "guvfx_u_25")  # forced back to THIS account's tenant

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_unprovisioned_forces_empty_not_caller_value(self):
        # Fail-closed + anti-spoof together: no PROVISIONED profile ⇒ the authoritative value is "" and it
        # STILL overwrites any caller-supplied value ⇒ the order refuses rather than run under a spoofed id.
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587")  # no provisioning
        job = ExecutionJob(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                           payload={"symbol": "X", "side": "SELL", "lots": "0.40", "comment": "c",
                                    "windows_username": "guvfx_u_1"})
        inject_identity_pin(job)
        self.assertEqual(job.payload["windows_username"], "")

    # ---- integration through ExecutionJob.save() + worker-acceptance ---------------------------------
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_pin_injected_via_save_carries_windows_username(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", server_name="IS6Technologies-Demo",
                        provisioned_username="guvfx_u_25")
        # CLOSE_TRADE takes the identical central injection but is not execution-gate-blocked at save().
        job = ExecutionJob(job_type=ExecutionJob.JobType.CLOSE_TRADE, account=acct, payload={"ticket": 1})
        job.save(); job.refresh_from_db()
        self.assertEqual(job.payload["windows_username"], "guvfx_u_25")
        self.assertTrue(job.payload["require_identity_pin"])
        self.assertEqual(job.payload["expected_login"], "1302587")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provisioned_payload_passes_worker_required_fields(self):
        # The exact predicate the worker enforces (mt5_trade_ingest_worker.py:825):
        #   all([windows_username, symbol, side, lots, comment])
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587", provisioned_username="guvfx_u_25")
        job = ExecutionJob(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                           payload={"symbol": "XAUUSD", "side": "SELL", "lots": "0.40", "comment": "WAY1L1",
                                    "windows_username": None})
        inject_identity_pin(job)
        p = job.payload
        self.assertTrue(all([p.get("windows_username"), p.get("symbol"), p.get("side"), p.get("lots"),
                             p.get("comment")]))  # would have been False (missing_payload_fields) before the fix

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_unprovisioned_payload_fails_worker_required_fields(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="1302587")  # no provisioning ⇒ "" username
        job = ExecutionJob(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                           payload={"symbol": "XAUUSD", "side": "SELL", "lots": "0.40", "comment": "c",
                                    "windows_username": None})
        inject_identity_pin(job)
        p = job.payload
        self.assertFalse(all([p.get("windows_username"), p.get("symbol"), p.get("side"), p.get("lots"),
                              p.get("comment")]))  # fails closed at the worker
