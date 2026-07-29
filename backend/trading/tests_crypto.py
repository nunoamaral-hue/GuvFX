"""Phase 3 / ADR-0019 (SEC-CRYPTO-001) — customer-credential encryption decoupling.

Proves: explicit-key encryption; backward-compatible reads of legacy DJANGO_SECRET_KEY-derived
ciphertext; that NEW writes are decoupled from DJANGO_SECRET_KEY; MultiFernet rotation; fail-closed
with no key; and the re-encryption migration command moves stored ciphertext onto the explicit primary.
"""
import os
from unittest import mock

from cryptography.fernet import Fernet, InvalidToken
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from trading.crypto import decrypt_password, encrypt_password, encryption_key_status
from trading.models import TradingAccount

U = get_user_model()

# Env where only DJANGO_SECRET_KEY is available (the legacy coupled mode) — explicit keys blanked so
# the process's ambient GUVFX_FERNET_KEY (if any) cannot leak into the assertion.
_DERIVED_ONLY = {"GUVFX_FERNET_KEY": "", "GUVFX_FERNET_KEYS": "", "DJANGO_SECRET_KEY": "unit-test-secret"}


def _explicit(key):
    return {"GUVFX_FERNET_KEY": key, "GUVFX_FERNET_KEYS": "", "DJANGO_SECRET_KEY": "unit-test-secret"}


def _keyed(**env):
    base = {"GUVFX_FERNET_KEY": "", "GUVFX_FERNET_KEYS": "", "DJANGO_SECRET_KEY": ""}
    base.update(env)
    return base


class CryptoDecouplingTests(SimpleTestCase):
    def test_explicit_key_roundtrip(self):
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            tok = encrypt_password("brokerpw")
            self.assertEqual(decrypt_password(tok), "brokerpw")

    def test_empty_and_none_passthrough(self):
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            self.assertEqual(encrypt_password(""), "")
            self.assertEqual(encrypt_password(None), "")
            self.assertEqual(decrypt_password(""), "")
            self.assertEqual(decrypt_password(None), "")

    def test_derived_fallback_roundtrips_but_warns(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            with self.assertLogs("trading.crypto", level="WARNING") as cm:
                tok = encrypt_password("pw")
            self.assertEqual(decrypt_password(tok), "pw")
            self.assertTrue(any("SEC-CRYPTO-001" in m for m in cm.output))

    def test_backward_compat_reads_legacy_derived_ciphertext(self):
        # Ciphertext written in the legacy derived-only mode must still decrypt once an explicit key is set.
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            legacy = encrypt_password("legacypw")
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            self.assertEqual(decrypt_password(legacy), "legacypw")

    def test_new_ciphertext_is_decoupled_from_django_secret_key(self):
        # The core SEC-CRYPTO-001 fix: a value encrypted with an explicit key must NOT be readable by
        # the DJANGO_SECRET_KEY-derived key alone — so rotating DJANGO_SECRET_KEY cannot destroy it.
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            tok = encrypt_password("secretpw")
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            with self.assertRaises(InvalidToken):   # specifically undecryptable, not merely "some error"
                decrypt_password(tok)

    def test_multifernet_rotation_reads_old_writes_new(self):
        knew = Fernet.generate_key().decode()
        kold = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=kold)):
            tok_old = encrypt_password("pw")
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEYS=f"{knew},{kold}")):
            self.assertEqual(decrypt_password(tok_old), "pw")   # reads old key
            tok_new = encrypt_password("pw2")                    # writes under the primary (first) key
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=knew)):
            self.assertEqual(decrypt_password(tok_new), "pw2")
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=kold)):
            with self.assertRaises(InvalidToken):
                decrypt_password(tok_new)                        # new token is NOT under the old key

    def test_keys_first_entry_is_primary(self):
        k1 = Fernet.generate_key().decode()
        k2 = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEYS=f"{k1},{k2}")):
            tok = encrypt_password("pw")
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=k1)):
            self.assertEqual(decrypt_password(tok), "pw")        # primary is the first entry

    def test_keys_takes_precedence_over_single_key(self):
        # Both GUVFX_FERNET_KEYS and GUVFX_FERNET_KEY set → KEYS wins; encryption uses KEYS[0].
        klist = Fernet.generate_key().decode()
        ksingle = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEYS=klist, GUVFX_FERNET_KEY=ksingle)):
            tok = encrypt_password("pw")
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=klist)):
            self.assertEqual(decrypt_password(tok), "pw")        # decrypts under the KEYS entry
        with mock.patch.dict(os.environ, _keyed(GUVFX_FERNET_KEY=ksingle)):
            with self.assertRaises(InvalidToken):
                decrypt_password(tok)                            # NOT under the ignored single key

    def test_fail_closed_when_no_key_material(self):
        with mock.patch.dict(os.environ, _keyed(SECRET_KEY="")):
            with self.assertRaises(RuntimeError):
                encrypt_password("pw")
            with self.assertRaises(RuntimeError):
                decrypt_password("x")


