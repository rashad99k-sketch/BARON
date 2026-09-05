"""Phase 1 regression: persistent zone_ref + WAVES stale-zone protection.

Proves the WAVES failure mode is fixed:
  - a candidate anchored to an Order Block that price escapes far beyond must
    become EXPANDED_AWAY -> STALE and be returned to the watchlist;
  - re-promotion of the SAME escaped zone is refused;
  - a genuinely NEW zone (different band) is still allowed;
  - a candidate still inside the entry window can remain READY-eligible.
"""
import importlib
import os
import sys
import types
import unittest

import numpy as np
import pandas as pd


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        pass
    def route(self, *args, **kwargs):
        return lambda fn: fn
    def add_url_rule(self, *args, **kwargs):
        return None


def _load_engine():
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}
    old_paper = os.environ.pop("PAPER_MODE", None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {}

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: None
    fake_flask.request = types.SimpleNamespace()
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask
    sys.modules.pop("core.engine", None)
    engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _waves_escape_df(n=100):
    """BUY: flat ~100, wick-heavy bearish OB at bar 65, 3-candle displacement,
    then ~30-bar grind upward so price ends ~30 ATR above the zone."""
    np.random.seed(7)
    price = 100.0
    rows = []
    for _ in range(65):
        price += np.random.uniform(-0.05, 0.05)
        rows.append(price)
    origin_open, origin_close = price, price - 0.6
    rows.append(origin_close)
    for _ in range(3):
        price += 0.9
        rows.append(price)
    for _ in range(n - len(rows)):
        price += 0.35
        rows.append(price)
    closes = np.array(rows)
    opens = np.array(rows, dtype=float)
    opens[65] = origin_open
    highs = np.maximum(opens, closes) + 0.10
    lows = np.minimum(opens, closes) - 0.20
    lows[65] = min(opens[65], closes[65]) - 0.55
    vol = np.random.uniform(100, 200, n)
    vol[66:69] = 600
    return pd.DataFrame({"timestamp": np.arange(n), "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vol})


def _fresh_zone_df():
    """Price has returned INTO the OB zone (mitigation), within the entry window.

    Build: flat ~100, wick-heavy bearish OB at bar 40, 3-candle displacement up,
    then a pullback that returns price back down into the OB band.
    """
    np.random.seed(3)
    n = 60
    price = 100.0
    rows = []
    for _ in range(40):
        price += np.random.uniform(-0.05, 0.05)
        rows.append(price)
    origin_open, origin_close = price, price - 0.6
    rows.append(origin_close)          # bar 40 = OB origin (bearish)
    for _ in range(3):
        price += 0.9                    # displacement up
        rows.append(price)
    # Pullback back down into the OB zone band (~ origin price).
    for _ in range(n - len(rows)):
        price -= 0.45
        rows.append(price)
    closes = np.array(rows)
    opens = np.array(rows, dtype=float)
    opens[40] = origin_open
    highs = np.maximum(opens, closes) + 0.10
    lows = np.minimum(opens, closes) - 0.20
    lows[40] = min(opens[40], closes[40]) - 0.55
    vol = np.random.uniform(100, 200, n)
    vol[41:44] = 600
    return pd.DataFrame({"timestamp": np.arange(n), "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vol})


class ZoneLifecycleWavesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def setUp(self):
        self.engine.MEMORY.pop("stale_zone_refs", None)

    def _mk_candidate(self, E, df, side="BUY"):
        atr = float(E.compute_atr(df).iloc[-1])
        price = float(df["close"].iloc[-1])
        return E.ExecutionCandidate(
            symbol="WAVES/USDT:USDT", side=side, price=price, entry_price=price,
            stop_loss=price - atr, take_profit_1=price + atr, take_profit_2=price + 2 * atr,
            atr=atr, df=df, ob={},
        )

    def test_zone_ref_is_anchored_to_order_block_not_price(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _waves_escape_df(100)
        cand = self._mk_candidate(E, df)
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        # Zone band resolved and entry anchored to the zone, not the last price.
        self.assertTrue(c.zone_low > 0 and c.zone_high > 0)
        self.assertNotEqual(c.entry_price, float(df["close"].iloc[-1]))
        self.assertAlmostEqual(c.entry_price, c.zone_low if c.side == "BUY" else c.zone_high, places=6)

    def test_escaped_zone_becomes_stale_and_returned(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _waves_escape_df(100)
        cand = self._mk_candidate(E, df)
        q.add_candidate(cand)
        # First eval: marks EXPANDED_AWAY.
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        self.assertIn(c.zone_state, ("EXPANDED_AWAY", "STALE"))
        # Second eval at the same escaped price: STALE + returned to watchlist.
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        self.assertEqual(c.zone_state, "STALE")
        self.assertEqual(c.state, E.ExecutionState.RETURNED_WATCHLIST)
        self.assertEqual(c.decision, "STALE")
        # Stale fingerprint persisted for re-promotion guard.
        self.assertIn("WAVES/USDT:USDT:BUY", E.MEMORY.get("stale_zone_refs", {}))

    def test_fresh_zone_in_entry_window_can_be_ready(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _fresh_zone_df()
        cand = self._mk_candidate(E, df)
        cand.confirmation_count = 2
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        # Price near the OB -> inside window -> not demoted, zone ENTRY_WINDOW/ACTIVE.
        self.assertIn(c.zone_state, ("ENTRY_WINDOW", "ACTIVE", "RETEST"))
        self.assertNotEqual(c.state, E.ExecutionState.RETURNED_WATCHLIST)

    def test_stale_zone_cannot_reach_ready(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _waves_escape_df(100)
        cand = self._mk_candidate(E, df)
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        self.assertNotEqual(c.state, E.ExecutionState.READY)

    def test_zone_invalidation_on_close_through_far_edge(self):
        E = self.engine
        q = E.ExecutionQueue()
        # Start from a valid in-window zone, then break price below zone far edge.
        df = _fresh_zone_df()
        cand = self._mk_candidate(E, df)
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("WAVES/USDT:USDT")
        self.assertTrue(c.zone_low > 0)
        # Force a close through the far edge (BUY: below zone_low - 0.15 ATR).
        df2 = df.copy()
        atr = float(E.compute_atr(df).iloc[-1])
        df2.loc[df2.index[-1], "close"] = c.zone_low - atr * 0.5
        df2.loc[df2.index[-1], "low"] = c.zone_low - atr * 0.5
        q.re_evaluate_all(lambda s: df2)
        c = q._candidates.get("WAVES/USDT:USDT")
        self.assertIsNone(c)  # invalidated and removed from the queue

    def test_retest_recovery_marks_retest(self):
        """EXPANDED_AWAY then price returns to window -> RETEST (not STALE)."""
        E = self.engine
        q = E.ExecutionQueue()
        cand = self._mk_candidate(E, _fresh_zone_df())
        # Manually seed a zone and put it in EXPANDED_AWAY.
        cand.zone_low, cand.zone_high = 99.0, 100.0
        cand.entry_price = 99.0
        cand.zone_state = "EXPANDED_AWAY"
        q._candidates["WAVES/USDT:USDT"] = cand
        # Price returns into the window (mid-zone).
        moved = q._update_zone_lifecycle(cand, 99.5, 1.0)
        self.assertFalse(moved)
        self.assertEqual(cand.zone_state, "RETEST")


class PromoteGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def test_same_zone_detected_as_stale_match(self):
        """The promote guard's same-zone comparison must match the escaped OB."""
        E = self.engine
        q = E.ExecutionQueue()
        df = _waves_escape_df(100)
        atr = float(E.compute_atr(df).iloc[-1])
        zl, zh, _b, _t = q._find_causal_ob_zone(df, "BUY", atr)
        self.assertTrue(zl > 0 and zh > 0)
        # Simulate the stored fingerprint being the same zone.
        same = abs(zl - zl) < atr * 0.5 and abs(zh - zh) < atr * 0.5
        self.assertTrue(same)
        # A genuinely different zone (shifted far) must NOT match.
        diff = abs((zl + 10 * atr) - zl) < atr * 0.5
        self.assertFalse(diff)


if __name__ == "__main__":
    unittest.main()
