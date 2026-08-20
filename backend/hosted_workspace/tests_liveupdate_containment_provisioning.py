"""P0 pre-beta reliability gate — PROACTIVE MT5 LiveUpdate containment during provisioning.

The proven first-launch LiveUpdate terminal-fork (a login-less /portable terminal + a roaming/non-portable
sibling that carries the broker login → account_info hangs → onboarding stalls at "Detecting your account...")
previously required per-customer operator repair. This suite proves the fix is:

  * WIRED into ``prepare_hosted_slot`` as a REQUIRED, fail-closed host step AFTER the runtime is materialised and
    BEFORE the customer's first launch (RemoteApp verify), behind ``HOSTED_LIVEUPDATE_CONTAINMENT_ENABLED``;
  * BYTE-IDENTICAL to before while the flag is OFF (Customer Zero / support@ / every existing slot untouched);
  * FAIL-CLOSED — an unverifiable containment leaves the slot NON-READY (preparing/retry UX), never advances;
  * TENANT-SCOPED at the executor + dispatch layers (server-derived identity/paths, Customer Zero refused);
  * reusing the CERTIFIED Variant-A containment body byte-identically (divergence-guarded against
    Relaunch-GuvfxTerminal.ps1's 35-test-certified logic), so the reparse/SID/DACL guarantees carry over.

Everything here runs against the in-memory host-executor fake or static-analyses the reviewed .ps1 — zero host
contact, no secret, no order, no broker login.
"""
import os
import re

from django.test import SimpleTestCase, TestCase, override_settings

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace import slot_preparation as SP
from hosted_workspace.tests_host_executor import KR, T, _executor
from hosted_workspace.tests_slot_preparation import (
    FakeExecutor, _PREP_ON, _bound_ws)

# Disable the CZ reserved-id guard by default (like tests_slot_preparation) so a test account that happens to
# get pk=1 is not mistaken for Customer Zero; the CZ refusal is exercised explicitly where it matters.
_CONTAIN_ON = dict(_PREP_ON, HOSTED_LIVEUPDATE_CONTAINMENT_ENABLED="1", HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")

_WIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "terminal_provisioning", "windows")
_CONTAIN_PS1 = os.path.join(_WIN_DIR, "Contain-GuvfxLiveUpdate.ps1")
_RELAUNCH_PS1 = os.path.join(_WIN_DIR, "Relaunch-GuvfxTerminal.ps1")


class ContainFakeExecutor(FakeExecutor):
    """Adds the proactive-containment host step to the in-memory fake (ok unless named in ``fail``/``raise_at``,
    absent if named in ``drop`` → executor-incomplete)."""

    def apply_liveupdate_containment(self, username, runtime_root, rdp_host=None):
        # Record the identity the step was asked to act on so cross-tenant isolation is assertable.
        self.calls.append("apply_liveupdate_containment")
        self.contain_identity = (username, runtime_root)
        if self.raise_at == "apply_liveupdate_containment":
            raise RuntimeError("host boom")
        return {"ok": "apply_liveupdate_containment" not in self.fail,
                "reason": "apply_liveupdate_containment",
                "contained": True, "profile_created": True}


