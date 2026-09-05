"""Orderbook side-identification stress suite.

The bot reads the LIMIT ORDER BOOK as two opposing liquidity pools:
    * the BID / buy-side orderbook (resting demand, buys),
    * the ASK / sell-side orderbook (resting supply, sells).

Correct identification of "which side is stacked" is what lets the engine
read who is in control. This suite stress-tests every consumer that touches
the two sides:

  * engine.orderbook_imbalance(ob, depth)   -> signed imbalance over top-K
  * engine.detect_walls(ob, ...)            -> per-side wall detection
  * scanner.deep_scanner._orderbook_imbalance -> the deep-scan top-5 line
  * engine.detect_liquidity_context(df)     -> candle-sweep side labels
  * engine.early_score(...)                 -> orderbook side-gating rewards

Each block runs deterministic edge fixtures PLUS randomized books with known
ground truth, so the classification is proven, not spot-checked.
"""

import random
import unittest

import numpy as np
import pandas as pd

from core.engine import (
    detect_liquidity_context,
    detect_walls,
    early_score,
    orderbook_imbalance,
)
from scanner.deep_scanner import DeepScanner


def _book(bids, asks):
    """Build a structurally valid exchange book.

    bids = descending [price, qty], asks = ascending [price, qty].
    """
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    return {"bids": [[float(p), float(q)] for p, q in bids],
            "asks": [[float(p), float(q)] for p, q in asks]}


class OrderbookImbalanceTest(unittest.TestCase):
    """Signed buy-side vs sell-side orderbook reading."""

    def test_balanced_book_is_neutral(self):
        ob = _book([(100, 10), (99, 10)], [(101, 10), (102, 10)])
        self.assertEqual(orderbook_imbalance(ob), 0.0)
        self.assertEqual(orderbook_imbalance(ob, depth=1), 0.0)

    def test_buy_heavy_book_is_positive(self):
        ob = _book([(100, 30), (99, 10)], [(101, 10), (102, 10)])
        self.assertGreater(orderbook_imbalance(ob), 0.0)
        self.assertAlmostEqual(orderbook_imbalance(ob), (40 - 20) / 60.0, places=12)

    def test_sell_heavy_book_is_negative(self):
        ob = _book([(100, 10), (99, 10)], [(101, 30), (102, 10)])
        self.assertLess(orderbook_imbalance(ob), 0.0)
        self.assertAlmostEqual(orderbook_imbalance(ob), (20 - 40) / 60.0, places=12)

    def test_one_side_present_hits_bound(self):
        self.assertEqual(orderbook_imbalance(_book([(100, 5), (99, 5)], [])), 1.0)
        self.assertEqual(orderbook_imbalance(_book([], [(101, 5), (102, 5)])), -1.0)
        self.assertEqual(orderbook_imbalance(_book([], [])), 0.0)

    def test_missing_and_empty_books_are_neutral(self):
        self.assertEqual(orderbook_imbalance(None), 0.0)
        self.assertEqual(orderbook_imbalance({}), 0.0)
        self.assertEqual(orderbook_imbalance({"bids": [] , "asks": []}), 0.0)

    def test_depth_parameter_cuts_to_top_k_only(self):
        # A huge bid wall sits at level 3-4; with depth=2 it must be invisible.
        bids = [(100, 10), (99, 10), (98, 900), (97, 900)]
        asks = [(101, 10), (102, 10), (103, 10), (104, 10)]
        ob = _book(bids, asks)
        self.assertEqual(orderbook_imbalance(ob, depth=2), 0.0)
        self.assertEqual(orderbook_imbalance(ob, depth=1), 0.0)
        # Default depth=10 consumes every level -> strongly bid-heavy.
        self.assertGreater(orderbook_imbalance(ob), 0.8)
        # exact manual value over ALL levels
        b = sum(q for _, q in bids)
        a = sum(q for _, q in asks)
        self.assertAlmostEqual(orderbook_imbalance(ob), (b - a) / (b + a), places=12)

    def test_only_quantity_counts_not_price(self):
        # Absurd price skew must not leak into the side read.
        ob = _book([(1e9, 5), (1.0, 5)], [(1e9, 5), (1.0, 5)])
        self.assertAlmostEqual(orderbook_imbalance(ob), 0.0, places=12)

    def test_string_quantities_scanner_parses_engine_ke_numeric_contract(self):
        # Real partial depths: engine assumes native floats; the deep-scan line
        # defensively coerces. The bot's production read of a string-quantity
        # book must still resolve on the scanner path.
        ob = {"bids": [[100, "5"], [99, "5"]], "asks": [[101, "2"], [102, "3"]]}
        self.assertAlmostEqual(DeepScanner._orderbook_imbalance(ob), (10 - 5) / 15.0, places=12)
        flipped = {"bids": [[100, "2"], [99, "3"]], "asks": [[101, "5"], [102, "5"]]}
        self.assertAlmostEqual(DeepScanner._orderbook_imbalance(flipped), (5 - 10) / 15.0, places=12)
        numeric = {"bids": [[100, 5.0], [99, 5.0]], "asks": [[101, 2.0], [102, 3.0]]}
        self.assertAlmostEqual(orderbook_imbalance(numeric), (10 - 5) / 15.0, places=12)

    def test_three_field_entries_and_ordering_are_tolerated(self):
        ob = {"bids": [[99, 5, 999], [100, 5, 998]],  # unsorted too
              "asks": [[102, 5, 0], [101, 5, 0]]}
        self.assertAlmostEqual(orderbook_imbalance(ob), 0.0, places=12)

    def test_random_books_match_manual_ground_truth(self):
        rng = random.Random(1337)
        for _ in range(300):
            nb, na = rng.randint(1, 12), rng.randint(1, 12)
            bids = [(100.0 - i, rng.randint(0, 40)) for i in range(nb)]
            asks = [(101.0 + i, rng.randint(0, 40)) for i in range(na)]
            ob = _book(bids, asks)
            depth = rng.randint(1, 10)
            got = orderbook_imbalance(ob, depth=depth)
            b = sum(q for _, q in bids[:depth])
            a = sum(q for _, q in asks[:depth])
            total = b + a
            want = (b - a) / total if total > 0 else 0.0
            self.assertAlmostEqual(got, want, places=12)
            self.assertTrue(-1.0 <= got <= 1.0, f"bounded: {got}")
            if total > 0:
                self.assertEqual(np.sign(got), np.sign(b - a))

    def test_random_sign_follows_heavier_side_and_direction_is_stable(self):
        rng = random.Random(7)
        for _ in range(200):
            bids = [(100.0 - i, rng.randint(1, 30)) for i in range(6)]
            asks = [(101.0 + i, rng.randint(1, 30)) for i in range(6)]
            ob = _book(bids, asks)
            v = orderbook_imbalance(ob)
            # doubling the bid side must push the read further to +1 (or stay at bound)
            ob2 = {"bids": [[p, q * 2.0] for p, q in ob["bids"]], "asks": ob["asks"]}
            v2 = orderbook_imbalance(ob2)
            self.assertGreaterEqual(v2, v - 1e-12)
            if v > 0:
                # bid-heavy: doubling reflects it even more buy-side
                self.assertGreater(v2, v)


