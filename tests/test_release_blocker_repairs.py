import re
import unittest

import core.engine as E


class ReleaseBlockerRepairTest(unittest.TestCase):
    def test_client_ids_are_unique_under_burst_generation(self):
        om = E.OrderManager(object())
        ids = [om._generate_client_id("BTC/USDT:USDT", "BUY") for _ in range(500)]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(len(x) <= 40 for x in ids))
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_]+", x) for x in ids))


if __name__ == "__main__":
    unittest.main()
