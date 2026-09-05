import os
import unittest
from scanner.universe import classify_legacy as classify, build_balanced

class DeepUniverseTest(unittest.TestCase):
    def test_asset_classification(self):
        self.assertEqual(classify("GOLD(XAU)/USDT", {"base":"GOLD(XAU)","type":"swap"}), "GOLD")
        self.assertEqual(classify("Oil-WTI-USDT", {"base":"OILWTI","type":"swap"}), "OIL")
        self.assertEqual(classify("US500-USDT", {"base":"US500","type":"swap"}), "INDEX")
        self.assertEqual(classify("AAPL-USDT", {"base":"AAPL","type":"swap"}), "STOCK")
        self.assertEqual(classify("BTC/USDT:USDT", {"base":"BTC","type":"swap"}), "CRYPTO")

    def test_balanced_universe(self):
        markets = {
            "BTC/USDT:USDT": {"base":"BTC","type":"swap"},
            "ETH/USDT:USDT": {"base":"ETH","type":"swap"},
            "AAPL-USDT": {"base":"AAPL","type":"swap"},
            "US500-USDT": {"base":"US500","type":"swap"},
            "GOLD(XAU)-USDT": {"base":"GOLD(XAU)","type":"swap"},
            "Oil-WTI-USDT": {"base":"OILWTI","type":"swap"},
        }
        rows = build_balanced(markets, radar_limit=20)
        assets = {r["asset_class"] for r in rows}
        self.assertEqual(assets, {"CRYPTO","STOCK","INDEX","GOLD","OIL"})

if __name__ == "__main__":
    unittest.main()
