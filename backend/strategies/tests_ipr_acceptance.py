"""IPR Area F — backend API-sequence acceptance test for the self-service beta journey.

Walks the journey a newly-deployed beta user takes, at the API level, and pins the DARK invariant: with
BETA_SELF_SERVE_ARM_ENABLED OFF the arm endpoint refuses (409 arming_disabled); only with it ON does the
account reach ARMED. Proves the Area B contradiction repairs (runtime ready + no mt5_instance, no
"not connected" 409) and the Area D contract (arm status codes) are consistent end-to-end.

No execution flag is enabled beyond the arm-authority flag; no order is placed.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from billing.models import BetaTester
from strategies.models import StrategyAssignment
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import AccountRuntime, ProvisioningJob
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op
from trading.crypto import encrypt_password
from trading.models import TradingAccount

User = get_user_model()

MP = "mp-010"  # Wayond WIM Strategy → signal_source ti_signals
STATUS_URL = f"/api/strategies/strategies/signal-copy/status/?marketplace_strategy_id={MP}"
ARM_URL = "/api/strategies/strategies/signal-copy/arm/"
TOGGLE_URL = "/api/strategies/strategies/signal-copy/toggle/"

# BETA_RUNTIMES_ENABLED is needed to build a ready runtime; the arm flag stays OFF until the DARK step.
BASE = dict(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


@override_settings(**BASE)
class BetaJourneyAcceptanceTests(TestCase):
    def setUp(self):
        email = "pilot@x.invalid"
        BetaTester.objects.create(email=email)   # admitted (Area: controlled admission)
        self.user = User.objects.create_user(username=email, email=email, password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Pilot", account_number="990001", broker_name="DemoBroker",
            is_demo=True, is_active=True, password_enc=encrypt_password("pw"))
        # Runtime provisioned to RUNNING (ready): reserve slot + advance the provisioning job.
        rt = cap.reserve_beta_slot(self.acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_full_journey_dark_then_armed(self):
        # 1. Account list: runtime ready + NO legacy instance, and they never contradict.
        accts = self.client.get("/api/trading/accounts/").json()
        row = next(a for a in accts if a["id"] == self.acct.id)
        self.assertTrue(row["runtime_ready"])
        self.assertEqual(row["runtime_state"], "RUNNING")
        self.assertIsNone(row["mt5_instance"])

        # 2. Account actions do NOT tell a ready-runtime beta account "not connected".
        r = self.client.post(f"/api/trading/accounts/{self.acct.id}/test-mt5/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["reason"], "broker_login_deferred")

        # 3. Signal-copy status: not armed yet.
        st = self.client.get(STATUS_URL).json()
        self.assertFalse(st["armed"])

        # 4. DARK invariant: with the arm flag OFF, arming is refused (this is why Telegram is unarmed).
        with override_settings(BETA_SELF_SERVE_ARM_ENABLED=False):
            r = self.client.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id},
                                 format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "arming_disabled")
        self.assertFalse(
            StrategyAssignment.objects.filter(account=self.acct).exists(),
            "no assignment may exist while the arm flag is OFF")

        # 5. With the arm flag ON (Sponsor-gated in prod), the account reaches ARMED.
        with override_settings(BETA_SELF_SERVE_ARM_ENABLED=True):
            r = self.client.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id},
                                 format="json")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.json()["status"], "armed")

            # 6. Status now reflects ARMED (from the backend, not a click).
            st = self.client.get(STATUS_URL).json()
            self.assertTrue(st["armed"])
            self.assertTrue(st["enabled"])

            # 7. Toggle disable → resume: the ON state is backend-confirmed each time.
            r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": False},
                                 format="json")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "disabled")
            r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": True},
                                 format="json")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "enabled")

        # 8. The armed assignment is AUTO_DEMO + stage LIVE — authority only; no execution flag is on.
        asn = StrategyAssignment.objects.get(account=self.acct)
        self.assertEqual(asn.execution_mode, StrategyAssignment.ExecutionMode.AUTO_DEMO)
        self.assertEqual(asn.stage, StrategyAssignment.STAGE_LIVE)