class OrderbookWallTest(unittest.TestCase):
    """Per-side wall (spoof) identification."""

    def test_no_wall_on_uniform_book(self):
        ob = _book([(100 - i, 10) for i in range(6)], [(101 + i, 10) for i in range(6)])
        self.assertEqual(detect_walls(ob), (False, False))

    def test_bid_wall_detected_only_on_bid_side(self):
        ob = _book([(100 - i, 5) for i in range(5)] + [(99.5, 70)],
                   [(101 + i, 10) for i in range(6)])
        bid_wall, ask_wall = detect_walls(ob)
        self.assertTrue(bid_wall)
        self.assertFalse(ask_wall)

    def test_ask_wall_detected_only_on_ask_side(self):
        ob = _book([(100 - i, 10) for i in range(6)],
                   [(101 + i, 8) for i in range(5)] + [(101.5, 60)])
        bid_wall, ask_wall = detect_walls(ob)
        self.assertFalse(bid_wall)
        self.assertTrue(ask_wall)

    def test_walls_on_both_sides(self):
        ob = _book([(100 - i, 5) for i in range(5)] + [(99.5, 60)],
                   [(101 + i, 7) for i in range(5)] + [(102.0, 80)])
        self.assertEqual(detect_walls(ob), (True, True))

    def test_empty_and_missing_books(self):
        self.assertEqual(detect_walls(None), (False, False))
        self.assertEqual(detect_walls({}), (False, False))
        self.assertEqual(detect_walls(_book([], [])), (False, False))

    def test_custom_threshold_tightens_detection(self):
        sizes = [10, 10, 25]
        ob = _book([(100 - i, s) for i, s in enumerate(sizes)],
                   [(101 + i, 10) for i in range(3)])
        self.assertFalse(detect_walls(ob)[0])              # default 3.0x
        self.assertTrue(detect_walls(ob, threshold=1.5)[0])  # tighter 1.5x

    def test_walls_never_cross_report_sides(self):
        rng = random.Random(99)
        for _ in range(150):
            bids = [(100.0 - i, rng.randint(1, 30)) for i in range(6)]
            asks = [(101.0 + i, rng.randint(1, 30)) for i in range(6)]
            # place one massive order on exactly one side
            if rng.random() < 0.5:
                bids.append((99.7, rng.randint(150, 400)))
                stacked_bid = True
            else:
                asks.append((101.3, rng.randint(150, 400)))
                stacked_bid = False
            ob = _book(bids, asks)
            bid_wall, ask_wall = detect_walls(ob)
            if stacked_bid:
                self.assertTrue(bid_wall, "stacked bid side must expose a bid wall")
                self.assertFalse(ask_wall, "bid stack must not report on the ask side")
            else:
                self.assertTrue(ask_wall, "stacked ask side must expose an ask wall")
                self.assertFalse(bid_wall, "ask stack must not report on the bid side")


