"""CVM-Inc-3 B — management-channel protocol + agent-core security tests.

Covers requirement 10: replay, expiry, tampering, path-escape and duplicate-operation, plus key
rotation, response sanitisation, idempotency and boundary refusal.
"""
import uuid
from contextlib import contextmanager

from django.test import SimpleTestCase, TestCase, override_settings

from terminal_provisioning import mgmt_protocol as proto
from terminal_provisioning.mgmt_agent_core import (
    AgentError, BetaProvisioningAgent, DEFAULT_BETA_ROOT, derive_canonical_runtime_dir, is_beneath)

KEYRING = {"k1": "s3cret-key-one", "k2": "s3cret-key-two"}
RUUID = "12345678-1234-5678-1234-567812345678"


def _req(op="MATERIALISE", *, now=1_000_000, key_id="k1", job_id=7, ruuid=RUUID, ttl=30, nonce=None):
    return proto.sign_request(provisioning_job_id=job_id, runtime_uuid=ruuid, operation=op,
                              correlation_id="corr-1", keyring=KEYRING, key_id=key_id, now=now,
                              ttl_seconds=ttl, nonce=nonce)


class ProtocolTests(SimpleTestCase):
    def _verify(self, req, *, now=1_000_010, seen=None):
        seen = seen if seen is not None else set()

        def burn(n, e):
            if n in seen:
                return False
            seen.add(n)
            return True
        return proto.verify_request(req, keyring=KEYRING, now=now, nonce_burn=burn)

    def test_sign_verify_roundtrip(self):
        got = self._verify(_req())
        self.assertEqual(got["operation"], "MATERIALISE")
        self.assertEqual(got["runtime_uuid"], RUUID)

    def test_tampered_field_fails_signature(self):
        req = _req()
        req["operation"] = "TOMBSTONE"   # tamper after signing
        with self.assertRaises(proto.ProtocolError) as c:
            self._verify(req)
        self.assertEqual(c.exception.reason_code, "bad_signature")

    def test_expired_request_rejected(self):
        req = _req(now=1_000_000, ttl=5)
        # 10s later: within the 30s skew window but past the 5s expiry → distinctly "request_expired".
        with self.assertRaises(proto.ProtocolError) as c:
            self._verify(req, now=1_000_010)
        self.assertEqual(c.exception.reason_code, "request_expired")

    def test_timestamp_skew_rejected(self):
        req = _req(now=1_000_000)
        with self.assertRaises(proto.ProtocolError) as c:
            self._verify(req, now=1_000_000 + 5_000)   # far future ts vs now
        self.assertEqual(c.exception.reason_code, "timestamp_skew")

    def test_replayed_nonce_rejected(self):
        seen = set()
        req = _req(nonce="fixed-nonce")
        self._verify(req, seen=seen)                    # first: ok, nonce remembered
        with self.assertRaises(proto.ProtocolError) as c:
            self._verify(_req(nonce="fixed-nonce"), seen=seen)   # replay
        self.assertEqual(c.exception.reason_code, "nonce_replayed")

    def test_unknown_key_id_rejected(self):
        req = _req()
        req["key_id"] = "k-unknown"
        with self.assertRaises(proto.ProtocolError) as c:
            self._verify(req)
        self.assertEqual(c.exception.reason_code, "unknown_key_id")

    def test_key_rotation_verify_accepts_any_known_key(self):
        req = _req(key_id="k2")               # signed with the rotated key
        got = self._verify(req)               # keyring holds both k1 + k2
        self.assertEqual(got["key_id"], "k2")

    def test_disallowed_operation_cannot_be_signed(self):
        with self.assertRaises(proto.ProtocolError):
            proto.sign_request(provisioning_job_id=1, runtime_uuid=RUUID, operation="RM_RF",
                               correlation_id="x", keyring=KEYRING, key_id="k1", now=1)

    def test_nonce_not_burned_on_bad_signature(self):
        # An unsigned/forged request must NOT consume the victim's nonce.
        seen = set()
        forged = _req(nonce="victim-nonce")
        forged["signature"] = "00" * 32
        with self.assertRaises(proto.ProtocolError):
            self._verify(forged, seen=seen)
        self.assertNotIn("victim-nonce", seen)   # nonce store untouched


# ── agent-core fakes ──
class _Nonce:
    def __init__(self): self._s = set()
    def seen(self, n): return n in self._s
    def burn(self, n, e):
        if n in self._s:
            return False
        self._s.add(n)
        return True


class _Idem:
    def __init__(self): self._d = {}
    def get(self, j, op): return self._d.get((j, op))
    def put(self, j, op, resp): self._d[(j, op)] = resp


class _Locks:
    def __init__(self): self.acquired = []
    @contextmanager
    def acquire(self, u):
        self.acquired.append(u)
        yield


