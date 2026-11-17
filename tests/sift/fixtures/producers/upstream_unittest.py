"""Intentionally failing stdlib unittest fixture producer."""

import unittest


class CheckoutTests(unittest.TestCase):
    def test_checksum(self):
        for attempt in range(150):
            print(f"cache probe {attempt}: heartbeat ok")
        self.assertEqual(41, 42, "invalid invoice checksum for café/注文")

    def test_database(self):
        raise ConnectionError("checkout-db.local refused connection")

    def test_pass(self):
        self.assertTrue(True)