# ─────────────────────────────────────── provisioning integration (Phase 10) ────────────────────────────────
@override_settings(**_CONTAIN_ON)
class ContainmentProvisioningTests(TestCase):
    def _prep(self, ex=None, **ws_kw):
        ws, acct, node = _bound_ws(**ws_kw)
        ex = ex if ex is not None else ContainFakeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        ws.refresh_from_db()
        return res, ws, acct, ex

    def test_containment_runs_after_runtime_and_before_first_launch(self):
        # (1) fresh tenant → containment applied; ordering proves it is AFTER populate_runtime (runtime exists)
        # and BEFORE verify_remoteapp (the customer's first launch happens only after WAITING_FOR_LOGIN).
        res, ws, _, ex = self._prep()
        self.assertTrue(res.prepared, res.reason)
        self.assertIn("apply_liveupdate_containment", ex.calls)
        i_contain = ex.calls.index("apply_liveupdate_containment")
        self.assertGreater(i_contain, ex.calls.index("populate_runtime"))
        self.assertLess(i_contain, ex.calls.index("apply_autotrading_config"))
        self.assertLess(i_contain, ex.calls.index("verify_remoteapp"))
        self.assertEqual(res.reason, SP.PREP_OK)   # caller advances PROVISIONING→WAITING_FOR_LOGIN on prepared

    @override_settings(HOSTED_LIVEUPDATE_CONTAINMENT_ENABLED="0")   # containment flag OFF (other flags stay on)
    def test_flag_off_is_byte_identical_no_containment(self):
        # (12/13) With the flag off the step is skipped entirely — support@/CZ/existing slots unchanged.
        res, ws, _, ex = self._prep()
        self.assertTrue(res.prepared, res.reason)
        self.assertNotIn("apply_liveupdate_containment", ex.calls)

    def test_containment_failure_leaves_slot_not_ready(self):
        # (9) containment unverifiable → NON-READY, never advances, distinct fail-closed reason.
        res, ws, _, ex = self._prep(ex=ContainFakeExecutor(fail=("apply_liveupdate_containment",)))
        self.assertFalse(res.prepared)   # not prepared ⇒ the caller never advances ⇒ slot stays NON-READY
        self.assertEqual(res.reason, SP.PREP_CONTAINMENT_FAILED)
        self.assertEqual(res.stage_reached, SP.ST_CONTAINMENT)
        # A downstream launch step must NOT have run once containment failed closed.
        self.assertNotIn("verify_remoteapp", ex.calls)

    def test_containment_host_error_fails_closed(self):
        res, ws, _, _ = self._prep(ex=ContainFakeExecutor(raise_at="apply_liveupdate_containment"))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_CONTAINMENT_FAILED)

    def test_containment_missing_on_older_host_is_executor_incomplete(self):
        # (required step) an older host without the primitive → EXECUTOR_INCOMPLETE, never a silent skip.
        res, ws, _, _ = self._prep(ex=ContainFakeExecutor(drop=("apply_liveupdate_containment",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)
        self.assertEqual(res.stage_reached, SP.ST_CONTAINMENT)

    def test_retry_is_idempotent_and_reconverges(self):
        # (4) a fresh prepare re-drives every step (incl. containment) and re-converges to prepared.
        ws, acct, node = _bound_ws()
        r1 = SP.prepare_hosted_slot(ws, executor=ContainFakeExecutor())
        r2 = SP.prepare_hosted_slot(ws, executor=ContainFakeExecutor())
        self.assertTrue(r1.prepared and r2.prepared)

    def test_customer_zero_never_reaches_containment(self):
        # (13) CZ is refused at Stage-0 guard before ANY host step — containment is never attempted for it.
        ws, acct, node = _bound_ws(login="111111")
        with override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS=str(acct.pk)):
            ex = ContainFakeExecutor()
            res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_REFUSED_RESERVED)
        self.assertNotIn("apply_liveupdate_containment", ex.calls)

    def test_five_independent_tenants_each_contained_with_own_identity(self):
        # (5, Phase 8) five independent provisioning flows: each containment acts on its OWN username+runtime,
        # never another tenant's — no shared identity, no cross-write, all reach WAITING_FOR_LOGIN.
        seen = []
        for i in range(5):
            ex = ContainFakeExecutor()
            ws, acct, _ = _bound_ws(login=f"9000{i}", uname=f"t{i}")
            res = SP.prepare_hosted_slot(ws, executor=ex)
            self.assertTrue(res.prepared, f"tenant {i}: {res.reason}")
            uname, runtime = ex.contain_identity
            self.assertEqual(uname, f"guvfx_u_{acct.pk}")
            self.assertIn(str(acct.pk), runtime)
            seen.append((uname, runtime))
        self.assertEqual(len(set(seen)), 5)   # every tenant's containment identity is distinct

    def test_containment_does_not_fabricate_observation_or_readiness(self):
        # (14) containment is not an observation: it sets no observed_* / match fields; the server-authoritative
        # observer sequence still owns readiness. prepared here means "slot prepared", not "account detected".
        res, ws, _, _ = self._prep()
        self.assertTrue(res.prepared, f"{res.reason}/{res.stage_reached}")
        self.assertIsNone(ws.observed_connected)
        self.assertIsNone(ws.active_account_match)


# ─────────────────────────────────────── executor + dispatch confinement (Phase 8/11) ───────────────────────
class ContainmentExecutorTests(SimpleTestCase):
    def test_executor_confines_to_own_identity_and_refuses_cz(self):
        ex = _executor(account_id=24, result_by_op={"APPLY_LIVEUPDATE_CONTAINMENT":
                                                    {"ok": True, "contained": True, "profile_created": True}})
        ok = ex.apply_liveupdate_containment(username="guvfx_u_24", runtime_root=r"C:\GuvFX\accounts\24")
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["contained"])
        # foreign identity / foreign runtime are refused BEFORE any transport call
        self.assertFalse(ex.apply_liveupdate_containment(
            username="guvfx_u_1", runtime_root=r"C:\GuvFX\accounts\24")["ok"])
        self.assertFalse(ex.apply_liveupdate_containment(
            username="guvfx_u_24", runtime_root=r"C:\evil")["ok"])
        # a Customer-Zero executor (reserved default) fails closed without contacting the host
        from hosted_workspace.host_executor import SignedHostExecutor
        cz = SignedHostExecutor(account_id=1, rdp_host="x", transport=lambda *_a, **_k: {"ok": True},
                                keyring=KR, key_id="k1", base_url="x",
                                seal_password=lambda *a, **k: {}, reserved_ids=None, clock=lambda: T)
        self.assertFalse(cz.apply_liveupdate_containment(
            username="guvfx_u_1", runtime_root=r"C:\GuvFX\accounts\1")["ok"])


