"""Phase 3 (P3-C) — customer-credential ACCESS audit at the runtime-configure site.

When the provisioning driver decrypts the broker password to configure a runtime, it must emit a
redacted CREDENTIAL_ACCESSED audit (purpose="runtime-configure"), and emit NONE when the account
carries no password (credential-free beta path).
"""
import json

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from core.models import AuditEvent
from trading.crypto import encrypt_password
from trading.models import TradingAccount
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import ProvisioningJob
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


def _acct(n=1, password="brokerpw123"):
    from billing.models import BetaTester
    email = f"acc{n}@x.invalid"
    user = U.objects.create_user(username=f"acc{n}", email=email, password="x")
    BetaTester.objects.create(email=email)  # activation-gate precondition
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(7000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=(encrypt_password(password) if password else ""))


@ENABLED
class RuntimeConfigureAccessAuditTests(TestCase):
    def test_configure_decrypt_emits_access_audit(self):
        acct = _acct(1, password="s3cret-broker-pw")
        rt = cap.get_or_create_beta_runtime(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertEqual(ev.metadata["purpose"], "runtime-configure")
        self.assertEqual(ev.metadata["actor"], "terminal_provisioning")
        self.assertNotIn("s3cret-broker-pw", json.dumps(ev.metadata or {}))

    def test_no_password_emits_no_access_audit(self):
        acct = _acct(2, password="")   # credential-free runtime
        rt = cap.get_or_create_beta_runtime(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        self.assertFalse(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id)).exists())