class CandleSweepContextTest(unittest.TestCase):
    """Candle-level sweep side labels (sell-side LP taken vs buy-side LP taken)."""

    @staticmethod
    def _base(n=15):
        # flat, wick-free candles: no bar can trip either sweep by itself
        rows = [[100.0, 100.0, 100.0, 100.0, 1000.0] for _ in range(n)]
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])

    def test_no_sweep_returns_none(self):
        df = self._base()
        self.assertIsNone(detect_liquidity_context(df, lookback=10))

    def test_sell_side_liquidity_taken_detected(self):
        df = self._base()
        # deep wick below the prior low = sell-side liquidity absorbed -> bullish
        # high == max(open, close) so the buy-side check stays silent on this bar
        df.loc[6, ["open", "high", "low", "close"]] = [100.2, 100.2, 99.0, 100.1]
        self.assertEqual(detect_liquidity_context(df, lookback=10), "sell_side_taken")

    def test_buy_side_liquidity_taken_detected(self):
        df = self._base()
        # tall wick above the prior high = buy-side liquidity absorbed -> bearish
        # low == min(open, close) so the sell-side check stays silent on this bar
        df.loc[6, ["open", "high", "low", "close"]] = [99.9, 101.0, 100.0, 100.0]
        self.assertEqual(detect_liquidity_context(df, lookback=10), "buy_side_taken")

    def test_lookback_window_boundary_respected(self):
        df = self._base()
        df.loc[6, ["open", "high", "low", "close"]] = [100.2, 100.2, 99.0, 100.1]
        # outside the lookback window the same event is invisible
        self.assertIsNone(detect_liquidity_context(df, lookback=4))
        # inside the window it is seen
        self.assertEqual(detect_liquidity_context(df, lookback=10), "sell_side_taken")

    def test_last_bar_is_never_consulted(self):
        df = self._base()
        # a violent wick on the very last (still-forming) bar must be ignored
        df.loc[14, ["open", "high", "low", "close"]] = [100.4, 101.5, 95.0, 100.3]
        self.assertIsNone(detect_liquidity_context(df, lookback=10))

    def test_short_frame_and_bad_inputs_are_safe(self):
        self.assertIsNone(detect_liquidity_context(pd.DataFrame(), lookback=10))
        self.assertIsNone(detect_liquidity_context(None, lookback=10))


