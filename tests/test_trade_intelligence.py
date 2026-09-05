import unittest
import numpy as np
import pandas as pd

from core.trade_intelligence import analyze_setup, TradeManagementBoard


class TradeIntelligenceTest(unittest.TestCase):
    def _df(self):
        rng = np.random.default_rng(42)
        n = 260
        close = 100 + np.cumsum(rng.normal(0, 0.12, n))
        open_ = close + rng.normal(0, 0.05, n)
        high = np.maximum(open_, close) + 0.15
        low = np.minimum(open_, close) - 0.15
        volume = np.full(n, 1000.0)
        # Deliberate institutional-style sweep/displacement sequence.
        low[-8] = 98.0; open_[-8] = 99.0; close[-8] = 99.2; high[-8] = 99.5; volume[-8] = 1800
        open_[-7] = 99.2; close[-7] = 100.6; high[-7] = 100.9; low[-7] = 99.0; volume[-7] = 2400
        open_[-6] = 100.6; close[-6] = 101.0; high[-6] = 101.2; low[-6] = 100.4; volume[-6] = 1700
        open_[-5] = 101.0; close[-5] = 100.8; high[-5] = 101.1; low[-5] = 100.5; volume[-5] = 1300
        open_[-4] = 100.8; close[-4] = 100.9; high[-4] = 101.1; low[-4] = 100.6; volume[-4] = 1200
        open_[-3] = 100.9; close[-3] = 101.2; high[-3] = 101.4; low[-3] = 100.7; volume[-3] = 1500
        open_[-2] = 101.2; close[-2] = 101.5; high[-2] = 101.7; low[-2] = 101.0; volume[-2] = 1600
        open_[-1] = 101.5; close[-1] = 101.8; high[-1] = 102.0; low[-1] = 101.2; volume[-1] = 1800
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    def test_snapshot_has_style_timing_phase_and_zone_behaviour(self):
        df = self._df()
        r = analyze_setup(df, "BUY", float(df.close.iloc[-1]))
        self.assertTrue(r["valid"])
        self.assertIn(r["trade_style"], {"SCALP", "SWING", "TREND"})
        self.assertIn("timing", r)
        self.assertIn("phase", r)
        self.assertIn("behaviour", r)
        self.assertIn("zone", r["evidence"])

    def test_board_never_places_orders_and_tracks_stage(self):
        r = analyze_setup(self._df(), "BUY", 101.8)
        board = TradeManagementBoard()
        out = board.evaluate(r, roe=6.0, continuation_probability=0.80, distribution_risk=10)
        self.assertEqual(out["stage"], "CONTINUATION")
        self.assertEqual(board.to_dict()["stage"], "CONTINUATION")

    def test_board_defends_on_distribution(self):
        r = analyze_setup(self._df(), "BUY", 101.8)
        board = TradeManagementBoard()
        out = board.evaluate(r, roe=10.0, continuation_probability=0.45, distribution_risk=75)
        self.assertEqual(out["stage"], "DISTRIBUTION")


if __name__ == "__main__":
    unittest.main()