def _agent(op_impls=None, resolve=lambda p: None, manifest=None, now=1_000_010):
    manifest = manifest or {f"op_{o.lower()}": "sha-ok" for o in proto.ALLOWED_OPERATIONS}
    impls = op_impls or {}
    return BetaProvisioningAgent(
        keyring=KEYRING, nonce_store=_Nonce(), idempotency_store=_Idem(), op_impls=impls,
        agent_version="agent-1.0", script_manifest=manifest,
        script_versions={f"op_{o.lower()}": "ps-1.0" for o in proto.ALLOWED_OPERATIONS},
        resolve_real_path=resolve, runtime_locks=_Locks(), now_fn=lambda: now)


class AgentCoreTests(SimpleTestCase):
    def test_containment_helpers(self):
        self.assertTrue(is_beneath(r"C:\GuvFX\beta\accounts\x\terminal", DEFAULT_BETA_ROOT))
        self.assertFalse(is_beneath(r"C:\GuvFX\beta\accountsX\y", DEFAULT_BETA_ROOT))
        self.assertFalse(is_beneath(r"C:\Program Files\MetaTrader 5", DEFAULT_BETA_ROOT))
        self.assertEqual(derive_canonical_runtime_dir(RUUID),
                         rf"{DEFAULT_BETA_ROOT}\{RUUID}\terminal")

    def test_valid_materialise_returns_sanitised_response(self):
        calls = []
        def impl(**kw): calls.append(kw); return {"pid": 4242, "session_id": 1, "running": True,
                                                  "duration_ms": 12, "password": "SECRET", "cmdline": "x"}
        a = _agent({"MATERIALISE": impl})
        resp = a.handle(_req("MATERIALISE"))
        self.assertEqual(resp["outcome"], "ok")
        self.assertEqual(resp["pid"], 4242)
        self.assertEqual(resp["agent_version"], "agent-1.0")
        # sanitisation: secrets / cmdlines never returned
        self.assertNotIn("password", resp)
        self.assertNotIn("cmdline", resp)
        self.assertNotIn("SECRET", str(resp))
        # impl received ONLY the locally-derived canonical dir + uuid (no client path)
        self.assertEqual(calls[0]["canonical_dir"], derive_canonical_runtime_dir(RUUID))

    def test_duplicate_operation_is_idempotent_not_rerun(self):
        n = {"c": 0}
        def impl(**kw): n["c"] += 1; return {"pid": 1, "running": True}
        a = _agent({"START": impl})
        r1 = a.handle(_req("START", nonce="a"))
        # a DIFFERENT signed request (fresh nonce) for the SAME (job_id, op) must NOT re-run the impl.
        r2 = a.handle(_req("START", nonce="b"))
        self.assertEqual(n["c"], 1)              # impl ran exactly once
        self.assertEqual(r1, r2)

    def test_replay_same_request_denied(self):
        a = _agent({"START": lambda **k: {"pid": 1}})
        req = _req("START", nonce="same")
        a.handle(req)
        resp = a.handle(req)   # identical bytes replayed
        self.assertEqual(resp["outcome"], "denied")
        self.assertEqual(resp["reason_code"], "nonce_replayed")

    def test_reparse_point_escape_refused(self):
        # canonical dir is a junction that resolves OUTSIDE the beta root → refuse.
        a = _agent({"MATERIALISE": lambda **k: {"pid": 1}},
                   resolve=lambda p: r"C:\Windows\System32")
        resp = a.handle(_req("MATERIALISE"))
        self.assertEqual(resp["outcome"], "denied")
        self.assertEqual(resp["reason_code"], "reparse_escape")

    def test_bad_uuid_refused(self):
        a = _agent({"MATERIALISE": lambda **k: {"pid": 1}})
        resp = a.handle(_req("MATERIALISE", ruuid="not-a-uuid"))
        self.assertEqual(resp["outcome"], "denied")
        self.assertEqual(resp["reason_code"], "bad_runtime_uuid")

    def test_impl_integrity_mismatch_refused(self):
        m = {f"op_{o.lower()}": "sha-ok" for o in proto.ALLOWED_OPERATIONS}
        m["op_materialise:actual"] = "sha-TAMPERED"
        a = _agent({"MATERIALISE": lambda **k: {"pid": 1}}, manifest=m)
        resp = a.handle(_req("MATERIALISE"))
        self.assertEqual(resp["outcome"], "denied")
        self.assertEqual(resp["reason_code"], "impl_integrity_mismatch")

    def test_mutating_op_takes_runtime_lock(self):
        locks = _Locks()
        a = BetaProvisioningAgent(
            keyring=KEYRING, nonce_store=_Nonce(), idempotency_store=_Idem(),
            op_impls={"STOP": lambda **k: {"pid": 1}}, agent_version="agent-1.0",
            script_manifest={"op_stop": "sha-ok"}, script_versions={"op_stop": "ps-1.0"},
            resolve_real_path=lambda p: None, runtime_locks=locks, now_fn=lambda: 1_000_010)
        a.handle(_req("STOP"))
        self.assertEqual(locks.acquired, [RUUID])   # single-op lock acquired for the mutating op

    def test_release_dispatches_outside_the_runtime_lock(self):
        """RELEASE (ADR 0014) is the ONLY lifecycle op that runs OUTSIDE the per-runtime mutation lock —
        it must, because ``no_mutation_lock_held`` is one of its release proofs. It still dispatches and
        its ``available`` signal survives the response sanitiser so the backend can free the slot."""
        locks = _Locks()
        a = BetaProvisioningAgent(
            keyring=KEYRING, nonce_store=_Nonce(), idempotency_store=_Idem(),
            op_impls={"RELEASE": lambda **k: {"released": True, "available": True, "slot": 1}},
            agent_version="agent-1.0", script_manifest={"op_release": "sha-ok"},
            script_versions={"op_release": "ps-1.0"}, resolve_real_path=lambda p: None,
            runtime_locks=locks, now_fn=lambda: 1_000_010)
        resp = a.handle(_req("RELEASE"))
        self.assertEqual(resp["outcome"], "ok")
        self.assertTrue(resp["available"])                 # survives sanitisation → backend frees the slot
        self.assertEqual(locks.acquired, [])               # NOT taken: RELEASE runs outside the lock

    def test_denied_request_not_stored_idempotently(self):
        # A denial (expired) must not poison the idempotency store — a later valid request can succeed.
        n = {"c": 0}
        def impl(**kw): n["c"] += 1; return {"pid": 1}
        a = _agent({"MATERIALISE": impl})
        expired = _req("MATERIALISE", now=1_000_000, ttl=1, nonce="x")   # expired at now=1_000_010
        r1 = a.handle(expired)
        self.assertEqual(r1["outcome"], "denied")
        r2 = a.handle(_req("MATERIALISE", nonce="y"))                    # fresh valid request
        self.assertEqual(r2["outcome"], "ok")
        self.assertEqual(n["c"], 1)


