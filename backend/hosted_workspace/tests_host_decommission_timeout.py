"""GFX-HOST-DECOMMISSION-REPAIR-001 — the DECOMMISSION_RUNTIME client read-timeout fix and the two observed
failure boundaries:

  * #55 (inactive runtime) — the Stage-2 teardown succeeds slowly on the host, but the backend's default 30s
    client read-timeout gives up first → a FALSE ``host_unavailable`` and the tombstoned slot is never reclaimed.
    Fix: give DECOMMISSION_RUNTIME a client read-timeout past the host's 600s primitive budget (mirrors
    MATERIALISE_RUNTIME), so a slow-but-successful teardown returns a real response. One POST, no retry → the
    longer wait can never re-run the destructive, idempotent teardown.
  * #25 (LIVE runtime) — the teardown genuinely fails host-side on an in-use/locked runtime; the host's unsigned
    error body carries no key_id and ``verify_hosted_response`` maps that to ``unknown_key_id`` (a MISLABELED
    teardown failure, not a signing-key fault). The timeout fix neither does nor should clean such an account.

Protocol-level replay / wrong-tenant / wrong-key / stale-request rejection is proven in tests_host_protocol.py
(test_replay_rejected, test_bad_account_id_refused / test_tamper_account_id_fails_signature,
test_unknown_key_id_refused / test_tamper_operation_fails_signature, test_timestamp_skew_rejected /
test_expired_rejected) and is unaffected by this change. Pure, no host, no DB (SimpleTestCase).
"""
import urllib.request

from django.test import SimpleTestCase

from hosted_workspace import host_executor as HX
from hosted_workspace.host_executor import SignedHostExecutor
from hosted_workspace.tests_host_executor import KR, T, _fake_seal, _signing_transport


def _ex(transport, *, key_id="k1", account_id=14):
    return SignedHostExecutor(account_id=account_id, rdp_host="x", transport=transport, keyring=KR,
                              key_id=key_id, base_url="https://host.invalid", seal_password=_fake_seal,
                              reserved_ids="", clock=lambda: T)


class DecommissionClientTimeoutTests(SimpleTestCase):
    """The fix: the client read-timeout for the long teardown must exceed the host's 600s primitive budget."""

    def _captured_timeout(self, op):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _Resp()

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            HX._http_transport("http://host.invalid:8790", {"operation": op})
        finally:
            urllib.request.urlopen = orig
        return captured["timeout"]

    def test_decommission_waits_past_host_primitive_budget(self):
        # Regression (#55): DECOMMISSION_RUNTIME must NOT fall back to the 30s default while the host runs the
        # teardown under a 600s budget — that false host_unavailable is exactly the bug.
        self.assertGreaterEqual(self._captured_timeout("DECOMMISSION_RUNTIME"), 600)

    def test_unlisted_op_keeps_default_timeout(self):
        # The quick teardown steps (REMOVE_OBSERVER etc.) legitimately keep the fast default.
        self.assertEqual(self._captured_timeout("REMOVE_OBSERVER"), HX._DEFAULT_HTTP_TIMEOUT_S)


class DecommissionBoundaryTests(SimpleTestCase):
    def test_host_unavailable_on_raising_transport(self):
        # #55 boundary: a client read-timeout (or any transport error) on a slow teardown → fail closed. Post
        # ADR-0091 the fail-closed reason is the specific, truthful `host_unreachable` (never reached the host).
        def boom(base_url, request):
            raise OSError("timed out")

        res = _ex(boom).decommission_runtime(username="guvfx_u_14", runtime_root=r"C:\GuvFX\accounts\14")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "host_unreachable")

    def test_unknown_key_id_on_unsigned_host_error_response(self):
        # #25 boundary: the teardown fails host-side on a LIVE/locked runtime and the host returns an unsigned
        # error body with no key_id; verify_hosted_response maps a response lacking a known key_id to
        # unknown_key_id — a mislabeled teardown failure, NOT a signing-key fault.
        def unsigned_error(base_url, request):
            return {"outcome": "denied", "reason_code": "runtime_in_use"}  # no key_id / no signature

        res = _ex(unsigned_error).decommission_runtime(username="guvfx_u_14", runtime_root=r"C:\GuvFX\accounts\14")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "unknown_key_id")

    def test_unknown_key_id_on_backend_sign_when_key_absent(self):
        # The other producer of unknown_key_id: a key_id absent from the keyring fails at SIGN time (backend),
        # before any transport. (Not #25's live case — host+backend share hx-1 — but part of the boundary map.)
        res = _ex(_signing_transport({}), key_id="absent").decommission_runtime(
            username="guvfx_u_14", runtime_root=r"C:\GuvFX\accounts\14")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "unknown_key_id")

    def test_successful_decommission_is_idempotent(self):
        # #55 after the fix: a valid signed ok response completes; repeated calls stay ok (already-absent => ok).
        ex = _ex(_signing_transport({"DECOMMISSION_RUNTIME": {"ok": True, "steps": {"runtime_dir": "absent"}}}))
        for _ in range(2):
            res = ex.decommission_runtime(username="guvfx_u_14", runtime_root=r"C:\GuvFX\accounts\14", port=8812)
            self.assertTrue(res["ok"])

    def test_decommission_confines_and_refuses_foreign_identity(self):
        # The signed path still refuses a foreign identity/runtime BEFORE any transport (no weakening).
        sent = {"n": 0}

        def counting(base_url, request):
            sent["n"] += 1
            return _signing_transport({})(base_url, request)

        res = _ex(counting).decommission_runtime(username="guvfx_u_99", runtime_root=r"C:\GuvFX\accounts\14")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "confinement_mismatch")
        self.assertEqual(sent["n"], 0)