class EarlyScoreOrderbookGatingTest(unittest.TestCase):
    """early_score must reward ONLY the side the orderbook actually supports."""

    N = 25

    @staticmethod
    def _df():
        c = 100.0
        rows = []
        for i in range(EarlyScoreOrderbookGatingTest.N):
            o = c
            c += 0.1
            rows.append([o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1000.0])
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])

    def _reasons(self, ob, side):
        return early_score(self._df(), ob, atr=1.0, side=side)[1]

    def test_buy_heavy_book_rewards_buy_only(self):
        ob = _book([(100 - i, 10) for i in range(4)] + [(99.6, 70)],
                   [(101 + i, 10) for i in range(5)])
        buy_r = self._reasons(ob, "BUY")
        sell_r = self._reasons(ob, "SELL")
        self.assertTrue(any(r.startswith("obi_bullish_") for r in buy_r))
        self.assertIn("bid_wall", buy_r)
        self.assertFalse(any(r.startswith("obi_bullish_") for r in sell_r))
        self.assertNotIn("bid_wall", sell_r)
        self.assertFalse(any(r.startswith("obi_bearish_") for r in sell_r))

    def test_sell_heavy_book_rewards_sell_only(self):
        ob = _book([(100 - i, 10) for i in range(5)],
                   [(101 + i, 10) for i in range(4)] + [(101.4, 70)])
        buy_r = self._reasons(ob, "BUY")
        sell_r = self._reasons(ob, "SELL")
        self.assertTrue(any(r.startswith("obi_bearish_") for r in sell_r))
        self.assertIn("ask_wall", sell_r)
        self.assertFalse(any(r.startswith("obi_bearish_") for r in buy_r))
        self.assertNotIn("ask_wall", buy_r)
        self.assertFalse(any(r.startswith("obi_bullish_") for r in buy_r))

    def test_exact_imbalance_threshold_boundary(self):
        # +0.20 is NOT funny-money: only strictly greater rewards a BUY.
        ob_neutral = _book([(100, 120)], [(101, 80)])   # (120-80)/200 = +0.20
        self.assertEqual(orderbook_imbalance(ob_neutral, depth=1), 0.20)
        buy_r_neutral = self._reasons(ob_neutral, "BUY")
        self.assertFalse(any(r.startswith("obi_bullish_") for r in buy_r_neutral))
        # +0.21 crosses the bar and must reward the BUY.
        ob_heavy = _book([(100, 121)], [(101, 79)])     # (121-79)/200 = +0.21
        buy_r_heavy = self._reasons(ob_heavy, "BUY")
        self.assertTrue(any(r.startswith("obi_bullish_") for r in buy_r_heavy))

    def test_balanced_book_rewards_neither_side(self):
        ob = _book([(100 - i, 10) for i in range(5)], [(101 + i, 10) for i in range(5)])
        for side in ("BUY", "SELL"):
            r = self._reasons(ob, side)
            self.assertFalse(any(r.startswith("obi_") for r in r))
            self.assertNotIn("bid_wall", r)
            self.assertNotIn("ask_wall", r)


class ScannerOrderbookLineTest(unittest.TestCase):
    """The deep-scanner's top-5 imbalance line must agree with the engine."""

    def test_scanner_top5_matches_engine_depth5(self):
        rng = random.Random(2024)
        for _ in range(150):
            bids = [(100.0 - i, rng.randint(0, 60)) for i in range(8)]
            asks = [(101.0 + i, rng.randint(0, 60)) for i in range(8)]
            ob = _book(bids, asks)
            self.assertAlmostEqual(
                DeepScanner._orderbook_imbalance(ob),
                orderbook_imbalance(ob, depth=5),
                places=12,
            )

    def test_scanner_ignores_depth_beyond_five(self):
        # level 6+ holds a monster stack; the scanner must NOT see it.
        bids = [(100 - i, 10) for i in range(5)] + [(94.0, 5000)]
        asks = [(101 + i, 10) for i in range(5)] + [(106.0, 10)]
        ob = _book(bids, asks)
        self.assertEqual(DeepScanner._orderbook_imbalance(ob), 0.0)
        self.assertAlmostEqual(orderbook_imbalance(ob, depth=5), 0.0, places=12)
        self.assertGreater(orderbook_imbalance(ob), 0.5)  # full book is bid-heavy

    def test_scanner_is_defensive_on_malformed_levels(self):
        ob = {"bids": [[100, 10], [99]],        # one bad 1-field level
              "asks": [[101, 10]]}
        self.assertEqual(DeepScanner._orderbook_imbalance(ob), 0.0)
        self.assertEqual(DeepScanner._orderbook_imbalance(None), 0.0)
        self.assertEqual(DeepScanner._orderbook_imbalance({}), 0.0)

    def test_scanner_side_sign_agrees_with_engine(self):
        rng = random.Random(55)
        for _ in range(120):
            bids = [(100.0 - i, rng.randint(1, 50)) for i in range(7)]
            asks = [(101.0 + i, rng.randint(1, 50)) for i in range(7)]
            ob = _book(bids, asks)
            s, e = DeepScanner._orderbook_imbalance(ob), orderbook_imbalance(ob, depth=5)
            self.assertEqual(np.sign(s), np.sign(e))


if __name__ == "__main__":
    unittest.main()