@override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
class ClientAgentRoundTripTests(TestCase):
    """The backend client and the Windows agent, wired together over the signed protocol, drive a real
    beta runtime through the provisioner to a verified RUNNING state + Verification Report — with NO
    command/path/credential ever crossing the channel."""

    def _admitted_account(self):
        from django.contrib.auth import get_user_model
        from billing.models import BetaTester
        from trading.models import TradingAccount
        from trading.crypto import encrypt_password
        U = get_user_model()
        BetaTester.objects.create(email="rt@example.invalid")
        u = U.objects.create_user(username="rt", email="rt@example.invalid", password="x")
        return TradingAccount.objects.create(
            user=u, name="A", account_number="808080", broker_name="DemoBroker",
            is_demo=True, password_enc=encrypt_password("pw"))

    def _agent_and_transport(self):
        # op impls: materialise/start return nothing; verify observes a live process in Session 1.
        impls = {
            "MATERIALISE": lambda **k: {"duration_ms": 5},
            "START": lambda **k: {"pid": 13020, "session_id": 1},
            "VERIFY": lambda **k: {"running": True, "logged_in": False, "pid": 13020, "session_id": 1},
            "STOP": lambda **k: {},
            "TOMBSTONE": lambda **k: {},
        }
        agent = _agent(impls)
        agent.now_fn = lambda: int(__import__("time").time())   # align with the client's real clock
        return agent, (lambda base_url, req: agent.handle(req))

    def test_provision_via_signed_channel_reaches_running_with_report(self):
        from terminal_provisioning import beta_capacity as cap
        from terminal_provisioning.models import ProvisioningJob, ProvisioningVerificationReport, RuntimeState
        from terminal_provisioning.provisioner import advance_provisioning_job, enqueue_op
        from terminal_provisioning.mgmt_client import AgentWindowsProvisioner

        acct = self._admitted_account()
        rt = cap.reserve_beta_slot(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        _agent_obj, transport = self._agent_and_transport()
        client = AgentWindowsProvisioner(job_id=job.id, transport=transport,
                                         keyring=KEYRING, key_id="k1")
        job = advance_provisioning_job(job, client)

        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        rep = ProvisioningVerificationReport.objects.get(runtime=rt)
        self.assertTrue(rep.verified)
        self.assertFalse(rep.broker_login_verified)     # broker-independent: no login crossed the channel
        self.assertEqual(rep.process_pid, 13020)
        self.assertEqual(rep.windows_session, 1)

    def test_denied_when_agent_refuses(self):
        from terminal_provisioning import beta_capacity as cap
        from terminal_provisioning.models import ProvisioningJob, RuntimeState
        from terminal_provisioning.provisioner import advance_provisioning_job, enqueue_op
        from terminal_provisioning.mgmt_client import AgentWindowsProvisioner

        acct = self._admitted_account()
        rt = cap.reserve_beta_slot(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        # agent whose MATERIALISE raises → sanitised error outcome → client raises → step fails (retryable)
        impls = {"MATERIALISE": lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
                 "START": lambda **k: {}, "VERIFY": lambda **k: {"running": True}}
        agent = _agent(impls); agent.now_fn = lambda: int(__import__("time").time())
        client = AgentWindowsProvisioner(job_id=job.id, transport=lambda b, r: agent.handle(r),
                                         keyring=KEYRING, key_id="k1")
        job = advance_provisioning_job(job, client)
        rt.refresh_from_db()
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)   # never reached RUNNING on an agent error
