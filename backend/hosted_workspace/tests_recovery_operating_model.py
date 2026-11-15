"""RECOVERY OPERATING MODEL (packet-127) — tests for the governed exclusive-window lease, the Django-free deploy
gate, the bounded acceptance runner's guaranteed teardown, and the recovery-config hardening.

Covers the packet-127 checklist:
  deploy refused during an active recovery lease; recovery refused during an active deploy lease; expired lease
  safely reclaimed; malformed lease fails closed; emergency override audited; allow-list missing/empty/malformed
  fails closed; Copy-Trading participant excluded; onboarding account excluded; execution ``is_active`` unchanged;
  a 299s threshold rejected and 300s accepted; only one account relaunched; sibling unchanged; timeout / exception /
  success all restore the byte-exact baseline; and no secret/environment leakage.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone as djtz

from hosted_workspace import liveness_recovery as LR
from hosted_workspace import recovery_acceptance as RA
from hosted_workspace import recovery_lease as RL

# Reuse the established-recovery fixture toolkit for the DB-backed invariants.
from hosted_workspace.tests_established_recovery import (  # noqa: E402
    EST_FLAGS, EST_LIVE_FLAGS, FakeExecutor, _down, _down_candidate, _ids, _mk_copier_master, _resolver,
)
from hosted_workspace.host_agent_dispatch import derive_slot  # noqa: E402

import importlib.util  # noqa: E402

_GATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "scripts", "deploy_recovery_gate.py")


def _load_gate():
    spec = importlib.util.spec_from_file_location("deploy_recovery_gate", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1) Governed lease — mutual exclusion, TTL/expiry, reclaim, malformed fail-closed, secret rejection, audit.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class RecoveryLeaseTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")

    def _acquire(self, kind, corr="c1", ttl=600, now=T0):
        return RL.acquire(kind, "bounded pilot", "op", corr, ttl, now, path=self.p)

    def test_deploy_refused_during_active_recovery_lease(self):
        ok, _ = self._acquire(RL.KIND_RECOVERY)
        self.assertTrue(ok)
        blocked, reason = RL.blocks(RL.KIND_DEPLOY, T0 + timedelta(seconds=10), path=self.p)
        self.assertTrue(blocked)
        self.assertIn("held:recovery_acceptance", reason)

    def test_recovery_refused_during_active_deploy_lease(self):
        ok, _ = self._acquire(RL.KIND_DEPLOY)
        self.assertTrue(ok)
        blocked, reason = RL.blocks(RL.KIND_RECOVERY, T0 + timedelta(seconds=10), path=self.p)
        self.assertTrue(blocked)
        # a second acquire of the other kind is also refused (mutual exclusion / single lease)
        ok2, reason2 = self._acquire(RL.KIND_RECOVERY, corr="c2")
        self.assertFalse(ok2)
        self.assertTrue(str(reason2).startswith("held:deploy"))

    def test_expired_lease_is_not_blocking_and_is_reclaimable(self):
        self._acquire(RL.KIND_RECOVERY, ttl=60)
        later = T0 + timedelta(seconds=120)           # past expiry
        status, _ = RL.read_lease(later, path=self.p)
        self.assertEqual(status, RL.LeaseStatus.EXPIRED)
        blocked, _ = RL.blocks(RL.KIND_DEPLOY, later, path=self.p)
        self.assertFalse(blocked)                     # expired ⇒ not blocking
        reclaimed, reason = RL.reclaim(later, path=self.p)
        self.assertTrue(reclaimed)
        self.assertEqual(RL.read_lease(later, path=self.p)[0], RL.LeaseStatus.ABSENT)

    def test_acquire_over_expired_reclaims_and_succeeds(self):
        self._acquire(RL.KIND_RECOVERY, ttl=60, corr="old")
        later = T0 + timedelta(seconds=120)
        ok, lease = RL.acquire(RL.KIND_DEPLOY, "deploy", "op", "new", 600, later, path=self.p)
        self.assertTrue(ok)
        self.assertEqual(lease.kind, RL.KIND_DEPLOY)

    def test_malformed_lease_fails_closed(self):
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.assertEqual(RL.read_lease(T0, path=self.p)[0], RL.LeaseStatus.MALFORMED)
        blocked, reason = RL.blocks(RL.KIND_DEPLOY, T0, path=self.p)
        self.assertTrue(blocked)
        self.assertEqual(reason, "malformed_fail_closed")
        ok, why = self._acquire(RL.KIND_RECOVERY)        # cannot acquire over a malformed lease
        self.assertFalse(ok)
        self.assertEqual(why, "malformed_existing_lease_fail_closed")

    def test_empty_file_is_malformed_not_absent(self):
        open(self.p, "w").close()
        self.assertEqual(RL.read_lease(T0, path=self.p)[0], RL.LeaseStatus.MALFORMED)

    def test_release_only_by_matching_correlation(self):
        self._acquire(RL.KIND_RECOVERY, corr="mine")
        bad, reason = RL.release("someone-else", T0, path=self.p)
        self.assertFalse(bad)
        self.assertEqual(reason, "correlation_mismatch")
        ok, _ = RL.release("mine", T0, path=self.p)
        self.assertTrue(ok)
        self.assertEqual(RL.read_lease(T0, path=self.p)[0], RL.LeaseStatus.ABSENT)
        # releasing an absent lease is idempotent success
        self.assertEqual(RL.release("mine", T0, path=self.p), (True, "already_absent"))

    def test_secret_bearing_field_rejected(self):
        ok, why = RL.acquire(RL.KIND_RECOVERY, "has SECRET=abc123", "op", "c1", 600, T0, path=self.p)
        self.assertFalse(ok)
        self.assertEqual(why, "secret_bearing_field_rejected")
        ok2, why2 = RL.acquire(RL.KIND_RECOVERY, "ok", "A" * 50, "c1", 600, T0, path=self.p)  # long opaque owner
        self.assertFalse(ok2)

    def test_ttl_clamped_to_bounds(self):
        ok, lease = RL.acquire(RL.KIND_RECOVERY, "p", "o", "c1", 999999, T0, path=self.p)
        self.assertTrue(ok)
        self.assertLessEqual(lease.remaining_s(T0), RL.TTL_MAX_S)

    def test_acquire_writes_audit_line(self):
        self._acquire(RL.KIND_RECOVERY)
        ap = RL.audit_path(self.p)
        self.assertTrue(os.path.exists(ap))
        rec = json.loads(open(ap).read().strip().splitlines()[-1])
        self.assertEqual(rec["event"], "acquire")
        self.assertEqual(rec["kind"], RL.KIND_RECOVERY)

    def test_acquire_lost_race_is_fail_closed(self):
        # The O_EXCL create is the compare-and-swap: if another acquirer wins the file between our ABSENT check and
        # our write, we lose the race fail-closed rather than clobbering the winner's lease.
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("placeholder-written-by-the-race-winner")
        with mock.patch.object(RL, "read_lease", return_value=(RL.LeaseStatus.ABSENT, None)):
            ok, why = self._acquire(RL.KIND_RECOVERY)
        self.assertFalse(ok)
        self.assertEqual(why, "lost_acquire_race")

    def test_non_utf8_lease_is_malformed_not_absent(self):
        with open(self.p, "wb") as fh:
            fh.write("recovery".encode("utf-16"))        # BOM-less-trap-class encoding (RULE 9/11)
        self.assertEqual(RL.read_lease(T0, path=self.p)[0], RL.LeaseStatus.MALFORMED)
        self.assertTrue(RL.blocks(RL.KIND_DEPLOY, T0, path=self.p)[0])


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2) Django-free deploy gate — classifies identically to the lease module; override audited; fail-closed.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class DeployGateTests(SimpleTestCase):
    def setUp(self):
        self.gate = _load_gate()
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")

    def test_gate_agrees_with_lease_module(self):
        # ABSENT
        self.assertEqual(self.gate.deploy_blocked(self.p, T0)[0],
                         RL.blocks(RL.KIND_DEPLOY, T0, path=self.p)[0])
        # ACTIVE recovery
        RL.acquire(RL.KIND_RECOVERY, "p", "o", "c1", 600, T0, path=self.p)
        self.assertTrue(self.gate.deploy_blocked(self.p, T0 + timedelta(seconds=5))[0])
        self.assertEqual(self.gate.deploy_blocked(self.p, T0 + timedelta(seconds=5))[0],
                         RL.blocks(RL.KIND_DEPLOY, T0 + timedelta(seconds=5), path=self.p)[0])
        # EXPIRED → not blocked
        self.assertFalse(self.gate.deploy_blocked(self.p, T0 + timedelta(seconds=700))[0])
        # MALFORMED → blocked (fail-closed)
        with open(self.p, "w") as fh:
            fh.write("garbage")
        self.assertTrue(self.gate.deploy_blocked(self.p, T0)[0])

    def test_gate_main_exit_codes_and_override_audit(self):
        # main() checks against real wall-clock now, so acquire with a current timestamp + long TTL (ACTIVE now).
        RL.acquire(RL.KIND_RECOVERY, "p", "o", "c1", 3600, datetime.now(timezone.utc), path=self.p)
        # blocked ⇒ exit 2
        self.assertEqual(self.gate.main(["--lease-file", self.p]), 2)
        # emergency override ⇒ exit 0 + audit line
        self.assertEqual(self.gate.main(["--lease-file", self.p, "--emergency-override", "hotfix approved"]), 0)
        ap = os.path.join(self.d, "recovery_operational_lease_audit.jsonl")
        self.assertTrue(os.path.exists(ap))
        self.assertIn("emergency_override", open(ap).read())

    def test_gate_no_lease_file_fails_closed(self):
        self.assertEqual(self.gate.main(["--lease-file", ""]), 2)

    def test_gate_secret_bearing_expired_blocks_matching_lease_module(self):
        # Parity on the dangerous case: a schema-valid but secret-bearing AND expired lease must be MALFORMED-blocked
        # by BOTH (not read as EXPIRED-permit by the gate). acquire() refuses to write such a lease, so craft it raw.
        past = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        doc = {"kind": "deploy", "purpose": "ok", "owner": "x", "correlation_id": "TOKEN=sk_" + "a" * 42,
               "created_at": past, "expires_at": past}
        with open(self.p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(RL.blocks(RL.KIND_DEPLOY, now, path=self.p)[0])        # lease module blocks (MALFORMED)
        self.assertTrue(self.gate.deploy_blocked(self.p, now)[0])             # gate must ALSO block (parity)

    def test_gate_non_utf8_is_malformed_blocked(self):
        with open(self.p, "wb") as fh:
            fh.write("recovery".encode("utf-16"))
        self.assertTrue(self.gate.deploy_blocked(self.p, datetime(2026, 1, 1, tzinfo=timezone.utc))[0])

    def test_gate_override_reason_redacted_in_audit(self):
        RL.acquire(RL.KIND_RECOVERY, "p", "o", "c1", 3600, datetime.now(timezone.utc), path=self.p)
        secret = "sk_" + "a" * 42
        self.gate.main(["--lease-file", self.p, "--emergency-override", "TOKEN=" + secret])
        blob = open(os.path.join(self.d, "recovery_operational_lease_audit.jsonl")).read()
        self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", blob)


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3) Bounded acceptance runner — guaranteed teardown on EVERY exit path.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class _FakeHostOps(RA.HostOps):
    def __init__(self, *, observe=RA.OUTCOME_PASS, flags_off=True, raise_observe=False,
                 raise_recreate_on=0, raise_recreate_always=False):
        self.env = "BASE=1\nKEEP=2\n"
        self._now = T0
        self._observe = observe
        self._flags_off = flags_off
        self._raise = raise_observe
        self._raise_recreate_on = raise_recreate_on         # raise on the Nth recreate call (1-based); 0 = never
        self._raise_recreate_always = raise_recreate_always
        self.writes = []
        self.recreates = 0
        self.alerts = []

    def now(self):
        self._now = self._now + timedelta(seconds=1)
        return self._now

    def read_env(self):
        return self.env

    def write_env(self, content):
        self.env = content
        self.writes.append(content)

    def recreate_backend(self):
        self.recreates += 1
        if self._raise_recreate_always or (self._raise_recreate_on and self.recreates == self._raise_recreate_on):
            raise RuntimeError("recreate boom")

    def flags_off(self):
        return self._flags_off

    def observe(self, account_id, deadline):
        if self._raise:
            raise RuntimeError("observe boom")
        return self._observe

    def alert(self, title, detail):
        self.alerts.append((title, detail))


class AcceptanceRunnerTeardownTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")
        self.baseline = "BASE=1\nKEEP=2\n"

    def _run(self, ops, **kw):
        return RA.run_bounded_acceptance(ops, 900050, ttl_s=600, purpose="pilot", owner="op",
                                         correlation_id="c1", lease_path=self.p, **kw)

    def _assert_clean_teardown(self, ops, res):
        self.assertEqual(ops.env, self.baseline)                       # byte-exact baseline restored
        self.assertTrue(res.teardown_ok)
        self.assertIs(res.flags_off, True)
        self.assertEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)   # lease released

    def test_success_restores_byte_exact_baseline(self):
        ops = _FakeHostOps(observe=RA.OUTCOME_PASS)
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_PASS)
        self.assertTrue(res.armed)
        self.assertNotIn("HOSTED_ESTABLISHED_RECOVERY_ENABLED", ops.env)
        self._assert_clean_teardown(ops, res)

    def test_fail_restores_byte_exact_baseline(self):
        ops = _FakeHostOps(observe=RA.OUTCOME_FAIL)
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_FAIL)
        self._assert_clean_teardown(ops, res)

    def test_timeout_restores_byte_exact_baseline(self):
        ops = _FakeHostOps(observe=RA.OUTCOME_TIMEOUT)
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_TIMEOUT)
        self._assert_clean_teardown(ops, res)

    def test_exception_restores_byte_exact_baseline(self):
        ops = _FakeHostOps(raise_observe=True)
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_ERROR)
        self.assertEqual(res.detail.get("exception"), "RuntimeError")
        self._assert_clean_teardown(ops, res)

    def test_recreate_failure_during_arm_still_restores_baseline(self):
        # CRITICAL regression: beta.env is written to flags-ON before the arm recreate. If that recreate raises, the
        # teardown MUST still restore the byte-exact baseline (gated on env_dirty, not on the arm completing).
        ops = _FakeHostOps(raise_recreate_on=1)      # arm recreate (1st) fails; teardown recreate (2nd) succeeds
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_ERROR)
        self.assertFalse(res.armed)                  # arm did not complete...
        self.assertEqual(ops.env, self.baseline)     # ...yet beta.env was restored byte-exact
        self.assertTrue(res.teardown_ok)
        self.assertEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)

    def test_restore_recreate_also_fails_alerts_and_keeps_lease(self):
        # If even the teardown recreate fails, the env is still rewritten to baseline on disk, an operator ALERT
        # fires, teardown_ok is False, and the lease is NOT released (it self-expires via TTL — fail-closed).
        ops = _FakeHostOps(raise_recreate_always=True)
        res = self._run(ops)
        self.assertFalse(res.teardown_ok)
        self.assertEqual(ops.env, self.baseline)     # restore WRITE happened even though recreate could not
        self.assertTrue(any("teardown FAILED" in t for t, _ in ops.alerts))
        self.assertNotEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)  # lease retained

    def test_blocked_by_deploy_lease_never_arms(self):
        RL.acquire(RL.KIND_DEPLOY, "deploy", "op", "dep", 600, T0, path=self.p)
        ops = _FakeHostOps()
        res = self._run(ops)
        self.assertEqual(res.outcome, RA.OUTCOME_BLOCKED)
        self.assertFalse(res.armed)
        self.assertEqual(ops.writes, [])                 # nothing written → no arm
        self.assertEqual(ops.recreates, 0)
        self.assertEqual(RL.read_lease(T0, path=self.p)[1].kind, RL.KIND_DEPLOY)  # deploy lease untouched

    def test_teardown_flags_not_off_alerts_and_marks_incomplete(self):
        ops = _FakeHostOps(observe=RA.OUTCOME_PASS, flags_off=False)
        res = self._run(ops)
        self.assertFalse(res.teardown_ok)
        self.assertTrue(any("teardown INCOMPLETE" in t for t, _ in ops.alerts))

    def test_build_armed_env_shape(self):
        armed = RA.build_armed_env(self.baseline, 900050)
        self.assertIn("HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=900050", armed)
        self.assertIn("HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN=1", armed)
        self.assertIn("HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES=1", armed)
        self.assertTrue(armed.startswith(self.baseline))
        # faster threshold below the floor is defended to 900
        self.assertIn("HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S=900",
                      RA.build_armed_env(self.baseline, 900050, stale_s=120))
        self.assertIn("HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S=300",
                      RA.build_armed_env(self.baseline, 900050, stale_s=300))


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4) Recovery-config hardening — 300s floor, allow-list fail-closed, LIVE-requires-allow-list, approved steady-state,
#    enabled-without-lease warning, safe metadata (no env dump).
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class RecoveryConfigHardeningTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["GUVFX_RECOVERY_LEASE_DIR"] = self.d        # isolate lease reads from any real file
        self.addCleanup(lambda: os.environ.pop("GUVFX_RECOVERY_LEASE_DIR", None))

    def test_299_rejected_300_accepted(self):
        with override_settings(HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S="299"):
            self.assertEqual(LR._established_recovery_stale_s(), 900)   # below floor ⇒ fail-closed to 900
            self.assertTrue(LR._stale_malformed())
        with override_settings(HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S="300"):
            self.assertEqual(LR._established_recovery_stale_s(), 300)   # at floor ⇒ accepted
            self.assertFalse(LR._stale_malformed())

    def test_allowlist_missing_empty_malformed_fail_closed(self):
        for val in (None, "", "   ", "50,abc", "50 -1", "0"):
            ctx = override_settings() if val is None else override_settings(HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=val)
            with ctx:
                self.assertEqual(LR._established_recovery_allowlist(), frozenset())

    @override_settings(**EST_LIVE_FLAGS)
    def test_live_recovery_without_allowlist_warns(self):
        warnings = LR.recovery_enablement_warnings()
        self.assertTrue(any("LIVE recovery enabled without an allow-list" in w for w in warnings))
        self.assertTrue(any("EMPTY/malformed allow-list" in w for w in warnings))

    @override_settings(**EST_FLAGS, HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS="900001",
                       HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN="1", HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES="1")
    def test_approved_steady_state_config_true(self):
        self.assertTrue(LR._is_approved_steady_state_config())
        # enabled WITH an approved shape ⇒ no "leftover" warning
        self.assertFalse(any("LEFTOVER" in w for w in LR.recovery_enablement_warnings()))

    @override_settings(**EST_FLAGS, HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS="900001,900002",
                       HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN="1", HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES="1")
    def test_cohort_too_large_not_approved(self):
        self.assertFalse(LR._is_approved_steady_state_config())

    @override_settings(**EST_FLAGS, HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS="900001",
                       HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN="2", HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES="1")
    def test_breaker_not_pinned_not_approved(self):
        self.assertFalse(LR._is_approved_steady_state_config())

    @override_settings(**EST_FLAGS)   # established ON, allow-list empty, no lease
    def test_enabled_without_lease_or_approved_config_warns_leftover(self):
        warnings = LR.recovery_enablement_warnings()
        self.assertTrue(any("LEFTOVER" in w for w in warnings))

    @override_settings(**EST_LIVE_FLAGS, HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS="900001",
                       HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN="1", HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES="1")
    def test_live_config_never_approved_unleased_and_warns(self):
        # Backstop-blindness fix: a LIVE allow-list-of-one / breaker-1/1 config (identical to a bounded acceptance
        # arm) is NEVER an unleased 'approved steady-state' and ALWAYS warns without an active lease — so a LIVE
        # acceptance whose teardown failed is never silently treated as an approved cohort.
        self.assertFalse(LR._is_approved_steady_state_config())
        warnings = LR.recovery_enablement_warnings()
        self.assertTrue(any("LIVE recovery ENABLED without an ACTIVE governed lease" in w for w in warnings))

    @override_settings(**EST_LIVE_FLAGS, HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS="900001",
                       HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN="1", HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES="1")
    def test_config_metadata_is_safe_no_env_dump(self):
        meta = LR.recovery_config_metadata()
        self.assertEqual(meta["allowlist_ids"], [900001])
        self.assertTrue(meta["established_recovery_enabled"])
        self.assertIn("governed_lease", meta)
        blob = json.dumps(meta)
        # metadata must not leak raw environment values / secret-looking content
        for bad in ("SECRET", "TOKEN=", "PASSWORD", "KEYRING", "DJANGO_SECRET_KEY"):
            self.assertNotIn(bad, blob)


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 5) Preserved invariants under the operating model (DB) — only-one-relaunch, sibling unchanged, copier/onboarding
#    excluded, is_active never written. (Deep coverage lives in Packet118HardeningTests; these guard regressions.)
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class OperatingModelInvariantsTests(TestCase):
    @override_settings(**EST_FLAGS)
    def test_only_one_account_relaunched_siblings_unchanged(self):
        target, _, tws = _down_candidate()
        sibling, _, sws = _down_candidate()
        ex = FakeExecutor()
        with override_settings(HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=_ids(target)):
            out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 1)
        self.assertEqual(out["established"]["skipped_allowlist"], 1)
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.calls[0][1], derive_slot(target.id)["username"])
        sws.refresh_from_db()
        self.assertEqual(sws.liveness_recovery_count, 0)              # sibling untouched

    @override_settings(**EST_LIVE_FLAGS)
    def test_copier_participant_excluded_even_if_listed(self):
        acct, _, _ = _down_candidate(is_demo=False)
        _mk_copier_master(acct)
        ex = FakeExecutor()
        with override_settings(HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=_ids(acct)):
            out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_is_active_never_mutated_by_recovery(self):
        target, _, tws = _down_candidate()                           # _down_candidate builds is_active=False
        self.assertFalse(target.is_active)
        ex = FakeExecutor()
        with override_settings(HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=_ids(target)):
            LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        target.refresh_from_db()
        self.assertFalse(target.is_active)                           # execution gate unchanged


# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 6) No secret / environment leakage.
# ════════════════════════════════════════════════════════════════════════════════════════════════════════════════
class NoSecretLeakageTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")

    def test_audit_redacts_secret_bearing_content(self):
        # a correlation that sneaks a secret pattern is redacted in the audit, never written verbatim
        RL._audit("acquire", T0, self.p, kind=RL.KIND_RECOVERY, correlation_id="c1",
                  purpose="TOKEN=sk_live_abcdefghij", owner="op")
        ap = RL.audit_path(self.p)
        blob = open(ap).read()
        self.assertNotIn("sk_live_abcdefghij", blob)
        self.assertIn("<redacted>", blob)

    def test_status_metadata_has_no_env(self):
        RL.acquire(RL.KIND_RECOVERY, "pilot", "op", "c1", 600, T0, path=self.p)
        meta = RL.status_metadata(T0, path=self.p)
        blob = json.dumps(meta)
        for bad in ("SECRET", "PASSWORD", "KEYRING", "TOKEN="):
            self.assertNotIn(bad, blob)

    def test_audit_redacts_secret_in_correlation_id(self):
        secret = "sk_" + "a" * 42
        RL._audit("acquire", T0, self.p, kind=RL.KIND_RECOVERY, correlation_id="TOKEN=" + secret,
                  purpose="ok", owner="op")
        blob = open(RL.audit_path(self.p)).read()
        self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", blob)