class ContainmentDispatchTests(SimpleTestCase):
    def _run(self, op, reserved_ids="", **kw):
        calls = []

        def run_primitive(name, args):
            calls.append((name, args))
            return {"ok": True, "primitive": name}

        def _burn(n, e):
            return True

        def envelope_open(payload, *, account_id, correlation_id, nonce):
            return "OPENED_PW"

        req = P.sign_hosted_request(account_id=kw.get("account_id", 14), operation=op, correlation_id="c1",
                                    keyring=KR, key_id="k1", now=T, params=kw.get("params"), nonce=None,
                                    payload=None)
        resp = D.dispatch(req, keyring=KR, now=T, nonce_burn=_burn, run_primitive=run_primitive,
                          reserved_ids=reserved_ids, envelope_open=envelope_open)
        return calls, resp

    def test_maps_to_primitive_with_server_derived_args(self):
        calls, resp = self._run("APPLY_LIVEUPDATE_CONTAINMENT", account_id=24)
        self.assertEqual(calls[0][0], "apply_liveupdate_containment")
        self.assertEqual(calls[0][1], {"username": "guvfx_u_24",
                                       "terminal_root": r"C:\GuvFX\accounts\24\terminal", "account_id": 24})
        self.assertTrue(P.verify_hosted_response(resp, correlation_id="c1", nonce=resp["nonce"],
                                                 keyring=KR)["ok"])

    def test_refuses_customer_zero_host_side(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run("APPLY_LIVEUPDATE_CONTAINMENT", account_id=1, reserved_ids=None)
        self.assertEqual(cm.exception.reason_code, "reserved_identity")

    def test_rejects_smuggled_params(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run("APPLY_LIVEUPDATE_CONTAINMENT", account_id=24, params={"terminal_root": r"C:\evil"})
        self.assertEqual(cm.exception.reason_code, "params_not_allowed")

    def test_operation_registered_across_layers(self):
        self.assertIn("APPLY_LIVEUPDATE_CONTAINMENT", P.HOSTED_OPERATIONS)
        self.assertEqual(D.OP_PRIMITIVES["APPLY_LIVEUPDATE_CONTAINMENT"]["primitive"],
                         "apply_liveupdate_containment")
        self.assertEqual(D.OP_PRIMITIVES["APPLY_LIVEUPDATE_CONTAINMENT"]["params_allow"], ())


# ─────────────────────────────────── certified-body divergence guard (Phase 3) ──────────────────────────────
def _extract_function(path, name):
    """Return the top-level PowerShell function block (``function <name> ... `` up to the first line that is
    exactly ``}``) — used to assert the certified containment body is byte-identical across scripts."""
    with open(path, "r", encoding="ascii") as fh:
        lines = fh.read().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("function " + name)), None)
    assert start is not None, f"{name} not found in {path}"
    body = []
    for ln in lines[start:]:
        body.append(ln)
        if ln == "}":
            return "\n".join(body)
    raise AssertionError(f"{name} block not closed in {path}")


class ContainmentScriptStaticTests(SimpleTestCase):
    def test_certified_containment_body_is_byte_identical_to_relaunch(self):
        # Phase 3: the containment reuses the certified Variant-A body (Relaunch-GuvfxTerminal.ps1 — 35 tests).
        # A verbatim copy is guarded HERE so the two can never silently diverge.
        for fn in ("Test-ChainReparseFree", "Apply-LiveUpdateContainment"):
            self.assertEqual(_extract_function(_CONTAIN_PS1, fn), _extract_function(_RELAUNCH_PS1, fn),
                             f"{fn} diverged from the certified Relaunch body")

    def test_contain_script_is_ascii_only(self):
        # RULE 9: installation/host artefacts are ASCII-only so they parse identically under any encoding.
        with open(_CONTAIN_PS1, "rb") as fh:
            raw = fh.read()
        self.assertTrue(all(b < 128 for b in raw), "Contain-GuvfxLiveUpdate.ps1 contains non-ASCII bytes")

    def test_contain_script_never_launches_or_logs_in(self):
        # Defence: the proactive primitive must NOT close/relaunch a terminal or launch MT5. We assert on the
        # launch/close CONSTRUCTS (code-only), not prose — the header legitimately describes the "/portable" fork.
        with open(_CONTAIN_PS1, "r", encoding="ascii") as fh:
            text = fh.read()
        for forbidden in ("Register-ScheduledTask", "Start-ScheduledTask", "New-ScheduledTaskAction", "taskkill"):
            self.assertNotIn(forbidden, text,
                             f"proactive containment must not use '{forbidden}' (no launch/close of any terminal)")

    def test_reserved_ids_refuse_customer_zero(self):
        with open(_CONTAIN_PS1, "r", encoding="ascii") as fh:
            text = fh.read()
        self.assertIn("$RESERVED_ACCOUNT_IDS = @(1", text)
        self.assertIn("refusing_reserved_identity", text)