class KeyStatusTests(SimpleTestCase):
    def test_status_explicit(self):
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            s = encryption_key_status()
        self.assertTrue(s["explicit_key_configured"])
        self.assertFalse(s["derived_from_django_secret_key"])
        self.assertGreaterEqual(s["read_key_count"], 1)

    def test_status_derived_flags_sec_crypto_001(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            s = encryption_key_status()
        self.assertFalse(s["explicit_key_configured"])
        self.assertTrue(s["derived_from_django_secret_key"])

    def test_status_no_keys(self):
        with mock.patch.dict(os.environ, _keyed(SECRET_KEY="")):
            s = encryption_key_status()
        self.assertFalse(s["explicit_key_configured"])
        self.assertEqual(s["read_key_count"], 0)
        self.assertFalse(s["derived_from_django_secret_key"])


class ReencryptCommandTests(TestCase):
    def _mk_account(self, ciphertext, n=0):
        user = U.objects.create_user(
            username=f"reenc-{n}", email=f"reenc-{n}@x.invalid", password="x")
        return TradingAccount.objects.create(
            user=user, name="A", account_number=f"ACC{n}", broker_name="DemoBroker", is_demo=True, password_enc=ciphertext)

    def test_reencrypt_moves_derived_ciphertext_to_explicit_primary(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            legacy = encrypt_password("brokerpw")
        acct = self._mk_account(legacy)
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            call_command("reencrypt_customer_credentials")
            acct.refresh_from_db()
            self.assertEqual(decrypt_password(acct.password_enc), "brokerpw")
        # Now decoupled: the DJANGO_SECRET_KEY-derived key alone can no longer read it.
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            with self.assertRaises(InvalidToken):
                decrypt_password(acct.password_enc)

    def test_reencrypt_refuses_without_explicit_key(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            legacy = encrypt_password("pw")
            acct = self._mk_account(legacy)
            with self.assertRaises(CommandError):                 # non-zero exit, must refuse
                call_command("reencrypt_customer_credentials")
            acct.refresh_from_db()
            self.assertEqual(acct.password_enc, legacy)           # unchanged

    def test_reencrypt_refuses_on_separator_only_keys(self):
        # The HIGH finding: a whitespace/separator-only GUVFX_FERNET_KEYS must NOT be treated as an
        # explicit key. If it were, the command would re-encrypt under the DJANGO_SECRET_KEY-derived
        # key (the exact coupling it removes) while the audit falsely reported "decoupled".
        env = {"GUVFX_FERNET_KEYS": " , ", "GUVFX_FERNET_KEY": "", "DJANGO_SECRET_KEY": "unit-test-secret"}
        with mock.patch.dict(os.environ, env):
            status = encryption_key_status()
            self.assertFalse(status["explicit_key_configured"])
            self.assertTrue(status["derived_from_django_secret_key"])   # honestly still coupled
            legacy = encrypt_password("pw")
            acct = self._mk_account(legacy)
            with self.assertRaises(CommandError):
                call_command("reencrypt_customer_credentials")
            acct.refresh_from_db()
            self.assertEqual(acct.password_enc, legacy)                 # untouched

    def test_reencrypt_fail_closed_leaves_undecryptable_row_unchanged(self):
        # Central promise: a value that cannot be decrypted is left byte-for-byte unchanged and
        # counted as failed (never dropped), while valid rows in the same batch still move.
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            good_ct = encrypt_password("goodpw")
        good = self._mk_account(good_ct, n=1)
        bad = self._mk_account("not-a-fernet-token", n=2)
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            with self.assertRaises(CommandError):                       # failed>0 → non-zero exit
                call_command("reencrypt_customer_credentials")
            good.refresh_from_db()
            bad.refresh_from_db()
            self.assertEqual(decrypt_password(good.password_enc), "goodpw")   # good row moved
        self.assertEqual(bad.password_enc, "not-a-fernet-token")             # bad row untouched, not dropped
        self.assertNotEqual(bad.password_enc, "")

    def test_reencrypt_idempotent_second_run(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            legacy = encrypt_password("pw")
        acct = self._mk_account(legacy)
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            call_command("reencrypt_customer_credentials")
            call_command("reencrypt_customer_credentials")             # second run must not error
            acct.refresh_from_db()
            self.assertEqual(decrypt_password(acct.password_enc), "pw")

    def test_reencrypt_covers_all_three_targets(self):
        # Not only TradingAccount: AccountProvisioning.password_enc and TwoFactorSecret.secret_enc.
        from onboarding.models import TwoFactorSecret
        from terminal_provisioning.models import AccountProvisioning

        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            acct_ct = encrypt_password("brokerpw")
            prov_ct = encrypt_password("winpw")
            totp_ct = encrypt_password("TOTPSECRET")
        acct = self._mk_account(acct_ct, n=3)
        prov = AccountProvisioning.objects.create(
            trading_account=acct, windows_username="guvfx_u_x", password_enc=prov_ct,
            runtime_root="C:/GuvFX/accounts/x")
        totp_user = U.objects.create_user(username="totp-u", email="totp@x.invalid", password="x")
        totp = TwoFactorSecret.objects.create(user=totp_user, secret_enc=totp_ct)
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            call_command("reencrypt_customer_credentials")
            acct.refresh_from_db(); prov.refresh_from_db(); totp.refresh_from_db()
            self.assertEqual(decrypt_password(acct.password_enc), "brokerpw")
            self.assertEqual(decrypt_password(prov.password_enc), "winpw")
            self.assertEqual(decrypt_password(totp.secret_enc), "TOTPSECRET")
        # every column now decoupled from the derived key
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            for ct in (acct.password_enc, prov.password_enc, totp.secret_enc):
                with self.assertRaises(InvalidToken):
                    decrypt_password(ct)

    def test_reencrypt_dry_run_writes_nothing(self):
        with mock.patch.dict(os.environ, _DERIVED_ONLY):
            legacy = encrypt_password("pw")
        acct = self._mk_account(legacy)
        k = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, _explicit(k)):
            call_command("reencrypt_customer_credentials", "--dry-run")
            acct.refresh_from_db()
        self.assertEqual(acct.password_enc, legacy)          # unchanged by dry-run
