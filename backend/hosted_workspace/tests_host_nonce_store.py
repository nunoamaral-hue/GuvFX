"""Stream 7C - tests for the hosted executor's durable single-use nonce store."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

from nonce_store import SqliteNonceStore  # noqa: E402


class NonceStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "nonces.sqlite")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_first_use_true_replay_false(self):
        store = SqliteNonceStore(self.path)
        self.assertTrue(store.burn("n1", 9999999999))
        self.assertFalse(store.burn("n1", 9999999999))   # replay
        self.assertTrue(store.burn("n2", 9999999999))    # distinct nonce
        store.close()

    def test_durable_across_reopen(self):
        store = SqliteNonceStore(self.path)
        self.assertTrue(store.burn("dur", 9999999999))
        store.close()
        reopened = SqliteNonceStore(self.path)            # simulate a service restart
        self.assertFalse(reopened.burn("dur", 9999999999))   # still burned -> replay refused
        reopened.close()

    def test_purge_expired_removes_only_expired(self):
        store = SqliteNonceStore(self.path)
        self.assertTrue(store.burn("old", 100))
        self.assertTrue(store.burn("new", 9999999999))
        store.purge_expired(1000)                          # drops 'old' (expiry 100 < 1000), keeps 'new'
        self.assertTrue(store.burn("old", 100))            # 'old' gone -> can be inserted again
        self.assertFalse(store.burn("new", 9999999999))    # 'new' still present
        store.close()


if __name__ == "__main__":
    unittest.main()
